#!/usr/bin/env python3
"""One-screen view of an experiment summary, for the campaign log."""
import json
import sys

d = json.load(open(sys.argv[1]))
print(f"== {d['id']}: {d['note']}")
print(f"   before {d['objects_before']} objects / {d['bytes_before']} bytes")
print(f"   deleted {d['deleted_objects']} objects / {d['deleted_bytes']} bytes "
      f"= {d['share_of_objects_pct']}% of objects, {d['share_of_bytes_pct']}% of bytes")
st = d.get('verify_status') or {}
print(f"   verify: total_anomalies={d['total_anomalies']} result={d['result']} "
      f"classes={d['anomaly_classes']}")
print(f"   verify status counters: {json.dumps(st)}")
print(f"   anomalies per snapshot: {json.dumps(d['anomalies_per_snapshot'])}")
print(f"   damaged pairs ({d['damaged_pair_count']}): {d['damaged_index_snapshot_pairs']}")
print(f"   catalog states: {json.dumps(d['catalog_states'])}")
for r in d['restores']:
    print(f"   restore {r['snapshot']}: shards={json.dumps(r['shards'])} "
          f"health={json.dumps(r['health'])} docs={json.dumps(r['docs'])}")
