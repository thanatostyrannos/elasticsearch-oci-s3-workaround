"""Generation chain depth: what a run costs as the chain gets longer.

The tool anchors at index.latest and then reads EVERY root generation blob in
the bucket, and for every shard it reads that shard's document at every
generation. A repository that leaks on a schedule accumulates one root
generation per operation and never loses one, so depth is the dimension that
grows without anybody deciding it should.

Wall clock here is local disk, which is the floor. The request count is the
number that travels: multiply it by the round trip of the store to get what an
operator will actually wait, and the parallelism column says whether that
multiplication is legitimate.
"""
from __future__ import annotations

import gc
import json
import os
import shutil
import sys
import tempfile
import time
import tracemalloc

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import base_parser, emit, rss_kb, table, use_tool

parser = base_parser(__doc__)
parser.add_argument("--generations", default="10,100,1000,5000")
parser.add_argument("--indices", type=int, default=1)
parser.add_argument("--shards", type=int, default=1)
parser.add_argument("--blobs", type=int, default=2)
parser.add_argument("--live-window", type=int, default=3)
parser.add_argument("--latency", type=float, default=0.0,
                    help="seconds of simulated round trip per store call")
parser.add_argument("--keep", action="store_true")
parser.add_argument("--out", default="depth")
args = parser.parse_args()
use_tool(args.tool_root)

import synthrepo
from instrument import InstrumentedSource
from generation_chain.sources.local import LocalMirrorSource
from generation_chain.derivation.audit import run_audit

rows = []
for depth in [int(x) for x in args.generations.split(",")]:
    root = tempfile.mkdtemp(prefix=f"genchain-depth-{depth}-")
    try:
        t0 = time.perf_counter()
        writer = synthrepo.build(generations=depth, indices=args.indices,
                                 shards=args.shards,
                                 blobs_per_shard_per_snapshot=args.blobs,
                                 live_window=args.live_window, root=root)
        build_seconds = time.perf_counter() - t0
        objects = len(writer.objects)
        source = InstrumentedSource(LocalMirrorSource(root),
                                    latency=args.latency)
        gc.collect()
        before = rss_kb()
        tracemalloc.start()
        t0 = time.perf_counter()
        result = run_audit(source)
        seconds = time.perf_counter() - t0
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        after = rss_kb()
        counters = source.counters
        coverage = result.coverage
        rows.append({
            "generations": depth,
            "objects": objects,
            "seconds": round(seconds, 3),
            "requests": counters.requests,
            "fetches": counters.fetch_calls,
            "exists": counters.exists_calls,
            "list_pages": counters.list_pages,
            "req_per_gen": round(counters.requests / depth, 2),
            "us_per_request": round(seconds / max(counters.requests, 1) * 1e6, 1),
            "max_in_flight": counters.max_in_flight,
            "condemned": len(result.condemned),
            "explained": coverage.explained_fraction,
            "refused": coverage.refused,
            "peak_traced_mb": round(peak / 2**20, 2),
            "rss_delta_mb": round((after - before) / 1024, 1),
            "build_seconds": round(build_seconds, 2),
        })
        print(json.dumps(rows[-1]))
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)

print()
print(table(rows, ["generations", "objects", "requests", "req_per_gen",
                   "seconds", "us_per_request", "max_in_flight", "condemned",
                   "explained", "peak_traced_mb"]))
print(emit(args.out, rows))
