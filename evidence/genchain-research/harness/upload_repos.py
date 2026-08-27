"""Build synthetic repositories and put them into MinIO, one prefix each."""
from __future__ import annotations
import json, os, sys, time
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import use_tool, emit
use_tool()
import synthrepo, miniofill

BUCKET = os.environ.get("MINIO_BUCKET", "scale-limits")
# name: (generations, indices, shards)
SHAPES = {
    "r00500": (20, 2, 5),
    "r02700": (20, 5, 10),
    "r10600": (20, 10, 20),
    "r53000": (20, 20, 50),
    "r132000": (20, 50, 50),
}
if len(sys.argv) > 1:
    SHAPES = {k: v for k, v in SHAPES.items() if k in sys.argv[1:]}

miniofill.make_bucket(BUCKET)
rows = []
for name, (generations, indices, shards) in SHAPES.items():
    writer = synthrepo.build(generations=generations, indices=indices,
                             shards=shards, blobs_per_shard_per_snapshot=2,
                             live_window=3)
    prefixed = {f"{name}/{k}": v for k, v in writer.objects.items()}
    seconds = miniofill.put_many(BUCKET, prefixed)
    rows.append({"prefix": name, "generations": generations,
                 "indices": indices, "shards": shards,
                 "objects": len(prefixed), "upload_seconds": round(seconds, 1),
                 "objects_per_second": round(len(prefixed) / seconds)})
    print(json.dumps(rows[-1]), flush=True)
print(emit("minio-uploads", rows))
