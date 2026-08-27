"""Index and shard breadth: what many shards cost at a fixed chain depth.

Every (index, shard) directory the chain names gets its CURRENT document read
plus one document per generation, and the tool reads them one at a time. This
sweep holds the generation count still and widens the repository, so the shard
term separates from the depth term.
"""
from __future__ import annotations

import gc, json, os, shutil, sys, tempfile, time, tracemalloc

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import base_parser, emit, rss_kb, table, use_tool

parser = base_parser(__doc__)
parser.add_argument("--generations", type=int, default=20)
parser.add_argument("--shapes", default="1x1,2x5,5x10,10x20,20x50",
                    help="indices x shards, comma separated")
parser.add_argument("--blobs", type=int, default=2)
parser.add_argument("--live-window", type=int, default=3)
parser.add_argument("--out", default="breadth")
args = parser.parse_args()
use_tool(args.tool_root)

import synthrepo
from instrument import InstrumentedSource
from generation_chain.sources.local import LocalMirrorSource
from generation_chain.derivation.audit import run_audit

rows = []
for shape in args.shapes.split(","):
    indices, shards = (int(x) for x in shape.lower().split("x"))
    root = tempfile.mkdtemp(prefix=f"genchain-breadth-{shape}-")
    try:
        writer = synthrepo.build(generations=args.generations, indices=indices,
                                 shards=shards,
                                 blobs_per_shard_per_snapshot=args.blobs,
                                 live_window=args.live_window, root=root)
        source = InstrumentedSource(LocalMirrorSource(root))
        gc.collect(); before = rss_kb(); tracemalloc.start()
        t0 = time.perf_counter()
        result = run_audit(source)
        seconds = time.perf_counter() - t0
        _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
        after = rss_kb()
        c = source.counters
        rows.append({
            "shape": shape, "indices": indices, "shards_per_index": shards,
            "shard_dirs": indices * shards,
            "generations": args.generations,
            "objects": len(writer.objects),
            "seconds": round(seconds, 3),
            "requests": c.requests, "fetches": c.fetch_calls,
            "exists": c.exists_calls,
            "req_per_shard_dir": round(c.requests / (indices * shards), 1),
            "fetch_per_shard_per_gen": round(
                c.fetch_calls / (indices * shards * args.generations), 3),
            "max_in_flight": c.max_in_flight,
            "condemned": len(result.condemned),
            "shards_dropped": len(result.coverage.shards_dropped),
            "explained": result.coverage.explained_fraction,
            "peak_traced_mb": round(peak / 2**20, 2),
            "rss_delta_mb": round((after - before) / 1024, 1),
        })
        print(json.dumps(rows[-1]))
    finally:
        shutil.rmtree(root, ignore_errors=True)

print()
print(table(rows, ["shape", "shard_dirs", "objects", "requests",
                   "req_per_shard_dir", "fetch_per_shard_per_gen", "seconds",
                   "max_in_flight", "condemned", "explained"]))
print(emit(args.out, rows))
