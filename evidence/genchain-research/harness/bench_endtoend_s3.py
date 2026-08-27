"""A whole audit over the S3 compatibility API against a real MinIO.

Everything else in this harness reads a mirror on local disk and counts the
round trips a store would have seen. This one actually makes them, so the
per-request cost is a measured network cost rather than a multiplication, and
the manifest it produces can be compared key for key with the one the local
mirror produces from the same bytes.
"""
from __future__ import annotations

import gc, json, os, sys, time, tracemalloc

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import base_parser, emit, rss_kb, table, use_tool

parser = base_parser(__doc__)
parser.add_argument("--bucket", default=os.environ.get("MINIO_BUCKET", "scale-limits"))
parser.add_argument("--endpoint", default=os.environ.get("MINIO_ENDPOINT", "http://127.0.0.1:19000"))
parser.add_argument("--prefixes", default="r00500,r02700,r10600,r53000")
parser.add_argument("--out", default="endtoend-s3")
args = parser.parse_args()
use_tool(args.tool_root)

from generation_chain.sources.s3 import S3CompatibleSource, S3Credentials
from generation_chain.sources.http_reads import HttpReader
from generation_chain.derivation.audit import run_audit


class CountingReader(HttpReader):
    def __init__(self):
        super().__init__(sleep=lambda _s: None, jitter=lambda: 0.0)
        self.calls = 0
        self.gets = 0
        self.heads = 0
        self.max_in_flight = 0
        self._in_flight = 0

    def get(self, url, headers, method="GET", timeout=60.0):
        self.calls += 1
        if method == "HEAD":
            self.heads += 1
        else:
            self.gets += 1
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            return super().get(url, headers, method=method, timeout=timeout)
        finally:
            self._in_flight -= 1


rows = []
for prefix in args.prefixes.split(","):
    reader = CountingReader()
    source = S3CompatibleSource(
        endpoint=args.endpoint, region="us-east-1", bucket=args.bucket,
        prefix=prefix,
        credentials=S3Credentials(os.environ.get("MINIO_ACCESS", "minioadmin"),
                                  os.environ.get("MINIO_SECRET", "minioadmin123")),
        reader=reader)
    gc.collect()
    before = rss_kb()
    tracemalloc.start()
    t0 = time.perf_counter()
    result = run_audit(source)
    seconds = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    coverage = result.coverage
    rows.append({
        "prefix": prefix, "transport": "s3 (real MinIO over kubectl port-forward)",
        "data": "synthetic repository, real endpoint",
        "seconds": round(seconds, 1),
        "http_requests": reader.calls, "gets": reader.gets, "heads": reader.heads,
        "ms_per_request": round(seconds / max(reader.calls, 1) * 1000, 2),
        "max_in_flight": reader.max_in_flight,
        "condemned": len(result.condemned),
        "explained": coverage.explained_fraction,
        "shards_considered": coverage.shards_considered,
        "shards_dropped": len(coverage.shards_dropped),
        "refused": coverage.refused,
        "rss_growth_mb": round((rss_kb() - before) / 1024, 1),
        "traced_peak_mb": round(peak / 2**20, 1),
    })
    print(json.dumps(rows[-1]), flush=True)

print()
print(table(rows, ["prefix", "http_requests", "gets", "heads", "seconds",
                   "ms_per_request", "max_in_flight", "condemned",
                   "explained", "traced_peak_mb"]))
print(emit(args.out, rows))
