"""The command line, and the one question it refuses to answer for you.

An explicit transport flag always wins and never prompts, so scripted and
repeated use keeps working. With no flag, this asks, because an unanswered
question is a refusal in this project rather than a pass, and picking a
transport quietly is the same class of mistake as picking a scope quietly.
With no flag and no terminal, it refuses and names the flags, so a cron entry
or a CI job can never let silence choose.

The prompt is a CHOICE, not a confirmation. It is deliberately unlike the
typed-confirmation gate the sweepers put in front of a delete: this package
deletes nothing, and teaching an operator that typing at a prompt here is
routine would blunt the gate that matters.
"""

from __future__ import annotations

import argparse
import time
import json
import os
import tempfile
import sys
from typing import List, Optional, Sequence, TextIO, Tuple

from . import selftest
from .corroboration import (CorroborationUnavailable,
                            ElasticsearchVeto, Veto)
from .derivation.audit import run_audit
from .errors import GenerationChainError
from .model import AuditResult
from .reporting import coverage as coverage_report
from .reporting import manifest as manifest_writer
from .sizes import InvalidSize, parse_byte_size
from .sources import RepositorySource, overlap, prepared
from .sources.budget import available_bytes
from .sources.local import LocalMirrorSource
from .sources.oci import (OciCredentials, OciNativeSource, endpoint_for_region)
from .credentials import (CREDENTIAL_SUMMARY, load_elasticsearch,
                          load_oci, load_s3)
from .supported import SUPPORTED_SUMMARY
from .sources.s3 import (DEDICATED_ORACLE_ENDPOINT, STANDARD_ORACLE_ENDPOINT,
                         S3CompatibleSource, S3Credentials)

# EXIT CODES ARE A CONTRACT, not a detail. A scheduled job derives success
# from the code and retries on it, so the codes separate the failures worth
# retrying from the ones that will burn a backoff to reach the same answer.
# They are documented in --help and they stay stable.
EXIT_OK = 0
EXIT_REFUSED = 2      # A settled answer: this repository cannot be explained.
EXIT_USAGE = 3        # The invocation or a credential is wrong. Fix and rerun.
EXIT_TRANSPORT = 4    # The store or the cluster did not answer. Retry is sane.
EXIT_TOO_BIG = 5      # Not on this host. The same command fits on a larger one.

EXIT_CODES = """Exit codes
  0  the run completed and wrote a manifest
  2  the run refused for a settled reason, such as an unsupported repository
     format or a catalog it could not anchor. Retrying changes nothing
  3  the invocation or a credential is wrong. Fix it and run again
  4  the store or the cluster did not answer. A retry is reasonable
  5  a single shard directory is larger than this host can hold even alone.
     Run it somewhere with more memory, narrow it with --prefix, or raise
     --max-ram (or --memory-mb) if this host really has more than it reports"""

TRANSPORTS = ("s3", "oci", "local")


class ChoiceAbandoned(GenerationChainError):
    """The operator was asked and did not answer, which is a refusal."""


class Misconfigured(GenerationChainError):
    """The invocation cannot be completed, and the message says what is missing."""


# -- prompting ---------------------------------------------------------------

def _ask(stdin: TextIO, stderr: TextIO, question: str,
         options: Sequence[Tuple[str, str]]) -> str:
    stderr.write(question + "\n")
    for number, (_value, description) in enumerate(options, start=1):
        stderr.write(f"  {number}) {description}\n")
    stderr.write(f"Choose 1 to {len(options)}: ")
    stderr.flush()
    answer = stdin.readline().strip()
    for number, (value, _description) in enumerate(options, start=1):
        if answer == str(number):
            return value
    raise ChoiceAbandoned(
        f"{answer!r} is not one of 1 to {len(options)}; nothing was chosen "
        "and nothing was read")


def _line(stdin: TextIO, stderr: TextIO, question: str) -> str:
    stderr.write(question + " ")
    stderr.flush()
    answer = stdin.readline().strip()
    if not answer:
        raise ChoiceAbandoned(f"nothing was entered for: {question}")
    return answer


def choose_transport(stdin: TextIO, stderr: TextIO) -> str:
    return _ask(stdin, stderr, "Which store holds the repository?", [
        ("s3", "the S3 compatibility API (MinIO, AWS, or Oracle's Amazon S3 "
               "Compatibility API)"),
        ("oci", "OCI native Object Storage"),
        ("local", "a local mirror of the bucket on disk"),
    ])


def choose_s3_endpoint(stdin: TextIO, stderr: TextIO) -> str:
    """Make the operator pick an endpoint rather than constructing one.

    Oracle publishes two S3 compatibility domains. The dedicated one is
    commercial realm OC1 only, from 19.24, and Oracle recommends it; the
    standard one exists in every realm. Reaching the wrong one fails as a
    connection error or a bare 403, which reads like a network problem or a
    credential problem, so an operator can burn an hour on the wrong
    diagnosis. This tool derives neither.
    """
    form = _ask(stdin, stderr, "Which endpoint?", [
        ("standard", f"{STANDARD_ORACLE_ENDPOINT}   (Oracle, every realm)"),
        ("dedicated", f"{DEDICATED_ORACLE_ENDPOINT}   (Oracle, commercial "
                      "realm OC1 only, 19.24 and later, Oracle recommends it)"),
        ("other", "another S3-compatible endpoint (MinIO, AWS, something else)"),
    ])
    if form == "other":
        return _line(stdin, stderr, "Endpoint URL:")
    namespace = _line(stdin, stderr, "Object Storage namespace:")
    region = _line(stdin, stderr, "Region:")
    template = (STANDARD_ORACLE_ENDPOINT if form == "standard"
                else DEDICATED_ORACLE_ENDPOINT)
    return template.replace("<namespace>", namespace).replace("<region>", region)


# -- assembling the source ---------------------------------------------------

def _corroboration(args: argparse.Namespace) -> Optional[Veto]:
    """The Elasticsearch veto, when it was asked for.

    Returns None only when nobody asked. A request that could not be answered
    raises, and the run refuses: proceeding would produce a manifest LARGER
    than a successful call would have, and a failure that grows the list is
    the one thing this tool guarantees cannot happen.

    If this refuses, the remedy is the credential, the endpoint or the network.
    Dropping --elasticsearch is not a remedy for a corroboration that failed,
    and this tool will not suggest it as one.
    """
    if not args.elasticsearch:
        return None
    if not args.es_repository:
        raise Misconfigured(
            "--elasticsearch needs --es-repository naming the repository as "
            "Elasticsearch knows it")
    return ElasticsearchVeto(
        endpoint=args.elasticsearch, repository=args.es_repository,
        credentials=load_elasticsearch(args.credentials),
        ca_certificate=args.es_ca_cert).fetch()


def build_source(transport: str, args: argparse.Namespace, stdin: TextIO,
                 stderr: TextIO) -> RepositorySource:
    if transport == "local":
        root = args.local_repo or _line(
            stdin, stderr, "Path to the mirrored repository:")
        return LocalMirrorSource(root)
    if transport == "s3":
        endpoint = args.endpoint or choose_s3_endpoint(stdin, stderr)
        if not args.bucket:
            raise Misconfigured("the S3 path needs --bucket")
        if not args.region:
            raise Misconfigured(
                "the S3 path needs --region; a wrong region and a wrong "
                "endpoint both answer a bare 403, so this is never defaulted")
        return S3CompatibleSource(endpoint=endpoint, region=args.region,
                                  bucket=args.bucket, prefix=args.prefix,
                                  credentials=load_s3(args.credentials,
                                                      args.profile),
                                  allow_plain_http=args.insecure_http)
    if not args.namespace or not args.bucket:
        raise Misconfigured("the OCI native path needs --namespace and --bucket")
    if not args.oci_region and not args.endpoint:
        raise Misconfigured(
            "the OCI native path needs --oci-region or --endpoint")
    host = endpoint_for_region(args.oci_region or "", args.endpoint or None)
    return OciNativeSource(
        endpoint=f"https://{host}", namespace=args.namespace,
        bucket=args.bucket, prefix=args.prefix,
        credentials=load_oci(args.credentials, args.oci_profile))


# -- the command -------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m generation_chain",
        description="Reconstruct the delete operations a snapshot "
                    "repository's generation chain records. Reads only, "
                    "deletes nothing, and writes manifests.",
        epilog=EXIT_CODES + "\n\n" + SUPPORTED_SUMMARY + "\n\n"
               + CREDENTIAL_SUMMARY,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transport", choices=TRANSPORTS,
                        help="name the store to read; without this the tool "
                             "asks, and with no terminal it refuses")
    parser.add_argument("--local-repo", metavar="DIR",
                        help="a mirrored bucket on disk; naming it names the "
                             "local transport")
    parser.add_argument("--endpoint", help="endpoint URL, or an OCI host")
    parser.add_argument("--region", help="region for the S3 compatibility path")
    parser.add_argument("--bucket")
    parser.add_argument("--prefix", default="",
                        help="the repository's base_path inside the bucket")
    parser.add_argument("--namespace", help="OCI Object Storage namespace")
    parser.add_argument("--oci-region")
    parser.add_argument("--credentials", metavar="FILE",
                        help="a JSON file holding the s3, oci or "
                             "elasticsearch sections this run needs. A PATH, "
                             "never a value: a secret in argv is visible in "
                             "ps to every user on the host. Without this, the "
                             "standard locations are read (~/.aws/credentials, "
                             "~/.oci/config), then the environment")
    parser.add_argument("--profile", default="default",
                        help="profile name inside ~/.aws/credentials")
    parser.add_argument("--oci-profile", default="DEFAULT",
                        help="profile name inside ~/.oci/config")
    parser.add_argument("--manifest", metavar="FILE",
                        help="orphan manifest, tab separated, key first "
                             "(default: standard output)")
    parser.add_argument("--classification", metavar="FILE",
                        help="every key in the store and its disposition")
    parser.add_argument("--coverage-json", metavar="FILE",
                        help="the coverage record, to keep as evidence of this run")
    parser.add_argument("--elasticsearch", metavar="URL",
                        help="ask a cluster what to protect. Everything it "
                             "reports leaves the manifest; what it does not "
                             "report is not thereby condemned. If this is "
                             "passed and the cluster cannot be consulted, the "
                             "run refuses")
    parser.add_argument("--es-repository", metavar="NAME",
                        help="the repository name as Elasticsearch knows it")
    parser.add_argument("--es-ca-cert", metavar="FILE")
    parser.add_argument("--self-test", action="store_true",
                        help="prove the signing and the framing offline")
    parser.add_argument("--concurrency", type=int,
                        default=overlap.DEFAULT_CONCURRENCY, metavar="N",
                        help=f"how many store reads may be outstanding at "
                             f"once (default {overlap.DEFAULT_CONCURRENCY}, "
                             f"maximum {overlap.MAX_CONCURRENCY}). 1 makes "
                             "every read wait for the one before it, which a "
                             "store that answers a burst with 429s may need. "
                             "The manifest is the same at every setting")
    memory = parser.add_mutually_exclusive_group()
    memory.add_argument("--memory-mb", type=int, default=None, metavar="MB",
                        help="the memory this run may plan on using, in "
                             "megabytes. Without it, or --max-ram, the host "
                             "is asked, and a host that does not say gets no "
                             "ceiling. This run sizes how many shard "
                             "directories it reads at once to fit; only a "
                             "single shard directory too large to hold even "
                             "alone still refuses before it is read. 0 turns "
                             "the ceiling off. Kept for scripts already "
                             "passing it; --max-ram takes a unit and cannot "
                             "be off by three orders of magnitude from a typo")
    memory.add_argument("--max-ram", type=_size_argument, default=None,
                        metavar="SIZE",
                        help="the memory this run may plan on using, such as "
                             "4GiB or 512MiB. A bare number is refused rather "
                             "than guessed as bytes or megabytes. Same "
                             "meaning as --memory-mb; the two cannot be "
                             "passed together")
    parser.add_argument(
        "--insecure-http", action="store_true",
        help="send to a plain http endpoint that is not loopback. A manifest names exactly which production objects are about to be deleted, so this is only for a lab store on a network you trust")
    parser.add_argument(
        "--quiet", action="store_true",
        help="do not write progress to stderr. A run against a real "
             "repository reads for many minutes and says nothing without "
             "it, which leaves you unable to tell working from stuck")
    return parser


def _size_argument(text: str) -> int:
    try:
        return parse_byte_size(text)
    except InvalidSize as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _budget_bytes(args: argparse.Namespace) -> Optional[int]:
    """The memory this run may plan on using, or None for no ceiling.

    `--max-ram` and `--memory-mb` cannot both be set: `build_parser` puts
    them in a mutually exclusive group, so there is no reading of this
    function where the two could name different amounts and one has to win.
    """
    if args.max_ram is not None:
        return args.max_ram
    if args.memory_mb is not None:
        return args.memory_mb * (1 << 20) if args.memory_mb > 0 else None
    return available_bytes()


def _named_transport(args: argparse.Namespace, stderr: TextIO) -> Optional[str]:
    if args.transport and args.local_repo and args.transport != "local":
        raise Misconfigured(
            f"--transport {args.transport} and --local-repo name different "
            "stores")
    if args.transport:
        return args.transport
    if args.local_repo:
        return "local"
    return None


def main(argv: Optional[Sequence[str]] = None, stdin: Optional[TextIO] = None,
         stdout: Optional[TextIO] = None,
         stderr: Optional[TextIO] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr

    if args.self_test:
        return EXIT_OK if selftest.run(stderr) == 0 else EXIT_REFUSED

    try:
        transport = _named_transport(args, stderr)
        if transport is None:
            if not stdin.isatty():
                stderr.write(
                    "No transport was named and there is no terminal to ask "
                    f"at. Pass --transport {{{','.join(TRANSPORTS)}}}, or "
                    "--local-repo DIR for a mirror. Nothing was read.\n")
                return EXIT_USAGE
            transport = choose_transport(stdin, stderr)
        # The stack the run reads through: the transport, then the guard,
        # escalation and read-ahead wrappers `prepared` assembles. The
        # memory ceiling no longer wraps the transport: it used to refuse a
        # repository this host could not hold in one go, and now it sizes
        # how many shard directories `run_audit` reads at once, so it is
        # passed to `run_audit` below instead of built into the source.
        source = prepared(build_source(transport, args, stdin, stderr),
                          concurrency=args.concurrency)
    except GenerationChainError as exc:
        stderr.write(f"{exc}\n")
        return EXIT_USAGE

    try:
        veto = _corroboration(args)
    except CorroborationUnavailable as exc:
        stderr.write(f"{exc}\n")
        return EXIT_TRANSPORT if exc.transient else EXIT_REFUSED
    except GenerationChainError as exc:
        stderr.write(f"{exc}\n")
        return EXIT_USAGE
    def _progress(message: str) -> None:
        # Straight to stderr, unbuffered, so it is visible while the run is
        # still going. The report goes to stderr too and the manifest goes to
        # its own file, so nothing here can end up parsed as a result.
        stderr.write(f"[{time.strftime('%H:%M:%S')}] {message}\n")
        stderr.flush()

    result = run_audit(source, veto, budget_bytes=_budget_bytes(args),
                       progress=None if args.quiet else _progress)
    # Optional across transports. A source that cannot size cheaply omits the
    # method, and the report says so rather than paying a request per object.
    sizer = getattr(source, "sizes", None)
    try:
        sizes = sizer() if callable(sizer) else {}
    except Exception:
        sizes = {}
    _write(result, transport, source.describe(), args, stdout, stderr,
           sizes=sizes)
    if not result.coverage.refused:
        return EXIT_OK
    if result.coverage.refusal_needs_a_bigger_host:
        return EXIT_TOO_BIG
    return EXIT_TRANSPORT if result.coverage.refusal_is_transient else EXIT_REFUSED


def _write_atomically(path: str, render) -> None:
    """Write through a neighbouring temporary file and rename over the target.

    A manifest is a list an operator acts on. A run interrupted part way
    through writing one leaves a file that reads as a complete, shorter
    manifest, and nothing in it says it was cut off. The rename makes the file
    appear whole or not at all.

    A path this cannot be done next to, such as /dev/null, is written directly.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, prefix=".genchain-",
            suffix=".part", delete=False)
    except OSError:
        with open(path, "w", encoding="utf-8") as direct:
            render(direct)
        return
    try:
        with handle:
            render(handle)
        os.replace(handle.name, path)
    except BaseException:
        safe_unlink(handle.name)
        raise


def safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _write_manifest_file(result: AuditResult, handle: TextIO) -> None:
    """The manifest, and a marker naming it whole once every row is written.

    An operator reading `--coverage-json` already sees `refused`; this puts
    the same fact where a reviewer opening only the manifest file will find
    it. `manifest_writer.write_manifest` never writes the marker itself, so
    a refused run's file, and this module's own unit tests calling
    `write_manifest` directly, stay exactly what they were: a header with no
    claim attached to it.
    """
    manifest_writer.write_manifest(result.condemned, handle)
    if not result.coverage.refused:
        handle.write(manifest_writer.COMPLETION_MARKER)


def _write(result: AuditResult, transport: str, location: str,
           args: argparse.Namespace, stdout: TextIO, stderr: TextIO,
           sizes=None) -> None:
    coverage_report.write_report(result, transport, location, stderr,
                                 sizes=sizes)
    dropped = manifest_writer.excluded_keys(result.condemned)
    if dropped:
        stderr.write(f"  {len(dropped)} key(s) were left out because they hold "
                     "a tab, a newline or a control character and cannot be "
                     "written to a tab separated file.\n")
    if args.manifest:
        _write_atomically(args.manifest, lambda h: _write_manifest_file(result, h))
    else:
        manifest_writer.write_manifest(result.condemned, stdout)
    if args.classification:
        _write_atomically(args.classification, lambda h:
                          manifest_writer.write_classification(
                              result.classification, h))
    if args.coverage_json:
        document = coverage_report.as_document(result, transport, location)
        _write_atomically(args.coverage_json, lambda h:
                          json.dump(document, h, indent=1, sort_keys=True))



