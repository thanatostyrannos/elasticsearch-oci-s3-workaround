#!/usr/bin/env python3
"""Claim under test: a snapshot taken after a bad delete reports SUCCESS and
cannot be restored.

Clone a healthy repository, remove one data blob that a live index still
references, then take a fresh snapshot of that same live index into the damaged
repository and try to restore it.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import esapi  # noqa: E402
import s3lib  # noqa: E402

ART = os.path.join(ROOT, "artifacts")
BUCKET = "blastrm"
EXP = "b13"
PREFIX = f"exp-{EXP}"
REPO = f"blast-{EXP}"
VICTIM = "indices/UXdt_l7dRxGjcj2NMCWFbw/0/__6HfSS9VGSUy_86kMSv-_Kw"

out = {}
esapi.call("DELETE", f"/_snapshot/{REPO}")
s3lib.purge(BUCKET, PREFIX)
out["cloned_objects"] = s3lib.clone_prefix(BUCKET, "base-s", PREFIX)
esapi.jcall("PUT", f"/_snapshot/{REPO}?verify=false", {
    "type": "s3",
    "settings": {"bucket": BUCKET, "client": "default", "base_path": PREFIX},
})

before = s3lib.list_all(BUCKET, PREFIX)
out["objects_before"] = len(before)
out["bytes_before"] = sum(s for _, s, _ in before)
sizes = {k[len(PREFIX) + 1:]: s for k, s, _ in before}
out["victim"] = VICTIM
out["victim_bytes"] = sizes[VICTIM]

out["verify_before_delete"] = esapi.jcall("POST", f"/_snapshot/{REPO}/_verify_integrity", timeout=900)["results"]
st, _ = s3lib.delete(BUCKET, f"{PREFIX}/{VICTIM}")
out["delete_http_status"] = st

# The live index has not changed since the snapshot that uploaded that blob, so
# deduplication has every reason to reference it again.
snap = esapi.jcall(
    "PUT", f"/_snapshot/{REPO}/blast-snap-4?wait_for_completion=true",
    {"indices": "blast-share1,blast-share2", "include_global_state": False}, timeout=900)
out["new_snapshot_response"] = snap
time.sleep(2)
out["new_snapshot_get"] = esapi.jcall("GET", f"/_snapshot/{REPO}/blast-snap-4")
out["new_snapshot_status"] = esapi.jcall("GET", f"/_snapshot/{REPO}/blast-snap-4/_status")

after = s3lib.list_all(BUCKET, PREFIX)
out["objects_after"] = len(after)
out["new_objects"] = sorted(set(k for k, _, _ in after) - set(k for k, _, _ in before))
out["victim_still_absent"] = f"{PREFIX}/{VICTIM}" not in set(k for k, _, _ in after)

out["verify_after"] = esapi.jcall("POST", f"/_snapshot/{REPO}/_verify_integrity", timeout=900)

tag = "bxrb13"
r = esapi.jcall(
    "POST", f"/_snapshot/{REPO}/blast-snap-4/_restore?wait_for_completion=true",
    {"indices": "*", "include_global_state": False,
     "rename_pattern": "blast-(.+)", "rename_replacement": tag + "-$1"}, timeout=900)
out["restore_response"] = r
time.sleep(3)
cat = esapi.jcall("GET", f"/_cat/indices/{tag}-*?format=json&h=index,health,status,docs.count")
out["restored_cat"] = cat
out["restored_counts"] = {
    row["index"]: esapi.jcall("GET", f"/{row['index']}/_count")
    for row in (cat if isinstance(cat, list) else [])
}
names = [c["index"] for c in (cat if isinstance(cat, list) else [])]
if names:
    esapi.call("DELETE", "/" + ",".join(names))

with open(os.path.join(ART, f"{EXP}-forward.json"), "w") as fh:
    json.dump(out, fh, indent=2)

res = out["verify_after"]["results"]
print(json.dumps({
    "cloned_objects": out["cloned_objects"],
    "victim_bytes": out["victim_bytes"],
    "delete_http_status": out["delete_http_status"],
    "new_snapshot_state": (snap.get("snapshot") or {}).get("state"),
    "new_snapshot_shards": (snap.get("snapshot") or {}).get("shards"),
    "new_snapshot_failures": (snap.get("snapshot") or {}).get("failures"),
    "new_objects_written": len(out["new_objects"]),
    "victim_still_absent": out["victim_still_absent"],
    "status_reported_size": {
        k: v.get("stats", {}).get("total", {})
        for k, v in ((out["new_snapshot_status"].get("snapshots") or [{}])[0].get("indices") or {}).items()
    },
    "verify_total_anomalies": res["total_anomalies"],
    "verify_result": res["result"],
    "restore_shards": (r.get("snapshot") or {}).get("shards"),
    "restored_health": {c["index"]: c["health"] for c in (cat if isinstance(cat, list) else [])},
    "restored_docs": {k: (v.get("count") if isinstance(v, dict) else None) for k, v in out["restored_counts"].items()},
}, indent=2))

esapi.call("DELETE", f"/_snapshot/{REPO}")
s3lib.purge(BUCKET, PREFIX)
