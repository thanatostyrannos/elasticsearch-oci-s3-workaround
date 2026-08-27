#!/usr/bin/env python3
"""
snapshot_churn_rig.py: stand up a continuously churning Elasticsearch
snapshot repository, so that tooling which classifies and reclaims leaked
objects can be measured against something that behaves like production.

What it builds. The script creates, under one namespace prefix, a data
stream, an ILM policy that rolls data from hot through frozen (as a partial
searchable snapshot) and then deletes it, an SLM policy that snapshots the
stream on a schedule and expires those snapshots on a retention window, and
the snapshot repository all of that writes into. It then indexes documents
continuously, so Elasticsearch itself manufactures every object in the
repository and Elasticsearch itself deletes them. Against a store that
rejects Elasticsearch's batch delete, every expiry leaks its blobs, which
turns the lifecycle into a continuous generator of exactly the garbage the
reclaim tooling exists to classify. No script places an object by hand.

Why a data stream and not an alias with a write index. A data stream is what
modern log ingestion produces in production, rollover needs no bootstrap
write index that the script would have to fabricate, and the ILM searchable
snapshot and delete actions operate on its backing indices without extra
plumbing. The alias shape is the legacy path and would put a hand-built step
back into an environment whose whole point is that Elasticsearch builds it.

Subcommands.

  run       preflight, create everything, ingest and report until the
            duration ends. The environment keeps churning after the run
            ends (SLM and ILM are cluster-side); teardown is separate on
            purpose, because the leaked state IS the measurement target.
  status    emit one machine readable report and exit. Safe at any time.
  teardown  remove only what this script created, restore the cluster
            settings it changed, and verify both.

Cluster settings the script touches. It records the prior persistent values
of indices.lifecycle.poll_interval, slm.retention_schedule and
slm.minimum_interval into the state file before changing them, and teardown
restores exactly those recorded values. Without a short poll interval a
one minute ILM age does nothing for ten minutes, and without a frequent
retention schedule SLM checks expiry once a day at 01:30.

Reports. Each report is one JSON object covering ingest counters, backing
indices by ILM phase, snapshot counts alive and expired, SLM deletion
attempts and failures, mounted searchable snapshots, and, when S3 listing
credentials are supplied, the repository itself: object count, bytes, root
generation count, and metadata blobs of expired snapshots that a failed
delete left behind. A mounted index whose source snapshot no longer exists
is reported as a hazard, never prevented: that state is one of the things
the reclaim tooling is measured against.

Secrets. The Elasticsearch password comes from --password-file or the
ES_PASSWORD environment variable. The S3 secret key comes from
--s3-secret-key-file or S3_SECRET_KEY. No secret is accepted on the
command line.

Requirements on the target cluster: Elasticsearch 8.x or 9.x with ILM and
SLM available, a node carrying the data_frozen role, and a searchable
snapshot shared cache larger than zero on that node. Preflight checks all
of this and refuses with a plain message when the cluster cannot do it.
Python 3.8+, standard library only, runs from a copy.

Example, defaults spelled out:

  ES_PASSWORD=... ./snapshot_churn_rig.py run \\
      --es http://localhost:9200 --user elastic \\
      --bucket my-test-bucket \\
      --snapshot-interval 15m --retention 1h --duration 1h

  ./snapshot_churn_rig.py status --es http://localhost:9200 --user elastic
  ./snapshot_churn_rig.py teardown --es http://localhost:9200 --user elastic
"""

import argparse
import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

DEFAULT_PREFIX = "churnrig"

# ---------------------------------------------------------------------------
# small helpers

_DUR_RE = re.compile(r"^(\d+)(ms|s|m|h|d)?$")
_DUR_UNIT = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, None: 1}


def parse_duration(text):
    """Turn an Elasticsearch style duration like 15m or 1h into seconds."""
    m = _DUR_RE.match(text.strip())
    if not m:
        raise argparse.ArgumentTypeError(
            "duration %r is not <number><ms|s|m|h|d>" % text)
    return int(m.group(1)) * _DUR_UNIT[m.group(2)]


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print("[%s] %s" % (now_iso(), msg), file=sys.stderr, flush=True)


def die(msg, code=2):
    log("FATAL: " + msg)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Elasticsearch client, urllib only


class EsError(Exception):
    def __init__(self, status, body, url):
        super().__init__("HTTP %s on %s: %s" % (status, url, body[:400]))
        self.status = status
        self.body = body


class Es:
    def __init__(self, base, user, password, ca_cert, insecure):
        self.base = base.rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        if user and password:
            tok = base64.b64encode(
                ("%s:%s" % (user, password)).encode()).decode()
            self.headers["Authorization"] = "Basic " + tok
        if self.base.startswith("https"):
            if insecure:
                self.ctx = ssl._create_unverified_context()
            else:
                self.ctx = ssl.create_default_context(cafile=ca_cert)
        else:
            self.ctx = None

    def req(self, method, path, body=None, ok=(200, 201), timeout=60,
            ndjson=None):
        url = self.base + path
        data = None
        headers = dict(self.headers)
        if ndjson is not None:
            data = ndjson.encode()
            headers["Content-Type"] = "application/x-ndjson"
        elif body is not None:
            data = json.dumps(body).encode()
        r = urllib.request.Request(url, data=data, method=method,
                                   headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=timeout,
                                        context=self.ctx) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            status = e.code
        except (urllib.error.URLError, OSError) as e:
            raise EsError(0, str(e), url)
        if status not in ok:
            raise EsError(status, raw, url)
        if not raw:
            return status, None
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw

    def get(self, path, ok=(200,), timeout=60):
        return self.req("GET", path, ok=ok, timeout=timeout)[1]

    def get_or_none(self, path, timeout=60):
        status, body = self.req("GET", path, ok=(200, 404), timeout=timeout)
        return body if status == 200 else None

    def put(self, path, body=None, timeout=120):
        return self.req("PUT", path, body=body, timeout=timeout)[1]

    def delete(self, path, ok=(200, 404), timeout=300):
        return self.req("DELETE", path, ok=ok, timeout=timeout)


# ---------------------------------------------------------------------------
# S3 listing and single-object delete, SigV4 with the standard library.
# Duplicating a small signer here instead of importing one keeps the tool
# runnable from a copy, which this repository treats as a feature.


class S3:
    def __init__(self, endpoint, region, access_key, secret_key, bucket):
        self.endpoint = endpoint.rstrip("/")
        self.region = region
        self.access = access_key
        self.secret = secret_key
        self.bucket = bucket
        self.host = urllib.parse.urlparse(self.endpoint).netloc

    def _sign(self, method, key, query):
        amz_date = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ")
        datestamp = amz_date[:8]
        payload_hash = hashlib.sha256(b"").hexdigest()
        canonical_uri = "/" + self.bucket
        if key:
            canonical_uri += "/" + urllib.parse.quote(key, safe="/-_.~")
        canonical_query = "&".join(
            "%s=%s" % (urllib.parse.quote(k, safe="-_.~"),
                       urllib.parse.quote(str(v), safe="-_.~"))
            for k, v in sorted(query.items()))
        canonical_headers = ("host:%s\nx-amz-content-sha256:%s\n"
                             "x-amz-date:%s\n" % (self.host, payload_hash,
                                                  amz_date))
        signed = "host;x-amz-content-sha256;x-amz-date"
        creq = "\n".join([method, canonical_uri, canonical_query,
                          canonical_headers, signed, payload_hash])
        scope = "%s/%s/s3/aws4_request" % (datestamp, self.region)
        sts = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(creq.encode()).hexdigest()])

        def h(k, m):
            return hmac.new(k, m.encode(), hashlib.sha256).digest()

        k = h(("AWS4" + self.secret).encode(), datestamp)
        k = h(h(h(k, self.region), "s3"), "aws4_request")
        sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
        auth = ("AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, "
                "Signature=%s" % (self.access, scope, signed, sig))
        url = self.endpoint + canonical_uri
        if canonical_query:
            url += "?" + canonical_query
        return url, {"Authorization": auth, "x-amz-date": amz_date,
                     "x-amz-content-sha256": payload_hash}

    def _call(self, method, key="", query=None, ok=(200, 204)):
        url, headers = self._sign(method, key, query or {})
        r = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code not in ok:
                raise EsError(e.code, body.decode("utf-8", "replace"), url)
            return e.code, body

    def list(self, prefix):
        """Every object under prefix, as (key, size) pairs."""
        out = []
        token = None
        while True:
            q = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if token:
                q["continuation-token"] = token
            status, body = self._call("GET", query=q)
            root = ET.fromstring(body)
            ns = ""
            if root.tag.startswith("{"):
                ns = root.tag[:root.tag.index("}") + 1]
            for c in root.iter(ns + "Contents"):
                out.append((c.find(ns + "Key").text,
                            int(c.find(ns + "Size").text)))
            trunc = root.find(ns + "IsTruncated")
            if trunc is None or trunc.text != "true":
                return out
            token = root.find(ns + "NextContinuationToken").text

    def delete_object(self, key):
        """Single-object DELETE. This path works even against stores that
        reject the batch DeleteObjects call Elasticsearch uses."""
        self._call("DELETE", key=key, ok=(204, 200, 404))


def make_s3(args):
    """Build the S3 lister when endpoint and credentials are all present,
    otherwise return None and let reports say why the repository section
    is missing."""
    if not args.s3_endpoint:
        return None, "no --s3-endpoint supplied"
    access = args.s3_access_key or os.environ.get("S3_ACCESS_KEY")
    secret = None
    if args.s3_secret_key_file:
        with open(args.s3_secret_key_file) as f:
            secret = f.read().strip()
    else:
        secret = os.environ.get("S3_SECRET_KEY")
    if not access or not secret:
        return None, ("s3 endpoint supplied but credentials missing; need "
                      "--s3-access-key or S3_ACCESS_KEY, and "
                      "--s3-secret-key-file or S3_SECRET_KEY")
    if not args.bucket:
        return None, "s3 endpoint supplied but no --bucket"
    return S3(args.s3_endpoint, args.s3_region, access, secret,
              args.bucket), None


# ---------------------------------------------------------------------------
# naming


def names(prefix, data_stream=None):
    """Every name this harness creates, derived from one prefix.

    `data_stream` overrides just that one name, for a cluster where the index
    name is decided by a convention this harness does not get to pick. Only the
    stream moves: the repository, policies and template stay on the prefix, so
    two rigs writing the same stream name into different buckets do not collide.

    The teardown scope is computed from the resolved name, so an override
    narrows what teardown will touch rather than widening it.
    """
    return {
        "repo": prefix + "-repo",
        "ilm": prefix + "-ilm",
        "template": prefix + "-template",
        "data_stream": data_stream or (prefix + "-stream"),
        "slm": prefix + "-slm",
        "snap_prefix": prefix + "-snap-",
    }


MANAGED_SETTINGS = ("indices.lifecycle.poll_interval",
                    "slm.retention_schedule",
                    "slm.minimum_interval")


# ---------------------------------------------------------------------------
# preflight


def es_version(es):
    info = es.get("/")
    return tuple(int(x) for x in
                 info["version"]["number"].split(".")[:2])


def check_frozen_capable(es):
    """A frozen phase needs a node with the data_frozen role and a shared
    cache bigger than zero. Refuse with a plain message otherwise."""
    nodes = es.get("/_nodes?filter_path=nodes.*.roles,nodes.*.settings"
                   ".xpack.searchable.snapshot.shared_cache.size")
    frozen_ok = False
    cache_seen = None
    for node in nodes.get("nodes", {}).values():
        roles = node.get("roles", [])
        if "data_frozen" not in roles:
            continue
        frozen_ok = True
        cache = (node.get("settings", {}).get("xpack", {})
                 .get("searchable", {}).get("snapshot", {})
                 .get("shared_cache", {}).get("size"))
        if roles == ["data_frozen"]:
            cache_seen = cache or "default (90% on a dedicated frozen node)"
        elif cache and cache not in ("0", "0b"):
            cache_seen = cache
    if not frozen_ok:
        die("no node carries the data_frozen role, so the ILM frozen phase "
            "cannot mount searchable snapshots on this cluster")
    if cache_seen is None:
        die("a data_frozen node exists but "
            "xpack.searchable.snapshot.shared_cache.size is unset or zero "
            "on it; partial searchable snapshots cannot mount. Set a "
            "nonzero shared cache on that node and retry")
    return cache_seen


def check_prefix_free(es, prefix, n):
    """Refuse when anything on the cluster already answers to the prefix,
    so the harness can never entangle itself with another tenant's work."""
    hits = []
    res = es.get_or_none(
        "/_resolve/index/*%s*?expand_wildcards=all" % prefix) or {}
    for kind in ("indices", "aliases", "data_streams"):
        for entry in res.get(kind, []):
            hits.append("%s %s" % (kind[:-1] if kind != "indices"
                                   else "index", entry["name"]))
    if es.get_or_none("/_ilm/policy/" + n["ilm"]):
        hits.append("ilm policy " + n["ilm"])
    if es.get_or_none("/_slm/policy/" + n["slm"]):
        hits.append("slm policy " + n["slm"])
    if es.get_or_none("/_snapshot/" + n["repo"]):
        hits.append("snapshot repository " + n["repo"])
    if es.get_or_none("/_index_template/" + n["template"]):
        hits.append("index template " + n["template"])
    if hits:
        die("prefix %r collides with existing cluster state: %s. Pick "
            "another --prefix, or run teardown if these belong to a "
            "previous run of this script" % (prefix, "; ".join(hits)))


def derive_refusal(state, derive_ok, n):
    """Why teardown must not run from a prefix alone, or None when it may.

    With no state file every name teardown acts on comes from --prefix: the
    repository is <prefix>-repo, the policies <prefix>-slm and <prefix>-ilm.
    Those are ordinary names that other people pick. In the lab this runs
    against, gcw-repo holds 2 snapshots, s3c3-repo 2 and scalerig-repo 11, and
    every one of those prefixes passes the --prefix validator. Teardown would
    delete their snapshots and unregister them.

    Deleting snapshots is also the operation this project exists because of.
    Against a store that drops batch deletes it is what strands the objects
    nobody can then account for. Doing that to a repository the harness never
    created is the worst thing this script can do.
    """
    if state is not None or derive_ok:
        return None
    return ("refusing to tear down from --prefix alone. Without a state file "
            "every name below is a guess, and each one may belong to "
            "something this harness never created:\n"
            "    repository      %s\n"
            "    slm policy      %s\n"
            "    ilm policy      %s\n"
            "    index template  %s\n"
            "Check them against the cluster, then pass --derive-from-prefix "
            "to go ahead." % (n["repo"], n["slm"], n["ilm"], n["template"]))


def purge_refusal(state, base_path_explicit, base_path):
    """Why a bucket purge must not run, or None when the scope was stated.

    With no state file and no --base-path, teardown falls back to --prefix for
    the path it purges. That guess is the only thing scoping the delete, and
    the bucket here is shared: gcw, s3c3, scalerig, rv1based and rv2stale are
    all base paths of live repositories and all valid prefixes.

    This stays refused even under --derive-from-prefix. A wrong index or
    repository can be rebuilt from the cluster. Objects deleted out of this
    store cannot, which is the premise the whole repository rests on.
    """
    if state is not None or base_path_explicit:
        return None
    return ("refusing to purge bucket path %r: it came from --prefix, not "
            "from a run's state file and not from you. Objects deleted here "
            "do not come back. Pass --base-path to state the path, or run "
            "teardown where the state file is." % base_path)


def teardown_index_scope(resolved, data_stream):
    """Names from a _resolve response that teardown may delete.

    Every index this harness creates is a backing index of its own data
    stream, so the name carries -<data_stream>- somewhere inside. ILM keeps
    that intact when it mounts a searchable snapshot, prefixing partial- or
    restored- onto the front, so the frozen mounts match too.

    An index that merely starts with the prefix belongs to someone else.
    check_prefix_free refuses to start a run while any such index exists, so
    the harness never shares a namespace with one, and teardown has no reason
    to reach for it. It only ever reached the wrong thing.
    """
    marker = "-%s-" % data_stream
    return [e["name"] for e in resolved.get("indices", [])
            if marker in e["name"]]


def slm_schedule(version, interval_text, override):
    """Return the SLM schedule string for the wanted cadence.

    8.14+ accepts a plain interval like 15m, which cannot be got subtly
    wrong the way a cron expression can. Older clusters get a generated
    cron, and only for whole-minute intervals that divide the hour evenly;
    anything else must be spelled out with --slm-cron because a cron like
    0 0/7 restarts its count at the top of every hour and does not mean
    every seven minutes. The run loop verifies the real cadence by
    observing snapshot start times either way.
    """
    if override:
        return override
    if version >= (8, 14):
        return interval_text
    secs = parse_duration(interval_text)
    if secs % 60 == 0 and (secs // 60) < 60 and 3600 % secs == 0:
        return "0 0/%d * * * ?" % (secs // 60)
    die("this cluster is older than 8.14 so the SLM schedule must be cron, "
        "and %r does not map to an even cron cadence; pass --slm-cron "
        "yourself" % interval_text)


def retention_cron(check_interval_text):
    secs = int(parse_duration(check_interval_text))
    mins = max(1, secs // 60)
    if 60 % mins != 0:
        log("WARNING: a retention check every %d minutes restarts at the "
            "top of each hour, so the last gap in every hour is shorter"
            % mins)
    return "0 0/%d * * * ?" % mins


# ---------------------------------------------------------------------------
# setup


def read_prior_settings(es):
    flat = es.get("/_cluster/settings?flat_settings=true")
    prior = {}
    for key in MANAGED_SETTINGS:
        prior[key] = flat.get("persistent", {}).get(key)
    return prior


def cmd_setup(es, args, n, s3):
    version = es_version(es)
    if version < (7, 12):
        die("cluster is %s; the frozen tier needs 7.12 or later"
            % ".".join(map(str, version)))
    cache = check_frozen_capable(es)
    check_prefix_free(es, args.prefix, n)
    log("preflight passed: version %s, frozen shared cache %s"
        % (".".join(map(str, version)), cache))

    prior = read_prior_settings(es)
    schedule = slm_schedule(version, args.snapshot_interval, args.slm_cron)
    wanted = {
        "indices.lifecycle.poll_interval": args.ilm_poll_interval,
        "slm.retention_schedule": retention_cron(
            args.retention_check_interval),
    }
    min_interval_secs = parse_duration(args.snapshot_interval)
    prior_min = prior.get("slm.minimum_interval")
    effective_min = parse_duration(prior_min) if prior_min else 900
    if min_interval_secs < effective_min:
        wanted["slm.minimum_interval"] = args.snapshot_interval

    state = {
        "prefix": args.prefix,
        "started": now_iso(),
        "names": n,
        "prior_settings": prior,
        "settings_changed": wanted,
        "repo_type": args.repo_type,
        "bucket": args.bucket,
        "base_path": args.base_path or args.prefix,
        "slm_schedule": schedule,
    }
    with open(args.state_file, "w") as f:
        json.dump(state, f, indent=2)
    log("state written to %s (teardown restores settings from it)"
        % args.state_file)

    es.put("/_cluster/settings", {"persistent": wanted})
    log("cluster settings set: %s" % json.dumps(wanted))

    if args.repo_type == "s3":
        if not args.bucket:
            die("--repo-type s3 needs --bucket")
        repo_settings = {"bucket": args.bucket, "client": args.s3_client,
                         "base_path": state["base_path"]}
    else:
        if not args.location:
            die("--repo-type fs needs --location")
        repo_settings = {"location": args.location}
    repo_body = {"type": args.repo_type, "settings": repo_settings}
    try:
        es.put("/_snapshot/" + n["repo"], repo_body)
        log("repository %s registered and verified" % n["repo"])
    except EsError as e:
        if "cannot delete test data" not in e.body:
            raise
        # Registration verifies by writing test blobs and batch-deleting
        # them, and a store that rejects the batch delete fails right
        # here. That rejection is the property this rig exists to
        # exercise, so record it and register without the verify pass,
        # the same registration Elastic support prescribes for frozen
        # repositories on such stores.
        log("store rejected the batch delete inside repository "
            "verification, the first evidence this store leaks deletes; "
            "registering %s with verify=false and continuing" % n["repo"])
        state["verify_rejected_batch_delete"] = True
        es.put("/_snapshot/%s?verify=false" % n["repo"], repo_body)
        with open(args.state_file, "w") as f:
            json.dump(state, f, indent=2)

    es.put("/_ilm/policy/" + n["ilm"], {"policy": {"phases": {
        "hot": {"min_age": "0ms", "actions": {"rollover": {
            "max_age": args.rollover_max_age,
            "max_docs": args.rollover_max_docs}}},
        "frozen": {"min_age": args.frozen_min_age, "actions": {
            "searchable_snapshot": {
                "snapshot_repository": n["repo"],
                "force_merge_index": True}}},
        "delete": {"min_age": args.delete_min_age, "actions": {
            "delete": {"delete_searchable_snapshot": True}}},
    }}})
    log("ilm policy %s: rollover(max_age=%s, max_docs=%d) -> frozen(%s) "
        "-> delete(%s)" % (n["ilm"], args.rollover_max_age,
                           args.rollover_max_docs, args.frozen_min_age,
                           args.delete_min_age))

    es.put("/_index_template/" + n["template"], {
        "index_patterns": [n["data_stream"] + "*"],
        "data_stream": {},
        "priority": 500,
        "template": {"settings": {
            "index.number_of_shards": args.shards,
            "index.number_of_replicas": 0,
            "index.lifecycle.name": n["ilm"]}}})
    es.put("/_data_stream/" + n["data_stream"])
    log("data stream %s created (%d shard(s) per backing index)"
        % (n["data_stream"], args.shards))

    es.put("/_slm/policy/" + n["slm"], {
        "schedule": schedule,
        "name": "<" + n["snap_prefix"] + "{now/s{yyyyMMdd-HHmmss|UTC}}>",
        "repository": n["repo"],
        "config": {"indices": [n["data_stream"]],
                   "ignore_unavailable": True,
                   "include_global_state": False,
                   "partial": True},
        "retention": {"expire_after": args.retention}})
    log("slm policy %s: schedule=%s retention=%s (checked on cron %s)"
        % (n["slm"], schedule, args.retention,
           wanted["slm.retention_schedule"]))
    return state


# ---------------------------------------------------------------------------
# reporting


def snap_name(s):
    """The listing API names a snapshot under the key snapshot; older
    responses used name. Accept both so one field rename cannot blind
    every report."""
    return s.get("snapshot") or s.get("name") or ""


def gather_report(es, n, s3, s3_reason, base_path, cadence_memory=None):
    rep = {"ts": now_iso(), "prefix": n["data_stream"].rsplit("-", 1)[0]}

    ds = es.get_or_none("/_data_stream/" + n["data_stream"])
    if ds and ds.get("data_streams"):
        d = ds["data_streams"][0]
        rep["data_stream"] = {
            "backing_indices": len(d["indices"]),
            "backing_index_names": [i["index_name"] for i in d["indices"]],
            "generation": d["generation"]}
    else:
        rep["data_stream"] = None

    phases = {}
    errors = []
    explain = es.get_or_none("/%s/_ilm/explain" % n["data_stream"])
    if explain:
        for idx, info in explain.get("indices", {}).items():
            phases[info.get("phase", "unmanaged")] = \
                phases.get(info.get("phase", "unmanaged"), 0) + 1
            if info.get("step") == "ERROR":
                errors.append({"index": idx,
                               "failed_step": info.get("failed_step")})
    rep["by_ilm_phase"] = phases
    rep["ilm_errors"] = errors

    mounted = []
    settings = es.get_or_none(
        "/*%s*/_settings?expand_wildcards=all&filter_path="
        "*.settings.index.store.snapshot" % rep["prefix"]) or {}
    for idx, body in settings.items():
        snap = (body.get("settings", {}).get("index", {})
                .get("store", {}).get("snapshot", {}))
        if snap:
            mounted.append({"index": idx,
                            "snapshot": snap.get("snapshot_name"),
                            "repository": snap.get("repository_name"),
                            "partial": snap.get("partial")})

    alive = []
    snaps = es.get_or_none(
        "/_snapshot/%s/_all?ignore_unavailable=true" % n["repo"])
    if snaps:
        alive = snaps.get("snapshots", [])
    alive_names = {snap_name(s) for s in alive}
    alive_uuids = {s.get("uuid") for s in alive}
    by_state = {}
    for s in alive:
        by_state[s["state"]] = by_state.get(s["state"], 0) + 1

    hazards = []
    for m in mounted:
        if m["repository"] == n["repo"] and m["snapshot"] not in alive_names:
            hazards.append({
                "index": m["index"], "snapshot": m["snapshot"],
                "reason": "mounted searchable snapshot whose source "
                          "snapshot is no longer in the repository"})
    rep["mounted_searchable"] = {"count": len(mounted), "indices": mounted,
                                 "hazards": hazards}

    slm = es.get_or_none("/_slm/policy/" + n["slm"]) or {}
    stats = slm.get(n["slm"], {}).get("stats", {})
    global_stats = es.get_or_none("/_slm/stats") or {}
    starts = sorted(s["start_time_in_millis"] for s in alive
                    if snap_name(s).startswith(n["snap_prefix"]))
    if cadence_memory is not None:
        for s in alive:
            if snap_name(s).startswith(n["snap_prefix"]):
                cadence_memory[snap_name(s)] = s["start_time_in_millis"]
        starts = sorted(cadence_memory.values())
    deltas = [round((b - a) / 1000.0, 1)
              for a, b in zip(starts, starts[1:])]
    rep["snapshots"] = {
        "alive": len(alive),
        "alive_by_state": by_state,
        "taken_total": stats.get("snapshots_taken", 0),
        "failed_total": stats.get("snapshots_failed", 0),
        "expired_total": stats.get("snapshots_deleted", 0),
        "snapshot_deletion_failures": stats.get(
            "snapshot_deletion_failures", 0),
        "retention_runs": global_stats.get("retention_runs"),
        "retention_failed": global_stats.get("retention_failed"),
        "observed_start_deltas_s": deltas[-8:],
    }

    if s3 is None:
        rep["repository"] = {"unavailable": s3_reason}
        return rep

    base = base_path.rstrip("/") + "/" if base_path else ""
    objects = s3.list(base)
    gens = []
    snap_dats = []
    latest = None
    for key, _size in objects:
        rel = key[len(base):]
        if "/" in rel:
            continue
        m = re.fullmatch(r"index-(\d+)", rel)
        if m:
            gens.append(int(m.group(1)))
        elif rel == "index.latest":
            latest = True
        elif rel.startswith("snap-") and rel.endswith(".dat"):
            snap_dats.append(rel[5:-4])
    leaked_snap_meta = [u for u in snap_dats if u not in alive_uuids]
    rep["repository"] = {
        "object_count": len(objects),
        "bytes": sum(s for _k, s in objects),
        "root_generation_count": len(gens),
        "root_generations": sorted(gens)[-8:],
        "index_latest_present": bool(latest),
        "snapshot_metadata_blobs": len(snap_dats),
        "expired_snapshot_metadata_still_present": len(leaked_snap_meta),
    }
    return rep


# ---------------------------------------------------------------------------
# run loop

MILESTONES = (
    ("first_rollover", lambda r: (r.get("data_stream") or {})
     .get("backing_indices", 0) >= 2),
    ("first_frozen_mount", lambda r:
     r["mounted_searchable"]["count"] >= 1),
    ("first_snapshot_success", lambda r:
     r["snapshots"]["alive_by_state"].get("SUCCESS", 0) >= 1),
    ("first_snapshot_expired", lambda r:
     r["snapshots"]["expired_total"] >= 1),
    ("first_snapshot_deletion_failure", lambda r:
     r["snapshots"]["snapshot_deletion_failures"] >= 1),
    ("first_backing_index_deleted", lambda r:
     r.get("backing_index_disappeared", False)),
    ("first_leaked_root_generation", lambda r:
     isinstance(r.get("repository"), dict) and
     r["repository"].get("root_generation_count", 0) >= 3),
    ("first_leaked_snapshot_metadata", lambda r:
     isinstance(r.get("repository"), dict) and
     r["repository"].get(
         "expired_snapshot_metadata_still_present", 0) > 0),
    ("first_mount_hazard", lambda r:
     len(r["mounted_searchable"]["hazards"]) > 0),
)


def make_doc(seq, doc_bytes):
    payload = os.urandom(max(1, doc_bytes // 2)).hex()
    return {"@timestamp": now_iso(), "seq": seq, "payload": payload}


def cmd_run(es, args, n, s3, s3_reason):
    if os.path.exists(args.state_file):
        die("state file %s already exists; a previous run was not torn "
            "down. Run teardown first, or point --state-file elsewhere "
            "and pick a fresh --prefix" % args.state_file)
    state = cmd_setup(es, args, n, s3)
    base_path = state["base_path"]

    duration = parse_duration(args.duration)
    report_every = parse_duration(args.report_interval)
    poll_every = min(report_every, 20)
    rate = args.docs_per_second
    t0 = time.monotonic()
    docs_sent = 0
    bulk_errors = 0
    seq = 0
    milestones = {}
    cadence_memory = {}
    seen_backing = set()
    next_report = 0.0
    next_poll = 0.0
    log("run started: duration=%s ingest=%d docs/s of ~%d bytes; "
        "reports every %s to stdout and %s"
        % (args.duration, rate, args.doc_bytes, args.report_interval,
           args.report_file))

    def emit(rep):
        rep["elapsed_s"] = round(time.monotonic() - t0, 1)
        rep["ingest"] = {"docs_sent": docs_sent, "bulk_errors": bulk_errors}
        rep["milestones"] = {k: round(v, 1)
                             for k, v in sorted(milestones.items(),
                                                key=lambda kv: kv[1])}
        line = json.dumps(rep, sort_keys=True)
        print(line, flush=True)
        with open(args.report_file, "a") as f:
            f.write(line + "\n")
        return rep

    try:
        while time.monotonic() - t0 < duration:
            tick = time.monotonic()
            lines = []
            for _ in range(rate):
                seq += 1
                lines.append('{"create":{}}')
                lines.append(json.dumps(make_doc(seq, args.doc_bytes)))
            if lines:
                try:
                    _, resp = es.req(
                        "POST", "/%s/_bulk" % n["data_stream"],
                        ndjson="\n".join(lines) + "\n", timeout=60)
                    docs_sent += rate
                    if resp.get("errors"):
                        bulk_errors += sum(
                            1 for i in resp["items"]
                            if i["create"].get("status", 201) >= 300)
                except EsError as e:
                    bulk_errors += rate
                    log("bulk failed: %s" % e)

            now = time.monotonic() - t0
            if now >= next_poll:
                next_poll = now + poll_every
                try:
                    rep = gather_report(es, n, s3, s3_reason, base_path,
                                        cadence_memory)
                    # A frozen mount renames .ds-X to partial-.ds-X, so
                    # compare canonical names or every mount would count
                    # as a deletion.
                    current = {name[len("partial-"):]
                               if name.startswith("partial-") else name
                               for name in (rep.get("data_stream") or {})
                               .get("backing_index_names", [])}
                    if seen_backing - current:
                        rep["backing_index_disappeared"] = True
                    seen_backing |= current
                    for name, test in MILESTONES:
                        if name not in milestones and test(rep):
                            milestones[name] = now
                            log("milestone at %ds: %s" % (int(now), name))
                    for h in rep["mounted_searchable"]["hazards"]:
                        log("HAZARD: %s (index=%s snapshot=%s)"
                            % (h["reason"], h["index"], h["snapshot"]))
                    if now >= next_report:
                        next_report = now + report_every
                        emit(rep)
                except Exception as e:
                    log("report poll failed, continuing: %s: %s"
                        % (type(e).__name__, e))
            spent = time.monotonic() - tick
            if spent < 1.0:
                time.sleep(1.0 - spent)
    except KeyboardInterrupt:
        log("interrupted, emitting final report")
    final = emit(gather_report(es, n, s3, s3_reason, base_path,
                               cadence_memory))
    log("run finished after %ss. The environment keeps churning until "
        "teardown; that standing churn is the measurement target."
        % final["elapsed_s"])
    return 0


# ---------------------------------------------------------------------------
# teardown


def cmd_teardown(es, args, n, s3, s3_reason):
    state = None
    if os.path.exists(args.state_file):
        with open(args.state_file) as f:
            state = json.load(f)
        n = state["names"]
    else:
        log("WARNING: no state file at %s; deriving names from prefix %r "
            "and skipping settings restore because the prior values are "
            "not recorded" % (args.state_file, args.prefix))
    base_path = (state or {}).get("base_path", args.base_path or args.prefix)

    refusal = derive_refusal(state, args.derive_from_prefix, n)
    if refusal is None and args.purge_bucket:
        refusal = purge_refusal(state, args.base_path_explicit, base_path)
    if refusal:
        die(refusal)

    es.delete("/_slm/policy/" + n["slm"])
    log("slm policy %s deleted" % n["slm"])

    es.delete("/_data_stream/" + n["data_stream"])
    res = es.get_or_none("/_resolve/index/*%s*?expand_wildcards=all"
                         % args.prefix) or {}
    for name in teardown_index_scope(res, n["data_stream"]):
        es.delete("/" + name)
        log("leftover index %s deleted" % name)
    log("data stream %s deleted" % n["data_stream"])

    es.delete("/_index_template/" + n["template"])
    es.delete("/_ilm/policy/" + n["ilm"])
    log("index template and ilm policy deleted")

    snaps = es.get_or_none("/_snapshot/%s/_all?ignore_unavailable=true"
                           % n["repo"])
    if snaps:
        names_list = [snap_name(s) for s in snaps.get("snapshots", [])]
        for i in range(0, len(names_list), 10):
            batch = ",".join(names_list[i:i + 10])
            es.delete("/_snapshot/%s/%s" % (n["repo"], batch), timeout=600)
        log("%d snapshot(s) deleted from %s (each delete leaks blobs when "
            "the store rejects batch deletes; that is the state under "
            "test)" % (len(names_list), n["repo"]))
        es.delete("/_snapshot/" + n["repo"])
        log("repository %s unregistered" % n["repo"])

    if state:
        restore = {}
        for key in MANAGED_SETTINGS:
            if key in state["settings_changed"]:
                restore[key] = state["prior_settings"].get(key)
        es.put("/_cluster/settings", {"persistent": restore})
        log("cluster settings restored to recorded prior values: %s"
            % json.dumps(restore))

    purged = 0
    leftover = None
    if s3 is not None:
        base = base_path.rstrip("/") + "/" if base_path else ""
        objects = s3.list(base)
        if args.purge_bucket:
            for key, _size in objects:
                s3.delete_object(key)
                purged += 1
            leftover = len(s3.list(base))
            log("purged %d leaked object(s) under %s/%s via single-object "
                "deletes; %d remain" % (purged, s3.bucket, base, leftover))
        else:
            leftover = len(objects)
            if leftover:
                log("%d object(s) remain under %s/%s: the blobs "
                    "Elasticsearch failed to delete. Rerun teardown with "
                    "--purge-bucket to remove them, or keep them as a "
                    "measurement corpus" % (leftover, s3.bucket, base))
    else:
        log("bucket state not checked (%s)" % s3_reason)

    verdict = {"ts": now_iso(), "prefix": args.prefix, "clean": True,
               "leftover_bucket_objects": leftover, "purged": purged}
    res = es.get_or_none("/_resolve/index/*%s*?expand_wildcards=all"
                         % args.prefix) or {}
    remaining = [e["name"] for k in ("indices", "aliases", "data_streams")
                 for e in res.get(k, [])]
    for path, label in (("/_ilm/policy/" + n["ilm"], "ilm policy"),
                        ("/_slm/policy/" + n["slm"], "slm policy"),
                        ("/_snapshot/" + n["repo"], "repository"),
                        ("/_index_template/" + n["template"], "template")):
        if es.get_or_none(path):
            remaining.append(label)
    if remaining:
        verdict["clean"] = False
        verdict["remaining"] = remaining
    if state:
        current = read_prior_settings(es)
        mismatched = {k: {"expected": state["prior_settings"].get(k),
                          "actual": current.get(k)}
                      for k in MANAGED_SETTINGS
                      if k in state["settings_changed"]
                      and current.get(k) != state["prior_settings"].get(k)}
        if mismatched:
            verdict["clean"] = False
            verdict["settings_mismatch"] = mismatched
    print(json.dumps(verdict, sort_keys=True), flush=True)
    if verdict["clean"] and state:
        os.unlink(args.state_file)
        log("teardown verified clean; state file removed")
    elif not verdict["clean"]:
        log("teardown left residue, state file kept; see the verdict above")
        return 1
    return 0


# ---------------------------------------------------------------------------
# main


def build_parser():
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[1].strip(),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("cluster")
    g.add_argument("--es", required=True, metavar="URL",
                   help="Elasticsearch endpoint the harness drives, e.g. "
                        "http://localhost:9200")
    g.add_argument("--user", default="elastic",
                   help="user for basic auth; the password comes from "
                        "--password-file or ES_PASSWORD, never from argv")
    g.add_argument("--password-file", metavar="PATH",
                   help="file holding the Elasticsearch password, so the "
                        "secret stays out of the process list")
    g.add_argument("--ca-cert", metavar="PATH",
                   help="CA bundle for an https endpoint")
    g.add_argument("--insecure", action="store_true",
                   help="skip TLS verification for lab clusters with "
                        "self-signed certificates")
    g = common.add_argument_group("namespace")
    g.add_argument("--prefix", default=DEFAULT_PREFIX,
                   help="unique namespace for everything the harness "
                        "creates, so teardown can remove exactly this and "
                        "collisions with other tenants are refused; "
                        "lowercase letters and digits only")
    g.add_argument("--data-stream", metavar="NAME",
                   help="write into this data stream instead of "
                        "<prefix>-stream, for a cluster where the index name "
                        "follows a convention this harness does not pick; "
                        "everything else still comes from --prefix, and "
                        "teardown narrows to the name you give")
    g.add_argument("--state-file", metavar="PATH",
                   help="where setup records the prior cluster settings "
                        "and created names that teardown needs; defaults "
                        "to <prefix>-state.json in the working directory")
    g = common.add_argument_group("repository")
    g.add_argument("--repo-type", choices=("s3", "fs"), default="s3",
                   help="repository backend; s3 reproduces the "
                        "batch-delete leak, fs exists for plumbing tests")
    g.add_argument("--bucket",
                   help="bucket the s3 repository writes into; the "
                        "harness confines itself to base-path inside it")
    g.add_argument("--s3-client", default="default",
                   help="named s3 client in elasticsearch.yml whose "
                        "credentials the repository uses")
    g.add_argument("--base-path",
                   help="key prefix inside the bucket, so several rigs "
                        "can share one bucket; defaults to the prefix")
    g.add_argument("--location",
                   help="filesystem path for --repo-type fs")
    g = common.add_argument_group(
        "repository listing (optional; enables the object-count, "
        "generation-count and leak fields in reports)")
    g.add_argument("--s3-endpoint", metavar="URL",
                   help="S3 API endpoint to list the bucket through, so "
                        "reports can count what actually exists rather "
                        "than what Elasticsearch believes exists")
    g.add_argument("--s3-region", default="us-east-1",
                   help="region name used in request signing")
    g.add_argument("--s3-access-key",
                   help="access key id for listing; S3_ACCESS_KEY works "
                        "too")
    g.add_argument("--s3-secret-key-file", metavar="PATH",
                   help="file holding the secret key; S3_SECRET_KEY works "
                        "too. Never passed on argv")

    r = sub.add_parser(
        "run", parents=[common],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        help="create the environment and churn it for the duration")
    r.add_argument("--snapshot-interval", default="15m",
                   help="how often SLM takes a snapshot; the generator's "
                        "clock, one leak per interval once retention bites")
    r.add_argument("--retention", default="1h",
                   help="how long a snapshot lives before SLM expires it; "
                        "sets how many snapshots stay alive at once")
    r.add_argument("--retention-check-interval", default="5m",
                   help="how often SLM looks for expired snapshots; "
                        "without this the default cluster schedule checks "
                        "once a day at 01:30")
    r.add_argument("--slm-cron", metavar="CRON",
                   help="explicit SLM cron schedule, overriding "
                        "--snapshot-interval, for cadences the generator "
                        "refuses to guess")
    r.add_argument("--ilm-poll-interval", default="10s",
                   help="indices.lifecycle.poll_interval for the run; the "
                        "cluster default of 10m would stall every "
                        "short-age phase in the policy")
    r.add_argument("--docs-per-second", type=int, default=100,
                   help="ingest rate; drives how fast backing indices "
                        "fill and roll")
    r.add_argument("--doc-bytes", type=int, default=1024,
                   help="approximate payload size per document; drives "
                        "segment and snapshot sizes")
    r.add_argument("--shards", type=int, default=1,
                   help="primary shards per backing index; more shards "
                        "mean more shard directories and more blobs per "
                        "snapshot")
    r.add_argument("--rollover-max-age", default="10m",
                   help="age at which a backing index rolls over; with "
                        "max-docs this sets how often ILM manufactures a "
                        "new index")
    r.add_argument("--rollover-max-docs", type=int, default=100000,
                   help="document count that forces a rollover before "
                        "max age is reached")
    r.add_argument("--frozen-min-age", default="2m",
                   help="how long after rollover ILM converts an index "
                        "to a partial searchable snapshot; the mount "
                        "generator's clock")
    r.add_argument("--delete-min-age", default="20m",
                   help="how long after rollover ILM deletes the frozen "
                        "index and its searchable snapshot; must leave "
                        "the frozen phase time to finish")
    r.add_argument("--duration", default="1h",
                   help="how long the script ingests and reports; SLM "
                        "and ILM keep churning after it exits, until "
                        "teardown")
    r.add_argument("--report-interval", default="60s",
                   help="how often a report line is emitted to stdout "
                        "and the report file")
    r.add_argument("--report-file", metavar="PATH",
                   help="JSONL file the reports append to, the artifact "
                        "a later measurement cites; defaults to "
                        "<prefix>-reports.jsonl")

    s = sub.add_parser(
        "status", parents=[common],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        help="emit one report and exit; safe at any time")

    t = sub.add_parser(
        "teardown", parents=[common],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        help="remove what the script created, restore settings, verify")
    t.add_argument("--derive-from-prefix", action="store_true",
                   help="proceed without a state file, deriving every name "
                        "from --prefix. Read what teardown prints before "
                        "reaching for this: the derived names are ordinary "
                        "ones and may belong to a repository this harness "
                        "never created")
    t.add_argument("--purge-bucket", action="store_true",
                   help="also remove the leaked blobs under the base "
                        "path with single-object deletes, which succeed "
                        "even where the batch delete fails; without this "
                        "the leaked corpus survives for measurement")

    return p


def main():
    args = build_parser().parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9]{2,30}", args.prefix):
        die("--prefix must be 3 to 31 lowercase letters and digits and "
            "start with a letter, so every derived name stays legal")
    stream = getattr(args, "data_stream", None)
    if stream is not None and not re.fullmatch(r"[a-z][a-z0-9._-]{2,199}", stream):
        die("--data-stream must be 3 to 200 characters, start with a lowercase "
            "letter, and hold only lowercase letters, digits, dot, underscore "
            "or hyphen, because Elasticsearch will refuse anything else as an "
            "index name")
    if args.state_file is None:
        args.state_file = args.prefix + "-state.json"
    if getattr(args, "report_file", None) is None and args.cmd == "run":
        args.report_file = args.prefix + "-reports.jsonl"
    args.base_path_explicit = getattr(args, "base_path", None) is not None
    if not args.base_path_explicit:
        args.base_path = args.prefix

    password = None
    if args.password_file:
        with open(args.password_file) as f:
            password = f.read().strip()
    else:
        password = os.environ.get("ES_PASSWORD")
    es = Es(args.es, args.user, password, args.ca_cert, args.insecure)
    try:
        es.get("/")
    except EsError as e:
        die("cannot reach Elasticsearch at %s: %s" % (args.es, e))

    n = names(args.prefix, getattr(args, "data_stream", None))
    s3, s3_reason = make_s3(args)
    if args.cmd == "run":
        return cmd_run(es, args, n, s3, s3_reason)
    if args.cmd == "status":
        base_path = args.base_path
        if os.path.exists(args.state_file):
            with open(args.state_file) as f:
                state = json.load(f)
            n = state["names"]
            base_path = state.get("base_path", base_path)
        rep = gather_report(es, n, s3, s3_reason, base_path)
        print(json.dumps(rep, sort_keys=True, indent=2))
        return 0
    if args.cmd == "teardown":
        return cmd_teardown(es, args, n, s3, s3_reason)
    return 2


if __name__ == "__main__":
    sys.exit(main())
