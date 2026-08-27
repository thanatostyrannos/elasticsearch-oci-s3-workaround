#!/usr/bin/env python3
"""What the next snapshot costs after a given object is removed.

Usage: next_snapshot.py <exp-id> <relative key> <note>

Deleting a shard's current index-<gen> leaves every restore working, so the
question is what it costs the repository afterwards rather than what it breaks
now. This measures that: the objects the next snapshot writes, and whether the
snapshot it produces restores.
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
EXP, VICTIM, NOTE = sys.argv[1], sys.argv[2], sys.argv[3]
REREG = "--reregister" in sys.argv
PREFIX, REPO = f"exp-{EXP}", f"blast-{EXP}"

esapi.call("DELETE", f"/_snapshot/{REPO}")
s3lib.purge(BUCKET, PREFIX)
s3lib.clone_prefix(BUCKET, "base-s", PREFIX)
esapi.jcall("PUT", f"/_snapshot/{REPO}?verify=false", {
    "type": "s3", "settings": {"bucket": BUCKET, "client": "default", "base_path": PREFIX}})

before = s3lib.list_all(BUCKET, PREFIX)
sizes = {k[len(PREFIX) + 1:]: s for k, s, _ in before}
st, _ = s3lib.delete(BUCKET, f"{PREFIX}/{VICTIM}")

if REREG:
    # Drop any state the node is holding about this repository, so the next
    # snapshot has to read the shard file list from the store.
    esapi.call("DELETE", f"/_snapshot/{REPO}")
    time.sleep(2)
    esapi.jcall("PUT", f"/_snapshot/{REPO}?verify=false", {
        "type": "s3", "settings": {"bucket": BUCKET, "client": "default", "base_path": PREFIX}})
    time.sleep(2)

snap = esapi.jcall("PUT", f"/_snapshot/{REPO}/blast-snap-4?wait_for_completion=true",
                   {"indices": "blast-share1,blast-share2", "include_global_state": False}, timeout=900)
time.sleep(2)
after = s3lib.list_all(BUCKET, PREFIX)
new = sorted(set(k for k, _, _ in after) - set(k for k, _, _ in before))
newsz = {k: s for k, s, _ in after}

tag = f"bxr{EXP}"
r = esapi.jcall("POST", f"/_snapshot/{REPO}/blast-snap-4/_restore?wait_for_completion=true",
                {"indices": "*", "include_global_state": False,
                 "rename_pattern": "blast-(.+)", "rename_replacement": tag + "-$1"}, timeout=900)
time.sleep(3)
cat = esapi.jcall("GET", f"/_cat/indices/{tag}-*?format=json&h=index,health,docs.count")
counts = {c["index"]: esapi.jcall("GET", f"/{c['index']}/_count").get("count")
          for c in (cat if isinstance(cat, list) else [])}
names = [c["index"] for c in (cat if isinstance(cat, list) else [])]
if names:
    esapi.call("DELETE", "/" + ",".join(names))

out = {
    "id": EXP, "note": NOTE, "reregistered": REREG, "victim": VICTIM, "victim_bytes": sizes.get(VICTIM),
    "delete_http_status": st,
    "new_snapshot_state": (snap.get("snapshot") or {}).get("state"),
    "new_snapshot_shards": (snap.get("snapshot") or {}).get("shards"),
    "objects_written_by_next_snapshot": len(new),
    "bytes_written_by_next_snapshot": sum(newsz[k] for k in new),
    "data_blobs_written_by_next_snapshot": [k[len(PREFIX) + 1:] for k in new if "/0/__" in k],
    "verify_after": esapi.jcall("POST", f"/_snapshot/{REPO}/_verify_integrity", timeout=900)["results"],
    "restore_shards": (r.get("snapshot") or {}).get("shards"),
    "restored": {c["index"]: c["health"] for c in (cat if isinstance(cat, list) else [])},
    "restored_docs": counts,
}
with open(os.path.join(ART, f"{EXP}-next-snapshot.json"), "w") as fh:
    json.dump(out, fh, indent=2)
print(json.dumps(out, indent=2))
esapi.call("DELETE", f"/_snapshot/{REPO}")
s3lib.purge(BUCKET, PREFIX)
