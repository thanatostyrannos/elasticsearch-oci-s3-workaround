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

TLS. Certificate verification is on for every https endpoint this script
opens, the cluster and the object store alike, and no flag turns it off. A
lab cluster under ECK serves a certificate signed by a CA that lives only in
the cluster, so hand that CA to --ca-cert. One line writes it out:

  kubectl get secret <cluster>-es-http-certs-public \\
      -o jsonpath='{.data.ca\\.crt}' | base64 -d > ca.crt

Then pass --ca-cert ca.crt. TLS 1.0 and 1.1 are refused, because Python
before 3.10 still offers them.

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

If you would rather see it than read it, the diagrams are in
docs/engineering/architecture.md, which shows where this sits in the system
and how it is deployed, and docs/engineering/algorithms.md, which shows what
the audit does with what this manufactures. The security view, including
what this credential can do that the audit cannot, is in
docs/security/threat-model.md under mode 3.
"""

import argparse
import base64
import collections
import datetime
import hashlib
import hmac
import ipaddress
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET  # nosec B405 -- see refuse_doctype() below

# The audit already carries a refuse_doctype() guard for exactly this parse:
# a legitimate S3 listing never declares a DOCTYPE, and stdlib ElementTree
# expands internal entities, so a store able to answer with one can hang this
# process. Reused here rather than duplicated, on the sibling package this
# script already ships next to in the release archive.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generation_chain.sources.s3 import refuse_doctype  # noqa: E402

DEFAULT_PREFIX = "churnrig"

# An ECK cluster keeps the CA that signed its HTTP certificate in a secret
# next to it. Quoted in --help and in the message a failed lab connection
# prints, because an operator who cannot find this line goes looking for a
# way to skip verification instead.
ECK_CA_EXTRACTION = (
    "kubectl get secret <cluster>-es-http-certs-public "
    "-o jsonpath='{.data.ca\\.crt}' | base64 -d > ca.crt")

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


# --es and --s3-endpoint come from the command line, but a command line is
# not the same as a trusted value: it can be pasted wrong, templated from
# somewhere else, or wrong in a script that calls this one. urlopen does not
# care, and will happily open file:// or ftp://. Both clients below only ever
# need http or https, so anything else is refused at construction, before
# either client makes its first call.
_ALLOWED_URL_SCHEMES = ("http", "https")


def refuse_non_http_scheme(url, what):
    scheme = urllib.parse.urlsplit(url).scheme
    if scheme not in _ALLOWED_URL_SCHEMES:
        die(f"{what} is {url!r}; only http and https are accepted, so a "
            f"{scheme or '(no scheme)'!r} value cannot be opened")


# A host name or a bracketed IPv6 literal, with an optional port. An
# authority that does not match this one is not the plain host:port an
# endpoint should be: a user:password@ prefix is the shape that matters,
# since it puts a credential in every URL built from it.
_AUTHORITY_RE = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])"
    r"(?::\d{1,5})?")


def endpoint_origin(url, what):
    """The scheme and host of a command-line endpoint, or a refusal.

    An endpoint is scheme://host:port and nothing else. Whatever follows is
    refused rather than kept, so every path this script requests is one this
    file wrote, and a value with a path or a query in it fails at the flag
    that carried it instead of silently redirecting later requests.
    """
    url = url.strip()
    refuse_non_http_scheme(url, what)
    parts = urllib.parse.urlsplit(url)
    if not _AUTHORITY_RE.fullmatch(parts.netloc):
        die(f"{what} is {url!r}; the part after the scheme has to be a host "
            "and an optional port, with no credentials and no trailing "
            "punctuation")
    if parts.path.strip("/") or parts.query or parts.fragment:
        die(f"{what} is {url!r}; write it as scheme://host:port, because "
            "this harness supplies every path itself and would otherwise "
            "hang yours in front of each one")
    return f"{parts.scheme}://{parts.netloc}"


def describe_path(path):
    """A short phrase saying why a path is not the file it should be."""
    if not os.path.exists(path):
        return "does not exist"
    if os.path.isdir(path):
        return "is a directory"
    return "is not a regular file"


def resolve_input_file(path, what):
    """The resolved path of a file this script may read, or a refusal.

    Callers open what this returns rather than what they passed, so the file
    that was checked is the file that is read.
    """
    resolved = os.path.realpath(path)
    if not os.path.isfile(resolved):
        die(f"{what} {path!r} cannot be read: it resolves to {resolved!r}, "
            f"which {describe_path(resolved)}")
    return resolved


def resolve_output_file(path, what):
    """The resolved path of a file this script may write, or a refusal.

    An output path names something that does not exist yet, so there is
    nothing to check it against beyond whether writing it can work: the
    directory has to be there, and anything already sitting at the name has
    to be an ordinary file. Naming the flag and the resolved path beats a
    traceback from three frames down.
    """
    resolved = os.path.realpath(path)
    directory = os.path.dirname(resolved) or "."
    if not os.path.isdir(directory):
        die(f"{what} {path!r} cannot be written: it resolves to {resolved!r} "
            f"and its directory {directory!r} {describe_path(directory)}")
    if os.path.exists(resolved) and not os.path.isfile(resolved):
        die(f"{what} {path!r} cannot be written: it resolves to {resolved!r}, "
            f"which {describe_path(resolved)}")
    return resolved


# Names a lab cluster answers on. Kubernetes hands out the first three to
# in-cluster services, mDNS hands out .local, and a single-label name has no
# public DNS to resolve it.
LAB_HOST_SUFFIXES = (".svc", ".svc.cluster.local", ".cluster.local",
                     ".local", ".localdomain", ".internal")


def is_lab_host(host):
    """Is this an address only a lab or an in-cluster caller can reach?"""
    if not host:
        return False
    host = host.rstrip(".")
    if host == "localhost" or host.endswith(LAB_HOST_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A name with no dot has no public DNS to resolve it, so it is a
        # service name inside a cluster or a line in someone's hosts file.
        return "." not in host
    return address.is_loopback or address.is_private or address.is_link_local


def write_state(path, state):
    """Record what teardown will need, refusing the run if it cannot.

    A run whose state was never written leaves cluster settings changed with
    no record of what they were, so this stops before it starts rather than
    after.
    """
    try:
        with open(resolve_output_file(path, "--state-file"), "w") as handle:
            json.dump(state, handle, indent=2)
    except OSError as problem:
        die(f"state file {path!r} could not be written: "
            f"{problem.__class__.__name__}: {problem.strerror or problem}. "
            "Teardown restores cluster settings from this file, so the run "
            "stops rather than changing them with nothing to undo it.")


def load_state(path):
    """The state a previous run wrote, or a refusal naming what went wrong.

    A state file that will not parse is not the same as no state file at all.
    Falling back to prefix-derived names would restore no cluster settings
    and silently widen what teardown touches, so it refuses instead.
    """
    try:
        with open(resolve_input_file(path, "--state-file")) as handle:
            return json.load(handle)
    except (OSError, ValueError) as problem:
        die(f"state file {path!r} exists but could not be read: "
            f"{problem.__class__.__name__}: {problem}")


def missing_ca_hint(args):
    """What to try when a lab cluster will not verify, or nothing.

    An ECK cluster signs its own HTTP certificate, so the first connection to
    one fails until its CA is on disk. Printing the command that puts it
    there is the difference between an operator fixing the trust and an
    operator going looking for a way to skip it.
    """
    if args.ca_cert or not args.es.startswith("https:"):
        return ""
    host = urllib.parse.urlsplit(args.es).hostname
    if not is_lab_host(host):
        return ""
    return ("\nNo --ca-cert was given for lab host %s. If this is an ECK "
            "cluster serving a certificate it signed itself, write out the "
            "CA that signed it and pass it:\n  %s\n  --ca-cert ca.crt"
            % (host, ECK_CA_EXTRACTION))


def read_secret_file(path, what):
    """The one line in a secret file, or a refusal naming what would not open.

    The message quotes the path and never the contents, because the contents
    are the secret.
    """
    try:
        with open(resolve_input_file(path, what)) as handle:
            return handle.read().strip()
    except OSError as problem:
        die(f"{what} {path!r} could not be read: "
            f"{problem.__class__.__name__}: {problem.strerror or problem}")


# ---------------------------------------------------------------------------
# Elasticsearch client, urllib only


class EsError(Exception):
    def __init__(self, status, body, url):
        super().__init__("HTTP %s on %s: %s" % (status, url, body[:400]))
        self.status = status
        self.body = body


def tls_context(base, ca_cert):
    """How this client verifies an https endpoint, or None for plain http.

    Verification is always on. A cluster serving a certificate it signed
    itself is handled by naming the CA that signed it, not by agreeing to
    accept whichever host answered, so --ca-cert is the whole story. The
    floor is TLS 1.2 because Python before 3.10 still offers 1.0 and 1.1,
    and the ceiling is left alone: a cluster that speaks only 1.2 is
    ordinary.
    """
    if not base.startswith("https:"):
        return None
    cafile = resolve_input_file(ca_cert, "--ca-cert") if ca_cert else None
    try:
        context = ssl.create_default_context(cafile=cafile)
    except ssl.SSLError as problem:
        die(f"--ca-cert {ca_cert!r} is not a PEM certificate bundle "
            f"OpenSSL will load: {problem}")
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class Es:
    def __init__(self, base, user, password, ca_cert):
        self.base = endpoint_origin(base, "--es")
        self.origin = urllib.parse.urlsplit(self.base)
        self.headers = {"Content-Type": "application/json"}
        if user and password:
            tok = base64.b64encode(
                ("%s:%s" % (user, password)).encode()).decode()
            self.headers["Authorization"] = "Basic " + tok
        self.ctx = tls_context(self.base, ca_cert)

    def url_for(self, path):
        """The absolute URL for one of this file's request paths.

        The scheme and host were settled once, from --es, and are reused
        verbatim. Everything else comes from the path handed in here, which
        every caller in this file writes as a literal, so no caller can move
        a request to a host the operator did not name.
        """
        if not path.startswith("/"):
            die(f"request path {path!r} does not start with /, so the host "
                "it would reach is not the one --es named")
        route, _, query = path.partition("?")
        return urllib.parse.urlunsplit(
            (self.origin.scheme, self.origin.netloc, route, query, ""))

    def req(self, method, path, body=None, ok=(200, 201), timeout=60,
            ndjson=None):
        url = self.url_for(path)
        data = None
        headers = dict(self.headers)
        if ndjson is not None:
            data = ndjson.encode()
            headers["Content-Type"] = "application/x-ndjson"
        elif body is not None:
            data = json.dumps(body).encode()
        r = urllib.request.Request(url, data=data, method=method,
                                   headers=headers)
        # url_for() rebuilt this from the scheme and host endpoint_origin()
        # accepted in __init__ plus a path written in this file, so only
        # http and https, and only the named host, reach this call.
        try:
            with urllib.request.urlopen(  # nosec B310
                    r, timeout=timeout, context=self.ctx) as resp:
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
        refuse_non_http_scheme(self.endpoint, "--s3-endpoint")
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
        # refuse_non_http_scheme() checked self.endpoint in __init__; url is
        # self.endpoint plus a signed path this script builds, so only http
        # and https ever reach this call.
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:  # nosec B310
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
            refuse_doctype(body, "listing")
            # refuse_doctype() just above refuses any body carrying a
            # DOCTYPE, which is what makes entity expansion here possible;
            # this file never reaches fromstring without it.
            root = ET.fromstring(body)  # nosec B314
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
    if args.s3_secret_key_file:
        secret = read_secret_file(args.s3_secret_key_file,
                                  "--s3-secret-key-file")
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


ILM_POLL_INTERVAL = "indices.lifecycle.poll_interval"
SLM_RETENTION_SCHEDULE = "slm.retention_schedule"
SLM_MINIMUM_INTERVAL = "slm.minimum_interval"

MANAGED_SETTINGS = (ILM_POLL_INTERVAL, SLM_RETENTION_SCHEDULE,
                    SLM_MINIMUM_INTERVAL)

# The Elasticsearch paths this harness builds names onto. Named once so a
# typo fails every call that uses the path rather than leaving one of them
# quietly pointing at nothing.
ILM_POLICY_PATH = "/_ilm/policy/"
SLM_POLICY_PATH = "/_slm/policy/"
SNAPSHOT_PATH = "/_snapshot/"
INDEX_TEMPLATE_PATH = "/_index_template/"
DATA_STREAM_PATH = "/_data_stream/"
RESOLVE_PREFIX_PATH = "/_resolve/index/*%s*?expand_wildcards=all"
SNAPSHOTS_IN_REPO_PATH = SNAPSHOT_PATH + "%s/_all?ignore_unavailable=true"


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
    res = es.get_or_none(RESOLVE_PREFIX_PATH % prefix) or {}
    for kind in ("indices", "aliases", "data_streams"):
        for entry in res.get(kind, []):
            hits.append("%s %s" % (kind[:-1] if kind != "indices"
                                   else "index", entry["name"]))
    if es.get_or_none(ILM_POLICY_PATH + n["ilm"]):
        hits.append("ilm policy " + n["ilm"])
    if es.get_or_none(SLM_POLICY_PATH + n["slm"]):
        hits.append("slm policy " + n["slm"])
    if es.get_or_none(SNAPSHOT_PATH + n["repo"]):
        hits.append("snapshot repository " + n["repo"])
    if es.get_or_none(INDEX_TEMPLATE_PATH + n["template"]):
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
        ILM_POLL_INTERVAL: args.ilm_poll_interval,
        SLM_RETENTION_SCHEDULE: retention_cron(
            args.retention_check_interval),
    }
    min_interval_secs = parse_duration(args.snapshot_interval)
    prior_min = prior.get(SLM_MINIMUM_INTERVAL)
    effective_min = parse_duration(prior_min) if prior_min else 900
    if min_interval_secs < effective_min:
        wanted[SLM_MINIMUM_INTERVAL] = args.snapshot_interval

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
    write_state(args.state_file, state)
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
        es.put(SNAPSHOT_PATH + n["repo"], repo_body)
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
        es.put(SNAPSHOT_PATH + n["repo"] + "?verify=false", repo_body)
        write_state(args.state_file, state)

    es.put(ILM_POLICY_PATH + n["ilm"], {"policy": {"phases": {
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

    es.put(INDEX_TEMPLATE_PATH + n["template"], {
        "index_patterns": [n["data_stream"] + "*"],
        "data_stream": {},
        "priority": 500,
        "template": {"settings": {
            "index.number_of_shards": args.shards,
            "index.number_of_replicas": 0,
            "index.lifecycle.name": n["ilm"]}}})
    es.put(DATA_STREAM_PATH + n["data_stream"])
    log("data stream %s created (%d shard(s) per backing index)"
        % (n["data_stream"], args.shards))

    es.put(SLM_POLICY_PATH + n["slm"], {
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
           wanted[SLM_RETENTION_SCHEDULE]))
    return state


# ---------------------------------------------------------------------------
# reporting


# The cluster, the object store and the names one report is about, carried
# together because every section of a report needs some of them and none of
# them means anything without the others.
Rig = collections.namedtuple("Rig", "es names s3 s3_reason base_path")


def snap_name(s):
    """The listing API names a snapshot under the key snapshot; older
    responses used name. Accept both so one field rename cannot blind
    every report."""
    return s.get("snapshot") or s.get("name") or ""


def data_stream_section(rig):
    """Backing index count, names and generation, or None if it is gone."""
    ds = rig.es.get_or_none(DATA_STREAM_PATH + rig.names["data_stream"])
    if not (ds and ds.get("data_streams")):
        return None
    d = ds["data_streams"][0]
    return {"backing_indices": len(d["indices"]),
            "backing_index_names": [i["index_name"] for i in d["indices"]],
            "generation": d["generation"]}


def ilm_section(rig):
    """Backing indices counted by ILM phase, and any index stuck on ERROR."""
    phases = {}
    errors = []
    explain = rig.es.get_or_none(
        "/%s/_ilm/explain" % rig.names["data_stream"])
    for idx, info in (explain or {}).get("indices", {}).items():
        phase = info.get("phase", "unmanaged")
        phases[phase] = phases.get(phase, 0) + 1
        if info.get("step") == "ERROR":
            errors.append({"index": idx,
                           "failed_step": info.get("failed_step")})
    return phases, errors


def mounted_indices(rig, prefix):
    """Every index under the prefix that reads from a searchable snapshot."""
    settings = rig.es.get_or_none(
        "/*%s*/_settings?expand_wildcards=all&filter_path="
        "*.settings.index.store.snapshot" % prefix) or {}
    mounted = []
    for idx, body in settings.items():
        snap = (body.get("settings", {}).get("index", {})
                .get("store", {}).get("snapshot", {}))
        if snap:
            mounted.append({"index": idx,
                            "snapshot": snap.get("snapshot_name"),
                            "repository": snap.get("repository_name"),
                            "partial": snap.get("partial")})
    return mounted


def mount_hazards(mounted, repo, alive_names):
    """Mounts reading from a snapshot the repository no longer lists.

    Reported, never prevented: an index serving reads out of blobs no
    snapshot references any more is one of the states the reclaim tooling is
    measured against.
    """
    return [{"index": m["index"], "snapshot": m["snapshot"],
             "reason": "mounted searchable snapshot whose source snapshot "
                       "is no longer in the repository"}
            for m in mounted
            if m["repository"] == repo and m["snapshot"] not in alive_names]


def observed_start_deltas(rig, alive, cadence_memory):
    """Seconds between consecutive snapshot starts, newest eight.

    `cadence_memory` carries starts the repository has since expired, so the
    cadence a long run reports is not truncated to the retention window.
    """
    ours = [s for s in alive if snap_name(s).startswith(rig.names["snap_prefix"])]
    starts = sorted(s["start_time_in_millis"] for s in ours)
    if cadence_memory is not None:
        for s in ours:
            cadence_memory[snap_name(s)] = s["start_time_in_millis"]
        starts = sorted(cadence_memory.values())
    return [round((b - a) / 1000.0, 1) for a, b in zip(starts, starts[1:])][-8:]


def snapshot_section(rig, alive, cadence_memory):
    """Snapshot counts alive and by state, plus what SLM says it has done."""
    slm = rig.es.get_or_none(SLM_POLICY_PATH + rig.names["slm"]) or {}
    stats = slm.get(rig.names["slm"], {}).get("stats", {})
    global_stats = rig.es.get_or_none("/_slm/stats") or {}
    by_state = {}
    for s in alive:
        by_state[s["state"]] = by_state.get(s["state"], 0) + 1
    return {
        "alive": len(alive),
        "alive_by_state": by_state,
        "taken_total": stats.get("snapshots_taken", 0),
        "failed_total": stats.get("snapshots_failed", 0),
        "expired_total": stats.get("snapshots_deleted", 0),
        "snapshot_deletion_failures": stats.get(
            "snapshot_deletion_failures", 0),
        "retention_runs": global_stats.get("retention_runs"),
        "retention_failed": global_stats.get("retention_failed"),
        "observed_start_deltas_s": observed_start_deltas(
            rig, alive, cadence_memory),
    }


_ROOT_GENERATION = re.compile(r"index-(\d+)")


def repository_section(rig, alive_uuids):
    """What the bucket holds under the base path, counted by blob kind.

    Only the root of the base path is counted. A key with a slash left in it
    after the prefix belongs to a shard directory, and this section is about
    the repository's own top-level metadata.
    """
    base = rig.base_path.rstrip("/") + "/" if rig.base_path else ""
    objects = rig.s3.list(base)
    generations = []
    snapshot_blobs = []
    latest = False
    for key, _size in objects:
        rel = key[len(base):]
        if "/" in rel:
            continue
        generation = _ROOT_GENERATION.fullmatch(rel)
        if generation:
            generations.append(int(generation.group(1)))
        elif rel == "index.latest":
            latest = True
        elif rel.startswith("snap-") and rel.endswith(".dat"):
            snapshot_blobs.append(rel[5:-4])
    leaked = [uuid for uuid in snapshot_blobs if uuid not in alive_uuids]
    return {
        "object_count": len(objects),
        "bytes": sum(size for _key, size in objects),
        "root_generation_count": len(generations),
        "root_generations": sorted(generations)[-8:],
        "index_latest_present": latest,
        "snapshot_metadata_blobs": len(snapshot_blobs),
        "expired_snapshot_metadata_still_present": len(leaked),
    }


def gather_report(rig, cadence_memory=None):
    """One JSON-shaped report of everything the harness can currently see."""
    prefix = rig.names["data_stream"].rsplit("-", 1)[0]
    rep = {"ts": now_iso(), "prefix": prefix}
    rep["data_stream"] = data_stream_section(rig)
    rep["by_ilm_phase"], rep["ilm_errors"] = ilm_section(rig)

    mounted = mounted_indices(rig, prefix)
    snaps = rig.es.get_or_none(SNAPSHOTS_IN_REPO_PATH % rig.names["repo"])
    alive = snaps.get("snapshots", []) if snaps else []
    rep["mounted_searchable"] = {
        "count": len(mounted), "indices": mounted,
        "hazards": mount_hazards(mounted, rig.names["repo"],
                                 {snap_name(s) for s in alive})}
    rep["snapshots"] = snapshot_section(rig, alive, cadence_memory)

    if rig.s3 is None:
        rep["repository"] = {"unavailable": rig.s3_reason}
    else:
        rep["repository"] = repository_section(
            rig, {s.get("uuid") for s in alive})
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


class RunState:
    """Everything one `run` accumulates while the environment churns.

    The counters live here rather than in closures over cmd_run because the
    report writer, the milestone check and the ingest loop all read and write
    the same three numbers, and a closure hid which of them owned each one.
    """

    def __init__(self, rig, args):
        self.rig = rig
        self.args = args
        self.started = time.monotonic()
        self.docs_sent = 0
        self.bulk_errors = 0
        self.seq = 0
        self.milestones = {}
        self.cadence_memory = {}
        self.seen_backing = set()

    @property
    def elapsed(self):
        return time.monotonic() - self.started

    def ingest(self):
        """One second of documents, counted whether or not they land."""
        rate = self.args.docs_per_second
        lines = []
        for _ in range(rate):
            self.seq += 1
            lines.append('{"create":{}}')
            lines.append(json.dumps(make_doc(self.seq, self.args.doc_bytes)))
        if not lines:
            return
        try:
            _, resp = self.rig.es.req(
                "POST", "/%s/_bulk" % self.rig.names["data_stream"],
                ndjson="\n".join(lines) + "\n", timeout=60)
        except EsError as e:
            self.bulk_errors += rate
            log("bulk failed: %s" % e)
            return
        self.docs_sent += rate
        if resp.get("errors"):
            self.bulk_errors += sum(1 for item in resp["items"]
                                    if item["create"].get("status", 201) >= 300)

    def note_backing_index_loss(self, rep):
        """Mark the report when a backing index this run saw has gone.

        A frozen mount renames .ds-X to partial-.ds-X, so the comparison is
        on canonical names; otherwise every mount would count as a deletion.
        """
        current = {name[len("partial-"):] if name.startswith("partial-")
                   else name
                   for name in (rep.get("data_stream") or {})
                   .get("backing_index_names", [])}
        if self.seen_backing - current:
            rep["backing_index_disappeared"] = True
        self.seen_backing |= current

    def note_milestones(self, rep, now):
        """Record the first time each milestone's condition holds."""
        for name, reached in MILESTONES:
            if name not in self.milestones and reached(rep):
                self.milestones[name] = now
                log("milestone at %ds: %s" % (int(now), name))

    def observe(self, now):
        """One observation pass: gather, mark what changed, log hazards."""
        rep = gather_report(self.rig, self.cadence_memory)
        self.note_backing_index_loss(rep)
        self.note_milestones(rep, now)
        for hazard in rep["mounted_searchable"]["hazards"]:
            log("HAZARD: %s (index=%s snapshot=%s)"
                % (hazard["reason"], hazard["index"], hazard["snapshot"]))
        return rep

    def emit(self, rep):
        """Write one report to stdout and append it to the report file."""
        rep["elapsed_s"] = round(self.elapsed, 1)
        rep["ingest"] = {"docs_sent": self.docs_sent,
                         "bulk_errors": self.bulk_errors}
        rep["milestones"] = {k: round(v, 1)
                             for k, v in sorted(self.milestones.items(),
                                                key=lambda kv: kv[1])}
        line = json.dumps(rep, sort_keys=True)
        print(line, flush=True)
        report_file = resolve_output_file(self.args.report_file,
                                          "--report-file")
        with open(report_file, "a") as f:
            f.write(line + "\n")
        return rep


def churn(state, duration, report_every):
    """Ingest and observe until the duration ends or someone interrupts."""
    poll_every = min(report_every, 20)
    next_report = 0.0
    next_poll = 0.0
    while state.elapsed < duration:
        tick = time.monotonic()
        state.ingest()
        now = state.elapsed
        if now >= next_poll:
            next_poll = now + poll_every
            try:
                rep = state.observe(now)
                if now >= next_report:
                    next_report = now + report_every
                    state.emit(rep)
            except Exception as e:
                log("report poll failed, continuing: %s: %s"
                    % (type(e).__name__, e))
        spent = time.monotonic() - tick
        if spent < 1.0:
            time.sleep(1.0 - spent)


def cmd_run(es, args, n, s3, s3_reason):
    if os.path.exists(args.state_file):
        die("state file %s already exists; a previous run was not torn "
            "down. Run teardown first, or point --state-file elsewhere "
            "and pick a fresh --prefix" % args.state_file)
    setup = cmd_setup(es, args, n, s3)
    state = RunState(Rig(es, n, s3, s3_reason, setup["base_path"]), args)
    log("run started: duration=%s ingest=%d docs/s of ~%d bytes; "
        "reports every %s to stdout and %s"
        % (args.duration, args.docs_per_second, args.doc_bytes,
           args.report_interval, args.report_file))
    try:
        churn(state, parse_duration(args.duration),
              parse_duration(args.report_interval))
    except KeyboardInterrupt:
        log("interrupted, emitting final report")
    final = state.emit(gather_report(state.rig, state.cadence_memory))
    log("run finished after %ss. The environment keeps churning until "
        "teardown; that standing churn is the measurement target."
        % final["elapsed_s"])
    return 0


# ---------------------------------------------------------------------------
# teardown


def delete_cluster_objects(es, args, n):
    """Remove exactly what this harness created on the cluster."""
    es.delete(SLM_POLICY_PATH + n["slm"])
    log("slm policy %s deleted" % n["slm"])

    es.delete(DATA_STREAM_PATH + n["data_stream"])
    res = es.get_or_none(RESOLVE_PREFIX_PATH % args.prefix) or {}
    for name in teardown_index_scope(res, n["data_stream"]):
        es.delete("/" + name)
        log("leftover index %s deleted" % name)
    log("data stream %s deleted" % n["data_stream"])

    es.delete(INDEX_TEMPLATE_PATH + n["template"])
    es.delete(ILM_POLICY_PATH + n["ilm"])
    log("index template and ilm policy deleted")

    snaps = es.get_or_none(SNAPSHOTS_IN_REPO_PATH % n["repo"])
    if not snaps:
        return
    names_list = [snap_name(s) for s in snaps.get("snapshots", [])]
    for i in range(0, len(names_list), 10):
        batch = ",".join(names_list[i:i + 10])
        es.delete("%s%s/%s" % (SNAPSHOT_PATH, n["repo"], batch), timeout=600)
    log("%d snapshot(s) deleted from %s (each delete leaks blobs when "
        "the store rejects batch deletes; that is the state under "
        "test)" % (len(names_list), n["repo"]))
    es.delete(SNAPSHOT_PATH + n["repo"])
    log("repository %s unregistered" % n["repo"])


def restore_managed_settings(es, state):
    """Put back exactly the values recorded before this harness changed them."""
    restore = {key: state["prior_settings"].get(key)
               for key in MANAGED_SETTINGS
               if key in state["settings_changed"]}
    es.put("/_cluster/settings", {"persistent": restore})
    log("cluster settings restored to recorded prior values: %s"
        % json.dumps(restore))


def clear_bucket(s3, base_path, purge):
    """Count what the failed deletes left behind, and remove it if asked.

    Returns (purged, leftover). Without --purge-bucket nothing is deleted:
    the leaked corpus is the measurement target, so removing it is opt-in.
    """
    base = base_path.rstrip("/") + "/" if base_path else ""
    objects = s3.list(base)
    if not purge:
        leftover = len(objects)
        if leftover:
            log("%d object(s) remain under %s/%s: the blobs Elasticsearch "
                "failed to delete. Rerun teardown with --purge-bucket to "
                "remove them, or keep them as a measurement corpus"
                % (leftover, s3.bucket, base))
        return 0, leftover
    for key, _size in objects:
        s3.delete_object(key)
    leftover = len(s3.list(base))
    log("purged %d leaked object(s) under %s/%s via single-object "
        "deletes; %d remain" % (len(objects), s3.bucket, base, leftover))
    return len(objects), leftover


def surviving_cluster_objects(es, args, n):
    """Anything answering to the prefix after teardown has run."""
    res = es.get_or_none(RESOLVE_PREFIX_PATH % args.prefix) or {}
    remaining = [e["name"] for kind in ("indices", "aliases", "data_streams")
                 for e in res.get(kind, [])]
    for path, label in ((ILM_POLICY_PATH + n["ilm"], "ilm policy"),
                        (SLM_POLICY_PATH + n["slm"], "slm policy"),
                        (SNAPSHOT_PATH + n["repo"], "repository"),
                        (INDEX_TEMPLATE_PATH + n["template"], "template")):
        if es.get_or_none(path):
            remaining.append(label)
    return remaining


def settings_not_restored(es, state):
    """Managed settings whose current value is not the one recorded."""
    current = read_prior_settings(es)
    return {key: {"expected": state["prior_settings"].get(key),
                  "actual": current.get(key)}
            for key in MANAGED_SETTINGS
            if key in state["settings_changed"]
            and current.get(key) != state["prior_settings"].get(key)}


def teardown_verdict(es, args, n, state):
    """Whether teardown left the cluster as it found it, and what it did not."""
    verdict = {"ts": now_iso(), "prefix": args.prefix, "clean": True}
    remaining = surviving_cluster_objects(es, args, n)
    if remaining:
        verdict["clean"] = False
        verdict["remaining"] = remaining
    if state:
        mismatched = settings_not_restored(es, state)
        if mismatched:
            verdict["clean"] = False
            verdict["settings_mismatch"] = mismatched
    return verdict


def cmd_teardown(es, args, n, s3, s3_reason):
    state = None
    if os.path.exists(args.state_file):
        state = load_state(args.state_file)
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

    delete_cluster_objects(es, args, n)
    if state:
        restore_managed_settings(es, state)

    purged, leftover = 0, None
    if s3 is not None:
        purged, leftover = clear_bucket(s3, base_path, args.purge_bucket)
    else:
        log("bucket state not checked (%s)" % s3_reason)

    verdict = teardown_verdict(es, args, n, state)
    verdict["leftover_bucket_objects"] = leftover
    verdict["purged"] = purged
    print(json.dumps(verdict, sort_keys=True), flush=True)
    if not verdict["clean"]:
        log("teardown left residue, state file kept; see the verdict above")
        return 1
    if state:
        remove_state_file(args.state_file)
    return 0


def remove_state_file(path):
    """Delete the state file teardown has finished with, or say why not.

    The run is already done and its verdict already printed, so a state file
    that will not go away is worth a line and not an exit code. It is
    resolved before it is removed, and the resolved path is what gets
    removed, so this deletes the file that was checked.
    """
    resolved = os.path.realpath(path)
    if not os.path.isfile(resolved):
        log(f"teardown verified clean, but --state-file {path!r} resolves "
            f"to {resolved!r}, which {describe_path(resolved)}; nothing was "
            "removed")
        return
    try:
        os.unlink(resolved)
        log("teardown verified clean; state file removed")
    except OSError as problem:
        log(f"teardown verified clean, but the state file {path!r} could "
            f"not be removed ({problem.__class__.__name__}); delete it "
            "before the next run, which refuses to start while it exists")


# ---------------------------------------------------------------------------
# main


class Parser(argparse.ArgumentParser):
    """argparse, with one extra sentence when --insecure turns up.

    This harness verifies certificates and has no switch for turning that
    off, so an operator arriving with --insecure in a saved command line
    needs pointing at --ca-cert. A bare "unrecognized arguments" points them
    at the source to add the flag back instead.
    """

    def error(self, message):
        if "--insecure" in message:
            message += (". TLS verification is always on. A lab cluster "
                        "serving a certificate it signed itself is reached "
                        "by trusting the CA that signed it: " +
                        ECK_CA_EXTRACTION + " ... then --ca-cert ca.crt")
        super().error(message)


def build_parser():
    p = Parser(
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
                   help="PEM bundle holding the CA that signed the https "
                        "endpoint's certificate. Verification is always on "
                        "and there is no flag that turns it off, so this is "
                        "how a lab cluster serving its own certificate is "
                        "reached. Under ECK the CA comes out with: "
                        + ECK_CA_EXTRACTION)
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


def check_arguments(args):
    """Refuse a name the cluster would reject, and fill in what was omitted.

    Every default here is derived from --prefix, so a run that names only its
    prefix still keeps its state, its reports and its base path apart from
    anyone else's.
    """
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


def cmd_status(es, args, n, s3, s3_reason):
    """Report on the rig as it stands, preferring the state file's names.

    A run that has already set up recorded what it created, and those names
    are what a report should describe. Falling back to the names derived from
    --prefix keeps status useful before setup has run.
    """
    base_path = args.base_path
    if os.path.exists(args.state_file):
        state = load_state(args.state_file)
        n = state["names"]
        base_path = state.get("base_path", base_path)
    report = gather_report(Rig(es, n, s3, s3_reason, base_path))
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


def main():
    args = build_parser().parse_args()
    check_arguments(args)

    if args.password_file:
        password = read_secret_file(args.password_file, "--password-file")
    else:
        password = os.environ.get("ES_PASSWORD")
    es = Es(args.es, args.user, password, args.ca_cert)
    try:
        es.get("/")
    except EsError as e:
        die("cannot reach Elasticsearch at %s: %s%s"
            % (args.es, e, missing_ca_hint(args)))

    n = names(args.prefix, getattr(args, "data_stream", None))
    s3, s3_reason = make_s3(args)
    if args.cmd == "run":
        return cmd_run(es, args, n, s3, s3_reason)
    if args.cmd == "status":
        return cmd_status(es, args, n, s3, s3_reason)
    if args.cmd == "teardown":
        return cmd_teardown(es, args, n, s3, s3_reason)
    return 2


if __name__ == "__main__":
    sys.exit(main())
