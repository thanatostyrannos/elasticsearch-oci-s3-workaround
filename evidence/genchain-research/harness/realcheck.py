"""Run the tool against the captured real repository, for a shape comparison."""
import os, sys, json, collections
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import use_tool, SCRATCH
use_tool()
from instrument import InstrumentedSource
from generation_chain.sources.local import LocalMirrorSource
from generation_chain.derivation.audit import run_audit

root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(SCRATCH, "real")
src = InstrumentedSource(LocalMirrorSource(root))
keys = LocalMirrorSource(root).list_keys()
r = run_audit(src)
c = r.coverage
out = {
 "objects": len(keys),
 "refused": c.refused,
 "repository_uuid": c.repository_uuid,
 "current_generation": c.current_generation,
 "generations_present": list(c.generations_present),
 "generations_usable": list(c.generations_usable),
 "generations_missing": list(c.generations_missing),
 "transitions": [c.transitions_explained, c.transitions_total],
 "transitions_mixed": c.transitions_mixed,
 "operations": [c.operations_attributed, c.operations_found],
 "shards_considered": c.shards_considered,
 "shards_dropped": c.shards_dropped,
 "condemned": len(r.condemned),
 "dispositions": dict(collections.Counter(p.disposition for p in r.classification)),
 "counters": vars(src.counters),
 "notes": c.notes,
}
print(json.dumps(out, indent=1))
