#!/usr/bin/env python3
r"""Is Elasticsearch still whole after objects were deleted underneath it?

A repository can list clean, report SUCCESS on every snapshot and pass an
integrity check while being unrestorable. This project measured exactly that: a
mounted index with all eight of its data blobs deleted answered HTTP 200 with
"failed": 0, and a `size=1` query missed the loss entirely.

So the cheap checks run first because they are cheap, and none of them is
believed. The answer comes from restoring a snapshot and counting what comes
back. Exit 0 means intact, 1 means something is wrong and the caller should stop.

TLS verification is always on and there is no flag to turn it off. A cluster
that serves a certificate it signed itself, which is every ECK install, is
reached by naming the CA that signed it. ECK publishes that CA in a secret
beside the cluster, so getting it is one line:

  kubectl -n <namespace> get secret <cluster>-es-http-certs-public \
      -o jsonpath='{.data.ca\.crt}' | base64 -d > ca.crt
  ./verify_restorable.py --elasticsearch https://es:9200 --repository my-repo \
      --password-file pw.txt --ca-cert ca.crt
"""
import argparse
import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# How an ECK operator gets the CA their cluster signed its certificate with.
# Quoted in --help and in the refusal, so nobody has to go and find it.
CA_CERT_EXTRACTION = (
    r"kubectl -n <namespace> get secret <cluster>-es-http-certs-public "
    r"-o jsonpath='{.data.ca\.crt}' | base64 -d > ca.crt")

# The epilog is printed as written. Rewrapped, the command above breaks
# across a hyphen in the middle of a secret name and stops being a line
# anyone can paste.
CA_CERT_EPILOG = (
    "getting the CA for a cluster running under ECK:\n\n  "
    + CA_CERT_EXTRACTION + "\n")


class Parser(argparse.ArgumentParser):
    """argparse, with one extra sentence when --insecure turns up.

    This tool verifies certificates and has no switch for turning that off, so
    an operator arriving with --insecure in a saved command line needs pointing
    at --ca-cert. A bare "unrecognized arguments" points them at the source to
    add the flag back instead.

    The churn rig and the size report carry the same class for the same
    reason. The three ship separately and a stale command line reaches
    whichever one it names.
    """

    def error(self, message):
        if "--insecure" in message:
            message += (". TLS verification is always on. A lab cluster "
                        "serving a certificate it signed itself is reached "
                        "by trusting the CA that signed it: " +
                        CA_CERT_EXTRACTION + " ... then --ca-cert ca.crt")
        super().error(message)


def path_segment(name):
    """One name, encoded so it can only ever be a single path segment.

    The repository name comes from the command line and snapshot names come
    back from the cluster. Pasted into a URL as they are, a name holding a
    slash or a question mark aims the request at a different API than the
    code reads as calling.
    """
    return urllib.parse.quote(str(name), safe="")


def read_secret(path, what):
    """The one line in a secret file, or a refusal naming what would not open.

    The path is resolved first and the resolved name is what gets opened, so
    the file that was checked is the file that is read. Anything that is not
    a regular file is refused before the open rather than during it: a
    directory arrives as a crash from inside a read, and a device node or a
    pipe hands back bytes that read as a password and fail two steps later
    as an authentication error, which sends whoever is on call looking at
    the cluster instead of at the flag.

    The message quotes the path and never the contents, because the contents
    are the secret.
    """
    resolved = os.path.realpath(path)
    if not os.path.isfile(resolved):
        sys.exit(f"{what} {path!r} is not a regular file "
                 f"(it resolves to {resolved!r})")
    try:
        with open(resolved) as handle:
            return handle.read().strip()
    except OSError as problem:
        sys.exit(f"{what} {path!r} could not be read: "
                 f"{problem.__class__.__name__}: "
                 f"{problem.strerror or problem}")


def _pinned_context(ca_cert):
    """The one place this script builds a TLS context."""
    context = ssl.create_default_context(cafile=ca_cert)
    # create_default_context leaves minimum_version at MINIMUM_SUPPORTED and
    # lets the host's OpenSSL decide the floor. Naming it here makes the floor
    # a property of this tool rather than of whatever machine it runs on.
    #
    # 1.2 rather than 1.3, because a cluster that speaks only 1.2 is ordinary
    # and refusing it would fail the restore check for an unrelated reason.
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def checked_ca_cert(parser, path):
    """Refuse a --ca-cert that will not load, naming the path that failed.

    The file is loaded here rather than only opened, so a path that is
    missing, unreadable or not a certificate all fail at the command line
    with the path in the message. Left to the first connection, the same
    three read as a broken cluster.
    """
    if path is None:
        return
    try:
        # Built through the same helper the real connection uses, so
        # there is one context construction in this file and the
        # floor below is pinned on it. A throwaway context here was
        # a second, unpinned one that proved nothing about the first.
        _pinned_context(path)
    except (OSError, ssl.SSLError) as problem:
        parser.error(f"--ca-cert {path!r} could not be read: "
                     f"{getattr(problem, 'strerror', None) or problem}. It "
                     f"wants the PEM file holding the CA that signed the "
                     f"cluster certificate. Under ECK: {CA_CERT_EXTRACTION}")


_p = Parser(
    description="Prove a snapshot repository is still restorable after\n"
                "deletion traffic. Reads and restores under a fresh name;\n"
                "deletes nothing from the repository.",
    epilog=CA_CERT_EPILOG,
    formatter_class=argparse.RawDescriptionHelpFormatter)
_p.add_argument("--elasticsearch", required=True, metavar="URL")
_p.add_argument("--repository", required=True, metavar="NAME")
_p.add_argument("--user", default="elastic")
_p.add_argument("--password-file", required=True, metavar="PATH",
                help="a PATH, never a value: a secret in argv is visible in ps")
_p.add_argument("--ca-cert", metavar="PEM",
                help="PEM file holding the CA that signed the cluster's "
                     "certificate. This is how a lab cluster serving its own "
                     "certificate is reached; verification is always on and "
                     "there is no flag to turn it off. The line at the bottom "
                     "of this help gets the CA out of an ECK cluster")
_a = _p.parse_args()

REPO = _a.repository

# --elasticsearch comes from configuration, not from the network, but
# configuration is not the same as trusted. urlopen does not care, and will
# happily open file:// or ftp://. This script only ever needs http or https,
# so anything else is refused before the value is passed to urlopen. What
# every request is then built on is rebuilt from the parts that passed, so a
# query string or a fragment typed into --elasticsearch cannot reappear in
# the middle of a request path.
_split = urllib.parse.urlsplit(_a.elasticsearch)
if _split.scheme not in ("http", "https"):
    _p.error(f"--elasticsearch is {_a.elasticsearch!r}; only http and https "
             f"are accepted, so a {_split.scheme or '(no scheme)'!r} value "
             f"cannot be opened")
if not _split.hostname:
    _p.error(f"--elasticsearch is {_a.elasticsearch!r} and names no host, so "
             f"there is nothing to connect to")
ES = urllib.parse.urlunsplit(
    (_split.scheme, _split.netloc, _split.path.rstrip("/"), "", ""))

# Verification is always on. The restore this script drives reads a whole
# index back over this connection, and the cluster password went out over it
# first; an unverified connection hands both to whichever host answered. A
# lab cluster serving its own certificate is reached with --ca-cert, which
# verifies the connection rather than abandoning it.
checked_ca_cert(_p, _a.ca_cert)
CTX = _pinned_context(_a.ca_cert)
# create_default_context leaves minimum_version at MINIMUM_SUPPORTED before
# Python 3.10, which lets the host's OpenSSL build pick the floor and gives a
# different answer on every machine. 1.2 rather than 1.3: a cluster that
# speaks only 1.2 is ordinary, and refusing it would break this script for a
# reason that has nothing to do with the connection being safe.
CTX.minimum_version = ssl.TLSVersion.TLSv1_2

PW = read_secret(_a.password_file, "--password-file")
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
    _, cat = call("GET", f"/_snapshot/{path_segment(REPO)}"
                         f"/_all?ignore_unavailable=true")
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
_, s = call("GET",
            f"/_snapshot/{path_segment(REPO)}/_all?ignore_unavailable=true")
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
code, v = call("POST", f"/_snapshot/{path_segment(REPO)}/_verify_integrity",
               timeout=600)
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
code, body = call("POST",
                  f"/_snapshot/{path_segment(REPO)}"
                  f"/{path_segment(target['snapshot'])}"
                  f"/_restore?wait_for_completion=true",
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
