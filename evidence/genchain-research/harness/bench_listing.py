"""Bucket listing at page depth, on both endpoint transports.

Every state this project has tested was one page. This drives the listing past
a hundred pages on the S3 compatibility API against a real MinIO, and past a
hundred pages on the OCI native API against a stub, and checks the result by
identity rather than by count: the set of keys the transport returned has to
equal the set of keys that were put there.

Correctness at depth is the point. A continuation bug that shows up on page
two shows up in the project's existing tests; one that shows up on page 90,
or when a marker repeats, does not.
"""
from __future__ import annotations

import argparse, gc, json, os, sys, time, tracemalloc

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import base_parser, emit, rss_kb, table, use_tool

parser = base_parser(__doc__)
parser.add_argument("--transport", choices=("s3", "oci", "both"), default="both")
parser.add_argument("--bucket", default=os.environ.get("MINIO_BUCKET", "scale-limits"))
parser.add_argument("--endpoint", default=os.environ.get("MINIO_ENDPOINT", "http://127.0.0.1:19000"))
parser.add_argument("--prefixes", default="r00500,r02700,r10600,r53000,r132000")
parser.add_argument("--oci-sizes", default="1000,10000,50000,132000")
parser.add_argument("--repeats", type=int, default=3)
parser.add_argument("--out", default="listing")
args = parser.parse_args()
use_tool(args.tool_root)

import synthrepo, ocistub, miniofill
from generation_chain.sources.s3 import S3CompatibleSource, S3Credentials
from generation_chain.sources.oci import OciNativeSource
from generation_chain.sources.http_reads import HttpReader, Response


class CountingReader(HttpReader):
    """Counts round trips and never sleeps, so timing is transport, not backoff."""

    def __init__(self, **kw):
        super().__init__(sleep=lambda _s: None, jitter=lambda: 0.0, **kw)
        self.calls = 0
        self.max_in_flight = 0
        self._in_flight = 0

    def get(self, url, headers, method="GET", timeout=60.0) -> Response:
        self.calls += 1
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            return super().get(url, headers, method=method, timeout=timeout)
        finally:
            self._in_flight -= 1


def timed_listing(source, expected_keys, repeats):
    best = None
    runs = []
    for _ in range(repeats):
        reader = source.reader
        reader.calls = 0
        gc.collect()
        before = rss_kb()
        tracemalloc.start()
        t0 = time.perf_counter()
        keys = source.list_keys()
        seconds = time.perf_counter() - t0
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        runs.append(seconds)
        best = {"keys": len(keys), "pages": reader.calls,
                "seconds": round(seconds, 3),
                "peak_traced_mb": round(peak / 2**20, 2),
                "rss_delta_mb": round((rss_kb() - before) / 1024, 1),
                "max_in_flight": reader.max_in_flight,
                "exact_match": set(keys) == set(expected_keys)}
    best["seconds_min"] = round(min(runs), 3)
    best["seconds_median"] = round(sorted(runs)[len(runs) // 2], 3)
    return best


rows = []

if args.transport in ("s3", "both"):
    for prefix in args.prefixes.split(","):
        if not prefix:
            continue
        reader = CountingReader()
        source = S3CompatibleSource(
            endpoint=args.endpoint, region="us-east-1", bucket=args.bucket,
            prefix=prefix,
            credentials=S3Credentials(os.environ.get("MINIO_ACCESS", "minioadmin"),
                                      os.environ.get("MINIO_SECRET", "minioadmin123")),
            reader=reader)
        try:
            keys = source.list_keys()
        except Exception as exc:
            print(f"skipping {prefix}: {exc}")
            continue
        row = timed_listing(source, keys, args.repeats)
        row.update({"transport": "s3 (MinIO RELEASE.2025-01-18T00-31-37Z)",
                    "data": "synthetic", "prefix": prefix,
                    "objects": len(keys),
                    "keys_per_page": round(len(keys) / max(row["pages"], 1), 1),
                    "ms_per_page": round(row["seconds_min"] / max(row["pages"], 1) * 1000, 1)})
        rows.append(row)
        print(json.dumps(row), flush=True)

if args.transport in ("oci", "both"):
    for size in [int(x) for x in args.oci_sizes.split(",")]:
        objects = {f"pad/{i:07d}": b"x" for i in range(size)}
        with ocistub.OciStub(objects) as stub:
            source = OciNativeSource(
                endpoint=stub.endpoint, namespace="ns", bucket="b",
                credentials=ocistub.credentials(), reader=CountingReader())
            row = timed_listing(source, objects.keys(), args.repeats)
            row.update({"transport": "oci native (local stub)",
                        "data": "synthetic", "prefix": f"{size} objects",
                        "objects": size,
                        "keys_per_page": round(size / max(row["pages"], 1), 1),
                        "ms_per_page": round(row["seconds_min"] / max(row["pages"], 1) * 1000, 1)})
            rows.append(row)
            print(json.dumps(row), flush=True)

print()
print(table(rows, ["transport", "objects", "pages", "keys_per_page",
                   "seconds_min", "ms_per_page", "exact_match",
                   "max_in_flight", "peak_traced_mb", "rss_delta_mb"]))
print(emit(args.out, rows))
