#!/usr/bin/env python3
"""The object class against measured damage table, straight out of the summaries."""
import json
import os
import sys

ART = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "artifacts")
ORDER = ["b0", "b1", "b2", "b3", "b4", "b5", "b6r", "b7", "b8r", "b9r", "b10", "b11", "b12"]
print("\t".join(["exp", "objs_deleted", "bytes_deleted", "pct_bytes", "pct_objects",
                 "total_anomalies", "result", "anomaly_class", "damaged_pairs",
                 "restores", "indices_with_no_docs", "artifact"]))
for e in ORDER:
    p = os.path.join(ART, f"{e}-summary.json")
    if not os.path.exists(p):
        continue
    d = json.load(open(p))
    rs = d["restores"]
    ok = sum(1 for r in rs if r["shards"] and r["shards"].get("failed") == 0)
    part = sum(1 for r in rs if r["shards"] and r["shards"].get("failed", 0) > 0)
    err = sum(1 for r in rs if not r["shards"])
    lost = sum(1 for r in rs for v in r["docs"].values() if v is None)
    print("\t".join([e, str(d["deleted_objects"]), str(d["deleted_bytes"]),
                     str(d["share_of_bytes_pct"]), str(d["share_of_objects_pct"]),
                     str(d["total_anomalies"]), d["result"] or "-",
                     ",".join(d["anomaly_classes"]) or "-", str(d["damaged_pair_count"]),
                     f"{ok} clean / {part} partial / {err} refused", str(lost),
                     f"{e}-summary.json"]))
