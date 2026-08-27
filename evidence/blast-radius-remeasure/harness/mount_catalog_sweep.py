#!/usr/bin/env python3
"""Claim under test, sharpest version: the snapshot backing a mounted index is
removed from the catalog, and then a sweep removes the blobs the catalog no
longer explains.

MinIO rejects the batch delete Elasticsearch uses, so the catalog empties and
the bucket keeps everything. That is the fault this whole project exists for,
and it is also how an operator reaches this state by accident.
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
EXP = "b17"
PREFIX = f"exp-{EXP}"
REPO = f"blast-{EXP}"
MOUNTED = f"bxms{EXP}"
NODE = "rig-es-default-0"

steps = []


def step(name, fn):
    val = fn()
    steps.append({"step": name, "result": val})
    print(f"[{name}] {json.dumps(val)[:320]}")
    return val


def probe(label):
    def _():
        cat = esapi.jcall("GET", f"/_cat/indices/{MOUNTED}?format=json&h=index,health,status,docs.count,store.size")
        cnt = esapi.jcall("GET", f"/{MOUNTED}/_count")
        srch = esapi.jcall("GET", f"/{MOUNTED}/_search?size=1")
        listing = s3lib.list_all(BUCKET, PREFIX)
        return {
            "cat": cat,
            "count": cnt.get("count") if isinstance(cnt, dict) else None,
            "count_error": (cnt.get("error") or {}).get("type") if isinstance(cnt, dict) else None,
            "search_hits": (((srch.get("hits") or {}).get("total") or {}).get("value")
                            if isinstance(srch, dict) else None),
            "search_error": (srch.get("error") or {}).get("type") if isinstance(srch, dict) else None,
            "objects_in_bucket": len(listing),
        }
    return step(label, _)


esapi.call("DELETE", f"/{MOUNTED}")
esapi.call("DELETE", f"/_snapshot/{REPO}")
s3lib.purge(BUCKET, PREFIX)
step("clone", lambda: s3lib.clone_prefix(BUCKET, "base-ms", PREFIX))
step("register", lambda: esapi.jcall("PUT", f"/_snapshot/{REPO}?verify=false", {
    "type": "s3", "settings": {"bucket": BUCKET, "client": "default", "base_path": PREFIX}}))
step("mount", lambda: esapi.jcall(
    "POST", f"/_snapshot/{REPO}/blast-ms-1/_mount?wait_for_completion=true&storage=shared_cache",
    {"index": "blast-mount", "renamed_index": MOUNTED}, timeout=900))
time.sleep(5)
probe("after_mount")

step("delete_snapshot_from_catalog",
     lambda: esapi.jcall("DELETE", f"/_snapshot/{REPO}/blast-ms-1", timeout=900))
time.sleep(5)
probe("after_catalog_delete")
step("catalog_now", lambda: esapi.jcall("GET", f"/_snapshot/{REPO}/_all"))
step("verify_integrity_after_catalog_delete",
     lambda: esapi.jcall("POST", f"/_snapshot/{REPO}/_verify_integrity", timeout=900).get("results"))

# Now the sweep: the blobs the emptied catalog no longer explains are removed
# from the bucket, which is exactly what a sweeper would classify as orphaned.
data = [k for k, _, _ in s3lib.list_all(BUCKET, PREFIX) if "/0/__" in k]
step("delete_orphaned_data_blobs", lambda: {
    "keys": [k[len(PREFIX) + 1:] for k in data],
    "http": [s3lib.delete(BUCKET, k)[0] for k in data],
})
probe("after_blob_sweep")

step("clear_shared_cache", lambda: esapi.jcall("POST", "/_searchable_snapshots/cache/clear"))
time.sleep(3)
probe("after_cache_clear")

step("close", lambda: esapi.jcall("POST", f"/{MOUNTED}/_close?wait_for_active_shards=0"))
time.sleep(3)
step("open", lambda: esapi.jcall("POST", f"/{MOUNTED}/_open?wait_for_active_shards=0"))
time.sleep(15)
probe("after_close_reopen")

# A forced reallocation is what a node restart does days later. On one node the
# way to force it is to exclude the node the shard sits on and then let it back.
step("exclude_node", lambda: esapi.jcall("PUT", f"/{MOUNTED}/_settings",
     {"index.routing.allocation.exclude._name": NODE}))
time.sleep(20)
probe("after_exclude")
step("unexclude_node", lambda: esapi.jcall("PUT", f"/{MOUNTED}/_settings",
     {"index.routing.allocation.exclude._name": None}))
time.sleep(25)
probe("after_unexclude")
step("explain", lambda: esapi.jcall(
    "GET", "/_cluster/allocation/explain?filter_path=index,shard,current_state,unassigned_info,node_allocation_decisions.deciders",
    {"index": MOUNTED, "shard": 0, "primary": True}))

with open(os.path.join(ART, f"{EXP}-mount-catalog.json"), "w") as fh:
    json.dump({"steps": steps}, fh, indent=2)

esapi.call("DELETE", f"/{MOUNTED}")
esapi.call("DELETE", f"/_snapshot/{REPO}")
s3lib.purge(BUCKET, PREFIX)
