import os, sys, tempfile
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import use_tool
use_tool()
import synthrepo
from instrument import InstrumentedSource
from generation_chain.sources.local import LocalMirrorSource
from generation_chain.derivation.audit import run_audit

root = tempfile.mkdtemp(prefix="genchain-smoke-")
w = synthrepo.build(generations=8, indices=2, shards=2,
                    blobs_per_shard_per_snapshot=2, live_window=3, root=root)
print("objects:", len(w.objects), "snapshots:", w.snapshots)
print("history:", w.history)
src = InstrumentedSource(LocalMirrorSource(root))
r = run_audit(src)
c = r.coverage
print("refused:", c.refused)
print("gens usable:", len(c.generations_usable), "missing:", c.generations_missing)
print("transitions:", c.transitions_explained, "/", c.transitions_total, "mixed:", c.transitions_mixed)
print("ops:", c.operations_attributed, "/", c.operations_found)
print("shards:", c.shards_considered, "dropped:", c.shards_dropped)
print("condemned:", len(r.condemned))
print("counters:", src.counters)
from collections import Counter
print(Counter(p.disposition for p in r.classification))
for n in c.notes[:5]: print("note:", n)
