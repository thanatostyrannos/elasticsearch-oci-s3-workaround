#!/usr/bin/env python3
"""What a full-copy mount does when the node loses its local copy.

A fully mounted searchable snapshot keeps a complete copy on the node, so no
API call reaches the repository once it is warm. The failure the document
describes needs the shard to recover again. On a one-node rig the way to force
that without touching anything another agent owns is to close this one index
and remove this one index's own data directory, then reopen.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import esapi  # noqa: E402
import s3lib  # noqa: E402

ART = os.path.join(ROOT, "artifacts")
BUCKET = "blastrm"
EXP = "b19"
PREFIX = f"exp-{EXP}"
REPO = f"blast-{EXP}"
MOUNTED = f"bxms{EXP}"
KUBECTL = ["kubectl", "--context", "rancher-desktop", "-n", "es-rig", "exec",
           "rig-es-default-0", "-c", "elasticsearch", "--"]

steps = []


def step(name, fn):
    val = fn()
    steps.append({"step": name, "result": val})
    print(f"[{name}] {json.dumps(val)[:340]}")
    return val


def probe(label):
    def _():
        cat = esapi.jcall("GET", f"/_cat/indices/{MOUNTED}?format=json&h=index,health,status,docs.count,store.size")
        cnt = esapi.jcall("GET", f"/{MOUNTED}/_count")
        srch = esapi.jcall("GET", f"/{MOUNTED}/_search?size=1")
        return {
            "cat": cat,
            "count": cnt.get("count") if isinstance(cnt, dict) else None,
            "count_error": (cnt.get("error") or {}).get("type") if isinstance(cnt, dict) else None,
            "search_hits": (((srch.get("hits") or {}).get("total") or {}).get("value")
                            if isinstance(srch, dict) else None),
            "search_error": (srch.get("error") or {}).get("type") if isinstance(srch, dict) else None,
        }
    return step(label, _)


esapi.call("DELETE", f"/{MOUNTED}")
esapi.call("DELETE", f"/_snapshot/{REPO}")
s3lib.purge(BUCKET, PREFIX)
step("clone", lambda: s3lib.clone_prefix(BUCKET, "base-ms", PREFIX))
step("register", lambda: esapi.jcall("PUT", f"/_snapshot/{REPO}?verify=false", {
    "type": "s3", "settings": {"bucket": BUCKET, "client": "default", "base_path": PREFIX}}))
step("mount", lambda: esapi.jcall(
    "POST", f"/_snapshot/{REPO}/blast-ms-1/_mount?wait_for_completion=true&storage=full_copy",
    {"index": "blast-mount", "renamed_index": MOUNTED}, timeout=900))
time.sleep(5)
probe("after_mount")

uuid = esapi.jcall("GET", f"/{MOUNTED}/_settings")[MOUNTED]["settings"]["index"]["uuid"]
step("index_uuid", lambda: uuid)

data = [k for k, _, _ in s3lib.list_all(BUCKET, PREFIX) if "/0/__" in k]
step("delete_data_blobs", lambda: {
    "keys": [k[len(PREFIX) + 1:] for k in data],
    "http": [s3lib.delete(BUCKET, k)[0] for k in data]})
probe("after_delete")

step("close", lambda: esapi.jcall("POST", f"/{MOUNTED}/_close?wait_for_active_shards=0"))
time.sleep(5)

path = f"/usr/share/elasticsearch/data/indices/{uuid}"
step("local_copy_before", lambda: subprocess.run(
    KUBECTL + ["du", "-sh", path], capture_output=True, text=True).stdout.strip())
step("remove_local_copy", lambda: subprocess.run(
    KUBECTL + ["rm", "-rf", path], capture_output=True, text=True).returncode)
step("local_copy_after", lambda: subprocess.run(
    KUBECTL + ["ls", path], capture_output=True, text=True).stderr.strip()[:200])

step("open", lambda: esapi.jcall("POST", f"/{MOUNTED}/_open?wait_for_active_shards=0"))
for wait in (15, 30, 60):
    time.sleep(wait)
    probe(f"after_reopen_t{wait}")

step("explain", lambda: esapi.jcall(
    "GET", "/_cluster/allocation/explain?filter_path=index,shard,current_state,unassigned_info.reason,unassigned_info.details,unassigned_info.failed_allocation_attempts",
    {"index": MOUNTED, "shard": 0, "primary": True}))

with open(os.path.join(ART, f"{EXP}-mount-nodeloss.json"), "w") as fh:
    json.dump({"steps": steps}, fh, indent=2)

esapi.call("DELETE", f"/{MOUNTED}")
esapi.call("DELETE", f"/_snapshot/{REPO}")
s3lib.purge(BUCKET, PREFIX)
