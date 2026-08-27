"""Resident memory against object count.

The tool holds the whole listing, indexes it twice, and then builds one
Placement record per key with a sentence of explanation attached. None of that
streams, so the ceiling is set by the widest repository rather than by the
longest one. This measures the real resident set of the process, which is what
a jump host's ulimit and the OOM killer both look at.
"""
from __future__ import annotations

import gc, json, os, resource, shutil, sys, tempfile, time, tracemalloc

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import base_parser, emit, rss_kb, table, use_tool

parser = base_parser(__doc__)
parser.add_argument("--shapes", default="10x20x20,20x50x20,50x50x20,50x50x45",
                    help="indices x shards x generations")
parser.add_argument("--blobs", type=int, default=2)
parser.add_argument("--out", default="memory")
args = parser.parse_args()
use_tool(args.tool_root)

import synthrepo
from instrument import InstrumentedSource
from generation_chain.sources.local import LocalMirrorSource
from generation_chain.derivation.audit import run_audit
from generation_chain.derivation.attribution import KeyIndex

rows = []
for shape in args.shapes.split(","):
    indices, shards, generations = (int(x) for x in shape.lower().split("x"))
    root = tempfile.mkdtemp(prefix=f"genchain-mem-{shape}-")
    try:
        writer = synthrepo.build(generations=generations, indices=indices,
                                 shards=shards,
                                 blobs_per_shard_per_snapshot=args.blobs,
                                 live_window=3, root=root)
        objects = len(writer.objects)
        del writer
        gc.collect()
        mirror = LocalMirrorSource(root)

        base = rss_kb()
        keys = mirror.list_keys()
        after_listing = rss_kb()
        index = KeyIndex(keys, mirror)
        after_index = rss_kb()
        del index, keys
        gc.collect()

        source = InstrumentedSource(mirror)
        gc.collect()
        before = rss_kb()
        tracemalloc.start()
        t0 = time.perf_counter()
        result = run_audit(source)
        seconds = time.perf_counter() - t0
        _, traced_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        end = rss_kb()
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rows.append({
            "shape": shape, "objects": objects,
            "shard_dirs": indices * shards, "generations": generations,
            "seconds": round(seconds, 1),
            "listing_only_mb": round((after_listing - base) / 1024, 1),
            "listing_plus_keyindex_mb": round((after_index - base) / 1024, 1),
            "audit_rss_growth_mb": round((end - before) / 1024, 1),
            "traced_peak_mb": round(traced_peak / 2**20, 1),
            "process_peak_rss_mb": round(peak / 1024, 1),
            "bytes_per_object_audit": round((end - before) * 1024 / objects),
            "bytes_per_object_peak": round(peak * 1024 / objects),
            "condemned": len(result.condemned),
            "placements": len(result.classification),
        })
        print(json.dumps(rows[-1]), flush=True)
        del result, source
        gc.collect()
    finally:
        shutil.rmtree(root, ignore_errors=True)

print()
print(table(rows, ["shape", "objects", "seconds", "listing_only_mb",
                   "listing_plus_keyindex_mb", "audit_rss_growth_mb",
                   "traced_peak_mb", "process_peak_rss_mb",
                   "bytes_per_object_audit"]))
print(emit(args.out, rows))
