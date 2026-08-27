#!/usr/bin/env python3
"""Claim under test: a mounted searchable snapshot hides the damage.

Mount an index straight out of a repository, delete the blobs behind it, then
ask at each step what an operator would actually see: the search, the health,
a shared-cache clear, a close and reopen, and a forced reallocation.
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
MODE = sys.argv[1] if len(sys.argv) > 1 else "full_copy"
EXP = "b14" if MODE == "full_copy" else "b15"
PREFIX = f"exp-{EXP}"
REPO = f"blast-{EXP}"
MOUNTED = f"bxms{EXP}"

steps = []


def step(name, fn):
    t0 = time.time()
    val = fn()
    steps.append({"step": name, "elapsed_s": round(time.time() - t0, 1), "result": val})
    short = json.dumps(val)
    print(f"[{name}] {short[:300]}")
    return val


def probe(label):
    def _():
        cat = esapi.jcall("GET", f"/_cat/indices/{MOUNTED}?format=json&h=index,health,status,docs.count")
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
step("clone_base_ms", lambda: s3lib.clone_prefix(BUCKET, "base-ms", PREFIX))
step("register", lambda: esapi.jcall("PUT", f"/_snapshot/{REPO}?verify=false", {
    "type": "s3", "settings": {"bucket": BUCKET, "client": "default", "base_path": PREFIX}}))

before = s3lib.list_all(BUCKET, PREFIX)
step("listing_before", lambda: {"objects": len(before), "bytes": sum(s for _, s, _ in before)})

step("mount", lambda: esapi.jcall(
    "POST", f"/_snapshot/{REPO}/blast-ms-1/_mount?wait_for_completion=true&storage={MODE}",
    {"index": "blast-mount", "renamed_index": MOUNTED}, timeout=900))
time.sleep(5)
probe("after_mount")

data = [k for k, _, _ in before if "/0/__" in k]
step("delete_data_blobs", lambda: {
    "keys": [k[len(PREFIX) + 1:] for k in data],
    "bytes": sum(s for k, s, _ in before if k in data),
    "http": [s3lib.delete(BUCKET, k)[0] for k in data],
})
probe("after_delete")

step("clear_shared_cache", lambda: esapi.jcall("POST", "/_searchable_snapshots/cache/clear"))
time.sleep(3)
probe("after_cache_clear")

step("verify_integrity", lambda: (esapi.jcall(
    "POST", f"/_snapshot/{REPO}/_verify_integrity", timeout=900).get("results")))

step("close", lambda: esapi.jcall("POST", f"/{MOUNTED}/_close?wait_for_active_shards=0"))
time.sleep(3)
step("open", lambda: esapi.jcall("POST", f"/{MOUNTED}/_open?wait_for_active_shards=0"))
time.sleep(10)
probe("after_close_reopen")

step("reroute_retry_failed", lambda: esapi.jcall("POST", "/_cluster/reroute?retry_failed=true&metric=none"))
time.sleep(10)
probe("after_reroute")

step("explain_allocation", lambda: esapi.jcall(
    "GET", "/_cluster/allocation/explain?filter_path=index,shard,current_state,unassigned_info.reason,unassigned_info.details",
    {"index": MOUNTED, "shard": 0, "primary": True}))

with open(os.path.join(ART, f"{EXP}-mount.json"), "w") as fh:
    json.dump({"mode": MODE, "steps": steps}, fh, indent=2)

esapi.call("DELETE", f"/{MOUNTED}")
esapi.call("DELETE", f"/_snapshot/{REPO}")
s3lib.purge(BUCKET, PREFIX)
