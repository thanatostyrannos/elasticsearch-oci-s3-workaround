"""Does the synthetic repository cost the tool the same shape as a real one?

The point is not that the two repositories are identical. It is that the
per-generation and per-shard REQUEST COUNTS the tool makes on the synthetic
one match what it makes on the captured 9.5.2 repository, so a curve measured
on synthetic data is a curve about the tool rather than about the generator.
"""
import os, sys, json, tempfile, shutil
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import use_tool, SCRATCH, emit
use_tool()
import synthrepo
from instrument import InstrumentedSource
from generation_chain.sources.local import LocalMirrorSource
from generation_chain.derivation.audit import run_audit


def measure(root):
    src = InstrumentedSource(LocalMirrorSource(root))
    r = run_audit(src)
    c = r.coverage
    return {
        "objects": len(LocalMirrorSource(root).list_keys()),
        "generations": (c.current_generation or 0) + 1,
        "shards": c.shards_considered,
        "shards_dropped": len(c.shards_dropped),
        "fetches": src.counters.fetch_calls,
        "exists": src.counters.exists_calls,
        "condemned": len(r.condemned),
        "explained": c.explained_fraction,
        "refused": c.refused,
    }


real = measure(os.path.join(SCRATCH, "real"))
tmp = tempfile.mkdtemp(prefix="genchain-validate-")
try:
    synthrepo.build(generations=3, indices=3, shards=1,
                    blobs_per_shard_per_snapshot=2, live_window=2, root=tmp)
    synth = measure(tmp)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# The predicted model: one index.latest, one root generation blob each, and
# one shard document per (shard directory, generation).
def predicted(row):
    return 1 + row["generations"] + row["shards"] * row["generations"]

rows = []
for name, row in (("real es9.5.2 capture", real), ("synthetic G=3 I=3 S=1", synth)):
    rows.append({"repository": name, "objects": row["objects"],
                 "generations": row["generations"], "shards": row["shards"],
                 "fetches": row["fetches"],
                 "fetches_predicted": predicted(row),
                 "exists": row["exists"], "condemned": row["condemned"],
                 "explained": row["explained"]})
print(json.dumps(rows, indent=1))
print(emit("validate-generator", rows))
