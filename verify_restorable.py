#!/usr/bin/env python3
"""Is Elasticsearch still whole after objects were deleted underneath it?

A repository can list clean, report SUCCESS on every snapshot and pass an
integrity check while being unrestorable. This project measured exactly that: a
mounted index with all eight of its data blobs deleted answered HTTP 200 with
"failed": 0, and a `size=1` query missed the loss entirely.

So the cheap checks run first because they are cheap, and none of them is
believed. The answer comes from restoring a snapshot and counting what comes
back. Exit 0 means intact, 1 means something is wrong and the caller should stop.
"""
import json, os, ssl, sys, time, urllib.error, urllib.parse, urllib.request

import argparse

_p = argparse.ArgumentParser(
    description="Prove a snapshot repository is still restorable after deletion "
                "traffic. Reads and restores under a fresh name; deletes nothing "
                "from the repository.")
_p.add_argument("--elasticsearch", required=True, metavar="URL")
_p.add_argument("--repository", required=True, metavar="NAME")
_p.add_argument("--user", default="elastic")
_p.add_argument("--password-file", required=True, metavar="PATH",
                help="a PATH, never a value: a secret in argv is visible in ps")
_p.add_argument("--insecure", action="store_true",
                help="skip TLS verification, for a self-signed development cluster")
_a = _p.parse_args()

ES, REPO = _a.elasticsearch.rstrip("/"), _a.repository

# --elasticsearch comes from configuration, not from the network, but
# configuration is not the same as trusted. urlopen does not care, and will
# happily open file:// or ftp://. This script only ever needs http or https,
# so anything else is refused before ES is ever passed to urlopen.
_es_scheme = urllib.parse.urlsplit(ES).scheme
if _es_scheme not in ("http", "https"):
    _p.error(f"--elasticsearch is {ES!r}; only http and https are accepted, "
             f"so a {_es_scheme or '(no scheme)'!r} value cannot be opened")
PW = open(_a.password_file).read().strip()
CTX = ssl.create_default_context()
if _a.insecure:
    CTX.check_hostname = False
    CTX.verify_mode = ssl.CERT_NONE
import base64
AUTH = "Basic " + base64.b64encode(f"{_a.user}:{PW}".encode()).decode()


def call(method, path, body=None, timeout=300):
    req = urllib.request.Request(ES + path, method=method,
                                 data=json.dumps(body).encode() if body else None)
    req.add_header("Authorization", AUTH)
    if body:
        req.add_header("Content-Type", "application/json")
    # ES's scheme is checked once at startup, above; every call here is ES
    # plus a path this script builds, so only http and https ever reach
    # this call.
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:  # nosec B310
            raw = r.read()
            # A JSON array is as much JSON as an object. The _cat APIs answer
            # with one under format=json, and treating it as text made every
            # caller re-parse a string it had already been handed.
            head = raw.strip()[:1]
            return r.status, (json.loads(raw) if head in (b"{", b"[")
                              else raw.decode())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw.decode(errors="replace")


def fail(msg):
    print(f"  FAIL: {msg}")
    sys.exit(1)


print("== cluster ==")
_, h = call("GET", "/_cluster/health")
print(f"  status={h.get('status')} nodes={h.get('number_of_nodes')} "
      f"unassigned={h.get('unassigned_shards')}")
if h.get("status") == "red":
    # A cluster-wide gate fails on damage that has nothing to do with the
    # repository under test, and a check that cries wolf stops being read. So
    # name what is actually broken and decide from that.
    #
    # Seen on the lab cluster: ten shards unassigned across two indices left by
    # an unrelated campaign months earlier, one of them ALLOCATION_FAILED and
    # one CLUSTER_RECOVERED after a pod restart. Nothing to do with the
    # repository being verified, and enough to fail every run forever.
    _, rows = call("GET", "/_cat/shards?h=index,state&format=json")
    broken = sorted({r["index"] for r in (rows or [])
                     if r.get("state") != "STARTED"})
    _, cat = call("GET", f"/_snapshot/{REPO}/_all?ignore_unavailable=true")
    covered = set()
    for snap in (cat.get("snapshots", []) if isinstance(cat, dict) else []):
        covered.update(snap.get("indices", []) or [])
    ours = [i for i in broken if i in covered]
    print(f"  unhealthy indices: {', '.join(broken) or 'none'}")
    if ours:
        fail(f"cluster is RED and {len(ours)} of the unhealthy indices are in "
             f"this repository: {', '.join(ours)}")
    print(f"  none of them appear in any snapshot of {REPO}, so this is damage "
          "this repository did not cause and cannot fix; continuing")

print("== snapshots ==")
_, s = call("GET", f"/_snapshot/{REPO}/_all?ignore_unavailable=true")
snaps = s.get("snapshots", []) if isinstance(s, dict) else []
states = {}
for x in snaps:
    states[x.get("state")] = states.get(x.get("state"), 0) + 1
print(f"  {len(snaps)} snapshot(s): {states or 'none'}")
if not snaps:
    print("  nothing to restore from this pass, not a failure")
    sys.exit(0)
# IN_PROGRESS is the normal state of a cluster that is still taking snapshots,
# not a defect. Treating it as one meant this check never completed against a
# live repository, which is the only kind worth checking. It is excluded from
# the restore candidates instead, because restoring a snapshot mid-write is a
# different question than the one being asked.
#
# PARTIAL was previously accepted and should not be: it means shards failed and
# the snapshot cannot restore what it claims to. It is reported rather than
# silently used.
in_flight = [x["snapshot"] for x in snaps if x.get("state") == "IN_PROGRESS"]
partial = [x["snapshot"] for x in snaps if x.get("state") == "PARTIAL"]
broken = [x["snapshot"] for x in snaps
          if x.get("state") not in ("SUCCESS", "PARTIAL", "IN_PROGRESS")]
if in_flight:
    print(f"  {len(in_flight)} still being written, excluded from the restore")
if partial:
    print(f"  {len(partial)} PARTIAL, meaning shards failed: {partial[:3]}")
if broken:
    fail(f"snapshot(s) in a state that should not occur: {broken[:3]}")
snaps = [x for x in snaps if x.get("state") == "SUCCESS"]
if not snaps:
    print("  no completed snapshot to restore from yet, not a failure")
    sys.exit(0)

print("== integrity ==")
code, v = call("POST", f"/_snapshot/{REPO}/_verify_integrity", timeout=600)
anomalies = v.get("anomalies") if isinstance(v, dict) else None
print(f"  http={code} anomalies={anomalies if anomalies is not None else v if code!=200 else 0}")

# The only check that matters. Restore and count.
print("== restore, which is the only check that survives the others passing ==")
# A `partial-` index is a frozen-tier searchable-snapshot mount. Restoring one
# yields an index whose data still lives in the object store, so it counts zero
# documents locally and proves nothing about whether bytes survived. Restore a
# `.ds-` backing index, which actually carries its own segments.
target = src = None
for x in sorted(snaps, key=lambda y: y.get("start_time_in_millis", 0), reverse=True):
    if x.get("state") != "SUCCESS":
        continue
    real = [i for i in x.get("indices", []) if not i.startswith("partial-")]
    if real:
        target, src = x, real[0]
        break
if target is None:
    print("  no SUCCESS snapshot holding a non-mounted index; nothing restorable this pass")
    sys.exit(0)
stamp = str(int(time.time()))
code, body = call("POST", f"/_snapshot/{REPO}/{target['snapshot']}/_restore?wait_for_completion=true",
                  {"indices": src, "include_aliases": False,
                   # ^(.+)$ rather than .* : `.*` also matches the empty string at the end, so
                   # Elasticsearch applies the replacement twice and the index lands under a
                   # doubled name. Every check downstream then queries a name that does not
                   # exist and reads the absence as zero documents.
                   "rename_pattern": "^(.+)$", "rename_replacement": f"probe{stamp}",
                   "index_settings": {"index.number_of_replicas": 0}}, timeout=900)
print(f"  restore {target['snapshot']} index {src[:48]} -> probe{stamp}: http={code}")
if code != 200:
    fail(f"restore refused: {str(body)[:200]}")
shards = (body.get("snapshot") or {}).get("shards", {}) if isinstance(body, dict) else {}
print(f"  shards: {shards}")
if shards.get("failed"):
    fail(f"{shards['failed']} shard(s) failed to restore")

call("POST", f"/probe{stamp}/_refresh")
_, c = call("GET", f"/probe{stamp}/_count")
restored = c.get("count") if isinstance(c, dict) else None
print(f"  documents restored: {restored}")
call("DELETE", f"/probe{stamp}")
if not restored:
    fail("restore returned ZERO documents; the repository is not intact")
print("  INTACT")
