"""The reclaim command: read one approved manifest, delete what it names.

Separate entry point from `generation_chain/cli.py` on purpose. The audit
command reads a repository and writes a manifest; this command reads a
manifest and deletes. Keeping them apart means an operator can see, from the
command line alone, which one they are running, and it means this module
never has to import anything the audit path exposes for building a request.

DRY RUN IS THE DEFAULT. Every invocation without `--execute` builds the exact
requests it would send, checksums included, and reports them without sending
anything. `--execute` needs `--approve-digest` and `--approve-rows` naming
this exact manifest (see `approval.py`); without a match, nothing is deleted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from typing import Dict, List, Optional, Sequence, TextIO, Tuple

from ..corroboration import ElasticsearchVeto
from ..credentials import load_elasticsearch, load_s3
from ..errors import GenerationChainError
from ..sources.s3 import S3Credentials, _refuse_plain_http
from . import batch
from .approval import ApprovalError, verify_approval
from . import recheck
from .checksum import (DEFAULT_ALGORITHM, SUPPORTED_ALGORITHMS, ChecksumError,
                       checksum_header)
from .manifest import ManifestData, ManifestError, load_manifest
from .transport import TransportError, send_batch_delete

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_APPROVAL_REFUSED = 3
EXIT_PARTIAL = 4

EXIT_CODES = """Exit codes
  0  dry run reported, or every key executed against was deleted or already
     absent
  2  the invocation, the manifest, or the checksum algorithm is wrong
  3  --execute was passed without an approval matching this exact manifest
  4  the run executed and at least one key failed or went unconfirmed"""


class Misconfigured(GenerationChainError):
    """The invocation cannot be completed, and the message says what is missing."""


def normalise_prefix(prefix: str) -> str:
    """The same one-line rule `sources/s3.py` applies to a repository prefix.

    Kept here rather than imported so this package never reaches into the
    read transport for anything. Duplicated as one expression rather than a
    dependency; this project's own test suite pins it against
    `S3CompatibleSource` directly so the two cannot silently drift apart.
    """
    return (prefix.strip("/") + "/") if prefix.strip("/") else ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m generation_chain.reclaim",
        description="Delete exactly the keys an approved manifest names. "
                    "Dry run by default; --execute needs an approval that "
                    "matches this exact manifest.",
        epilog=EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True, metavar="FILE",
                        help="the orphan manifest to delete, as written by "
                             "the audit tool's --manifest")
    parser.add_argument("--endpoint", help="the store's endpoint URL")
    parser.add_argument("--region", help="region for the S3 compatibility path")
    parser.add_argument("--bucket")
    parser.add_argument("--prefix", default="",
                        help="the repository's base_path inside the bucket; "
                             "must match what the manifest was derived under")
    parser.add_argument("--credentials", metavar="FILE",
                        help="a JSON file holding the s3 section; without "
                             "this, ~/.aws/credentials then the environment "
                             "are read. Never a value on the command line")
    parser.add_argument("--profile", default="default",
                        help="profile name inside ~/.aws/credentials")
    parser.add_argument("--checksum-algorithm", default=DEFAULT_ALGORITHM,
                        choices=SUPPORTED_ALGORITHMS,
                        help=f"the content checksum this store requires on "
                             f"DeleteObjects (default {DEFAULT_ALGORITHM}). "
                             "The store decides this, not this tool; an "
                             "unrecognised value is refused rather than "
                             "guessed at")
    parser.add_argument("--execute", action="store_true",
                        help="send the deletes. Without this, nothing is "
                             "sent and the run only reports what would be")
    parser.add_argument("--approve-digest", metavar="SHA256",
                        help="the sha256 of this exact manifest's bytes; "
                             "required with --execute")
    parser.add_argument("--approve-rows", type=int, metavar="N",
                        help="how many keys this exact manifest names; "
                             "required with --execute")
    parser.add_argument("--timeout", type=float, default=60.0, metavar="SECS")
    parser.add_argument("--report", metavar="FILE",
                        help="append one JSON line per batch's outcome here")
    parser.add_argument(
        "--insecure-http", action="store_true",
        help="send to a plain http endpoint that is not loopback. A manifest names exactly which production objects are about to be deleted, so this is only for a lab store on a network you trust")
    group = parser.add_argument_group(
        "re-checking the cluster at execute time",
        "The manifest's Elasticsearch protection was decided when it was "
        "derived. A searchable snapshot mounted since then is not in it, and "
        "Elasticsearch does not stop a snapshot backing a mount from being "
        "deleted. One of the two flags below is required with --execute.")
    group.add_argument(
        "--elasticsearch", metavar="URL",
        help="re-check the veto against this cluster before deleting, and "
             "refuse if it now protects anything in the manifest")
    group.add_argument(
        "--es-repository", metavar="NAME",
        help="the repository as Elasticsearch knows it; needed with "
             "--elasticsearch")
    group.add_argument("--es-ca-cert", metavar="FILE")
    group.add_argument(
        "--without-elasticsearch", action="store_true",
        help="state that no cluster can be asked, which is the case for an "
             "orphaned repository. Deliberate, because it is the path with "
             "no second opinion")
    group.add_argument(
        "--max-manifest-age", type=int, metavar="SECONDS",
        default=recheck.DEFAULT_MAX_MANIFEST_AGE_SECONDS,
        help="refuse a manifest older than this, because the cluster can "
             "change under it. 0 disables the check (default: %(default)s)")
    return parser


def _store_keys(manifest: ManifestData, prefix: str) -> Dict[str, str]:
    """Store key -> the manifest's own relative key, in manifest order.

    A dict rather than a list because reporting translates a store key back
    to the spelling an operator recognises from the manifest; built with a
    dict comprehension rather than a loop that could drop or reorder an entry.
    """
    normalised = normalise_prefix(prefix)
    return {normalised + key: key for key in manifest.keys}


def _require_store_arguments(args: argparse.Namespace) -> Tuple[str, str]:
    if not args.endpoint or not args.region or not args.bucket:
        raise Misconfigured(
            "the store must be named to build even a dry-run request: "
            "--endpoint, --region and --bucket are all required")
    parsed = urllib.parse.urlsplit(args.endpoint)
    if parsed.scheme and parsed.netloc:
        # Same rule as the audit transport in sources/s3.py. The delete path
        # sends the keys themselves, so if anything must not travel in the
        # clear it is this one.
        _refuse_plain_http(parsed, args.endpoint, args.insecure_http)
    if not parsed.scheme or not parsed.netloc:
        raise Misconfigured(f"--endpoint {args.endpoint!r} is not a URL")
    return parsed.scheme, parsed.netloc


def _report_line(stream: TextIO, batch_number: int, requested: Sequence[str],
                 outcome) -> None:
    stream.write(json.dumps({
        "batch": batch_number,
        "requested": list(requested),
        "deleted": list(outcome.deleted),
        "already_absent": [{"key": k, "code": c, "message": m}
                          for k, c, m in outcome.already_absent],
        "failed": [{"key": k, "code": c, "message": m}
                  for k, c, m in outcome.failed],
        "unconfirmed": list(outcome.unconfirmed),
    }) + "\n")
    stream.flush()


def _relative(store_to_manifest: Dict[str, str], key: str) -> str:
    return store_to_manifest.get(key, key)


def main(argv: Optional[Sequence[str]] = None, stdout: Optional[TextIO] = None,
         stderr: Optional[TextIO] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr

    try:
        manifest = load_manifest(args.manifest)
        scheme, host = _require_store_arguments(args)
    except (ManifestError, Misconfigured) as exc:
        stderr.write(f"{exc}\n")
        return EXIT_USAGE

    store_to_manifest = _store_keys(manifest, args.prefix)
    store_keys: List[str] = list(store_to_manifest.keys())
    batches = list(batch.chunks(store_keys))

    stderr.write(
        f"manifest: {args.manifest}\n"
        f"  {len(manifest.keys)} key(s), sha256 {manifest.digest}\n"
        f"  {len(batches)} batch(es) of up to {batch.MAX_KEYS_PER_BATCH}, "
        f"checksum algorithm {args.checksum_algorithm}\n"
        f"  target: {scheme}://{host}/{args.bucket}"
        f"{'/' + args.prefix.strip('/') if args.prefix.strip('/') else ''}\n")

    if not args.execute:
        return _dry_run(manifest, batches, args, stderr)

    try:
        if args.approve_digest is None or args.approve_rows is None:
            raise ApprovalError(
                "--execute needs --approve-digest and --approve-rows naming "
                "this exact manifest; neither is optional and neither is "
                "inferred")
        verify_approval(manifest, args.approve_digest, args.approve_rows)
    except ApprovalError as exc:
        stderr.write(f"{exc}\n")
        return EXIT_APPROVAL_REFUSED
    problem = recheck.corroboration_choice_problem(
        args.elasticsearch, args.without_elasticsearch)
    if problem:
        stderr.write(f"{problem}\n")
        return EXIT_USAGE

    try:
        age = time.time() - os.path.getmtime(manifest.path)
    except OSError as exc:
        stderr.write(f"cannot read the age of {manifest.path}: {exc}\n")
        return EXIT_USAGE
    problem = recheck.staleness_problem(age, args.max_manifest_age,
                                        manifest.path)
    if problem:
        stderr.write(f"{problem}\n")
        return EXIT_APPROVAL_REFUSED

    if args.elasticsearch:
        if not args.es_repository:
            stderr.write("--elasticsearch needs --es-repository\n")
            return EXIT_USAGE
        try:
            veto = ElasticsearchVeto(
                endpoint=args.elasticsearch, repository=args.es_repository,
                credentials=load_elasticsearch(args.credentials),
                ca_certificate=args.es_ca_cert).fetch()
        except GenerationChainError as exc:
            # A veto that could not be fetched is not a veto that said yes.
            stderr.write(
                f"the cluster could not be asked, so nothing was deleted: "
                f"{exc}\n")
            return EXIT_APPROVAL_REFUSED
        problem = recheck.protection_problem(
            recheck.newly_protected(manifest.keys, veto), len(manifest.keys))
        if problem:
            stderr.write(f"{problem}\n")
            return EXIT_APPROVAL_REFUSED

    try:
        credentials = load_s3(args.credentials, args.profile)
    except GenerationChainError as exc:
        stderr.write(f"{exc}\n")
        return EXIT_USAGE

    return _execute(batches, store_to_manifest, scheme, host, args,
                    credentials, stdout, stderr)


def _dry_run(manifest: ManifestData, batches, args: argparse.Namespace,
            stderr: TextIO) -> int:
    if not batches:
        stderr.write("the manifest names no keys. Nothing would be sent.\n")
        return EXIT_OK
    try:
        preview_body = batch.build_request_body(batches[0])
        header, value = checksum_header(args.checksum_algorithm, preview_body)
    except ChecksumError as exc:
        stderr.write(f"{exc}\n")
        return EXIT_USAGE
    stderr.write(
        f"DRY RUN. Nothing was sent. The first batch's request:\n"
        f"  POST ...?delete, {len(preview_body)} byte body, "
        f"{len(batches[0])} key(s)\n"
        f"  {header}: {value}\n"
        "To execute against this exact manifest:\n"
        f"  --execute --approve-digest {manifest.digest} "
        f"--approve-rows {len(manifest.keys)}\n"
        "and one of these, because this manifest's protection was decided "
        "when it was derived\nand a searchable snapshot mounted since then "
        "is not in it:\n"
        "  --elasticsearch URL --es-repository NAME   re-check the veto now\n"
        "  --without-elasticsearch                    no cluster left to "
        "ask\n")
    return EXIT_OK


def _execute(batches, store_to_manifest: Dict[str, str], scheme: str,
            host: str, args: argparse.Namespace, credentials: S3Credentials,
            stdout: TextIO, stderr: TextIO) -> int:
    report = open(args.report, "a", encoding="utf-8") if args.report else None
    deleted: List[str] = []
    already_absent: List[Tuple[str, str, str]] = []
    failed: List[Tuple[str, str, str]] = []
    unconfirmed: List[str] = []
    try:
        for number, keys in enumerate(batches, start=1):
            body = batch.build_request_body(keys)
            try:
                header = checksum_header(args.checksum_algorithm, body)
            except ChecksumError as exc:
                stderr.write(f"{exc}\n")
                return EXIT_USAGE
            outcome = _send_one_batch(scheme, host, args, credentials, body,
                                      header, keys)
            if report is not None:
                _report_line(report, number, keys, outcome)
            deleted.extend(outcome.deleted)
            already_absent.extend(outcome.already_absent)
            failed.extend(outcome.failed)
            unconfirmed.extend(outcome.unconfirmed)
    finally:
        if report is not None:
            report.close()

    _write_tally(store_to_manifest, deleted, already_absent, failed,
                unconfirmed, stdout)
    return EXIT_OK if not failed and not unconfirmed else EXIT_PARTIAL


def _send_one_batch(scheme: str, host: str, args: argparse.Namespace,
                    credentials: S3Credentials, body: bytes,
                    checksum: Tuple[str, str], keys: Sequence[str]):
    try:
        response = send_batch_delete(
            scheme=scheme, host=host, region=args.region, bucket=args.bucket,
            credentials=credentials, body=body, checksum=checksum,
            timeout=args.timeout)
    except TransportError as exc:
        # The whole batch failed at the transport level, so not one of its
        # keys was confirmed deleted. Recorded as `failed` per key rather than
        # silently dropped, so the final tally still accounts for every key
        # the manifest named.
        return batch.BatchOutcome(
            failed=tuple((key, "TransportError", str(exc)) for key in keys))
    try:
        return batch.parse_response(response, keys)
    except batch.BatchDeleteError as exc:
        # A 200 whose body this package cannot read is not evidence that any
        # of these keys were removed, so every one of them is `failed` here
        # rather than `deleted` by default.
        return batch.BatchOutcome(
            failed=tuple((key, "UnparseableResponse", str(exc))
                        for key in keys))


def _write_tally(store_to_manifest: Dict[str, str], deleted, already_absent,
                 failed, unconfirmed, stdout: TextIO) -> None:
    stdout.write(
        f"deleted: {len(deleted)}\n"
        f"already absent: {len(already_absent)}\n"
        f"failed: {len(failed)}\n"
        f"unconfirmed: {len(unconfirmed)}\n")
    for key, code, message in already_absent:
        stdout.write(f"  absent  {_relative(store_to_manifest, key)}: "
                     f"{code} {message}\n")
    for key, code, message in failed:
        stdout.write(f"  FAILED  {_relative(store_to_manifest, key)}: "
                     f"{code} {message}\n")
    for key in unconfirmed:
        stdout.write(f"  UNCONFIRMED  {_relative(store_to_manifest, key)}: "
                     "not named in the store's response\n")


if __name__ == "__main__":
    sys.exit(main())
