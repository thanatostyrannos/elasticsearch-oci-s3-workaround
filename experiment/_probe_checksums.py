#!/usr/bin/env python3
"""Ask a store which delete checksum it accepts, without deleting anything.

`DeleteObjects` is checksum required on every store this project has measured,
and stores disagree about which checksum. Oracle's S3 Compatibility API takes
`Content-MD5`, `crc32c` or `sha256` and rejects `crc32`, which is the one the
AWS SDK sends by default and the reason a snapshot repository on that store
leaks. AWS S3 takes `crc32`. Which one yours takes decides what you pass to
`--checksum-algorithm`, and guessing wrong costs a confusing 400.

This sends one batch delete per algorithm and reports what came back.

IT DELETES NOTHING. A store validates the checksum header before it resolves
the keys in the body, so a batch naming a key that cannot exist gets the same
verdict on the header as a real one would. Each probe names a single key under
a random UUID that no repository would ever contain. If a store somehow did
hold that key it would be removed, which is why the key is a UUID rather than
anything guessable, and why the prefix says what this is.

Read only in effect, then, but it is not the audit: it sends POST, so it lives
outside `generation_chain/` and is a separate script you run deliberately.

    python3 probe_checksums.py \\
      --endpoint https://NAMESPACE.compat.objectstorage.REGION.oraclecloud.com \\
      --region REGION --bucket BUCKET --credentials creds.json
"""
from __future__ import annotations

import argparse
import sys
import urllib.parse
import uuid

from generation_chain.credentials import load_s3
from generation_chain.errors import GenerationChainError
from generation_chain.reclaim import batch, transport
from generation_chain.reclaim.checksum import (SUPPORTED_ALGORITHMS,
                                                checksum_header)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNREACHABLE = 4


def probe_key() -> str:
    """A key no repository holds, so a delete of it removes nothing."""
    return f"generation-chain-checksum-probe/{uuid.uuid4()}.probe"



def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Report which DeleteObjects checksum a store accepts. "
                    "Deletes nothing.")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--credentials", required=True,
                        help="JSON credentials file, mode 0600. A PATH, never "
                             "a value: an argument is visible in ps")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    try:
        credentials = load_s3(args.credentials)
    except GenerationChainError as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_USAGE

    sys.stderr.write(f"probing {args.endpoint}/{args.bucket}\n")
    sys.stderr.write("each probe names one key that does not exist, so nothing "
                     "is removed\n\n")

    accepted = []
    parsed = urllib.parse.urlparse(args.endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        sys.stderr.write(f"--endpoint must be an http or https URL: {args.endpoint}\n")
        return EXIT_USAGE

    for algorithm in SUPPORTED_ALGORITHMS:
        key = probe_key()
        body = batch.build_request_body([key])
        try:
            response = transport.send_batch_delete(
                scheme=parsed.scheme, host=parsed.netloc, region=args.region,
                bucket=args.bucket, credentials=credentials, body=body,
                checksum=checksum_header(algorithm, body),
                timeout=args.timeout)
        except transport.TransportError as exc:
            sys.stdout.write(f"  {algorithm:<8} refused: {exc}\n")
            continue
        except Exception as exc:                      # noqa: BLE001
            sys.stderr.write(f"  {algorithm:<8} could not ask: {exc}\n")
            return EXIT_UNREACHABLE
        accepted.append(algorithm)
        sys.stdout.write(f"  {algorithm:<8} accepted\n")

    sys.stdout.write("\n")
    if not accepted:
        sys.stdout.write(
            "This store refused every checksum this tool can compute. That is "
            "not a store\nthis project can reclaim from. Keep the report, it "
            "is the useful artefact.\n")
        return EXIT_OK
    sys.stdout.write(f"accepted: {', '.join(accepted)}\n")
    preferred = "md5" if "md5" in accepted else accepted[0]
    sys.stdout.write(f"pass --checksum-algorithm {preferred} to the delete tool\n")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
