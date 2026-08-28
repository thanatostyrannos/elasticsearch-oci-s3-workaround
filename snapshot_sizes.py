#!/usr/bin/env python3
"""
snapshot_sizes.py: per-day/week/month snapshot size report for ES 8.x/9.x.

Uses GET _snapshot/<repo>/<names>/_status and aggregates:
  * incremental bytes: new data each snapshot uploaded. Dedup-aware, so this
    is your real repository growth for the period.
  * total bytes: size of all files a snapshot references. Do NOT sum these
    across snapshots; shared segments are counted once per snapshot.

Note: _status on completed snapshots reads shard-level metadata from the
repository, so it costs one metadata read per shard per snapshot. Fine for
daily snapshots; use --batch to tune request sizes, and don't cron it every
minute against a repo with thousands of snapshots.

Usage:
  ./snapshot_sizes.py --es https://es:9200 --repo my-repo --group day
  ./snapshot_sizes.py --es https://es:9200 --repo my-repo --group week \
      --user elastic:changeme --ca-cert /path/ca.crt
  ./snapshot_sizes.py ... --api-key <base64-id:key>   # ApiKey auth
  ./snapshot_sizes.py ... --recommend                  # add sizing section
  ./snapshot_sizes.py ... --recommend --retention-days 10
  ./snapshot_sizes.py ... --split-frozen                # class-aware report
  ./snapshot_sizes.py ... --split-frozen --recommend    # slm-only sizing

--recommend appends a "Repository sizing recommendation" section:
baseline full (largest snapshot total) + retention_days x median of the
per-calendar-day incremental sums + 1x baseline headroom for an
upgrade-day full snapshot, plus the same figure with a +20% operational
margin; a p95-based conservative variant is printed alongside. Only
SUCCESS/PARTIAL snapshots feed the numbers (PARTIAL with a warning).
If the repository backs searchable snapshots (frozen tier), the baseline
undercounts. The printed warning explains why.
--retention-days must be 5..10 (site policy: 5-10 days max).

--split-frozen (default off) separates the two snapshot populations that
otherwise get averaged together:
  * slm            SLM-created backup snapshots (they carry metadata.policy).
                   These are the repository's real GROWTH. Shards already
                   mounted as searchable snapshots upload ZERO bytes here.
  * frozen-pinned  Snapshots pinned by a mounted searchable-snapshot index
                   (discovered from index.store.snapshot settings). These are
                   a footprint FLOOR, not growth. Partial mounts are the
                   frozen tier (shared_cache); full mounts are the cold tier.
                   A snapshot that is both SLM-created and mounted is
                   labelled slm+mounted and classed frozen-pinned.
  * other          Neither of the above: manual snapshots, ILM-orphaned mounts.
With --split-frozen the per-period table gains a class column and a class
summary, and --recommend derives baseline+growth from the slm class ONLY,
adding the frozen footprint as its own additive term. If the discovery
fetches fail the script falls back to the unsplit report (original warning
included) and says why.

--split-frozen also prints a loud DANGER warning if a mounted index pins a
snapshot that is NOT in the repository listing. Elasticsearch
does not block deleting a snapshot that still backs a mounted
searchable-snapshot index (only repository UNREGISTRATION checks mounts), so
this state is reachable: the index keeps serving reads from blobs no snapshot
references any more. On a repository whose deletes leak (the Amazon S3
Compatibility API bug) those blobs survive and a reachability sweeper will
classify them ORPHAN.

--emit-mounted (needs only --es/--repo/auth) prints the pinned-snapshot set to
stdout as tab-separated values, one line per snapshot, columns:

  snapshot_name | snapshot_uuid (or '-') | partial|full | mounting index/indices

This file is the set of snapshots nobody may delete, by any means, while those
indices are mounted. It was written as a pre-flight input for the reachability
sweepers, which are retired and removed; whatever reads a repository next
should read it the same way, first whitespace-separated token per line.

  ./snapshot_sizes.py --es https://es:9200 --repo my-repo --emit-mounted
      --out mounted.txt

--emit-classified (needs --es/--repo/auth) writes ONE tab-separated row per
snapshot in the repository, header row first, columns:

  snapshot | class | policy | tier | mounted_by | state | start_time_utc |
  incremental_bytes | total_bytes

It runs the same discovery fetches as --split-frozen plus the _status pass, so
it carries both populations the report separates:
  * class            the --split-frozen label: slm, frozen-pinned, slm+mounted
                     (a label; it buckets as frozen-pinned wherever counts or
                     a --class filter are taken), or other
  * policy           the SLM policy id, or '-'
  * tier             partial | full | '-'  (partial wins, as everywhere else)
  * mounted_by       comma-joined mounting index names, or '-'
  * state            the _status state (SUCCESS / PARTIAL / IN_PROGRESS / ...)
  * start_time_utc   ISO-8601 UTC, or '-' when the snapshot has no start stamp
Rows are sorted by start time, then by name.

A snapshot that a mounted index pins but the repository listing no longer
contains (the DANGER state above) STILL gets a row: class frozen-pinned, state
MISSING-FROM-CATALOG, sizes '-', so the export is complete. The DANGER banner
still goes to stderr. If either discovery fetch fails the export aborts with
exit 1 rather than write an incomplete file. A partial classified export is
worse than none.

--class NAMES restricts the exported rows to a comma-separated subset of the
accounting classes (slm, frozen-pinned, other; default: all). The stderr
summary always reports every class count plus a "filtered to:" line, so a
subset file is never mistaken for the whole repository. The DANGER banner
prints regardless of the filter, but MISSING-FROM-CATALOG rows are
frozen-pinned and follow it.

--out FILE sends an emit mode's machine-readable output to FILE instead of
stdout (human diagnostics stay on stderr either way). It applies to both
--emit-mounted and --emit-classified, and without an emit mode it is rejected:
the report tables are for humans, not for silent redirection into a file.
--emit-mounted and --emit-classified are mutually exclusive.

  ./snapshot_sizes.py --es https://es:9200 --repo my-repo \
      --emit-classified --out snapshots.tsv
  ./snapshot_sizes.py --es https://es:9200 --repo my-repo \
      --emit-classified --class slm,other --out backups-only.tsv
"""

from __future__ import annotations

import argparse
import base64
import collections
import datetime as dt
import ipaddress
import json
import math
import ssl
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request


# Names a lab cluster answers on. Kubernetes hands out the first three to
# in-cluster services, mDNS hands out .local, and a single-label name has no
# public DNS to resolve it.
LAB_HOST_SUFFIXES = (".svc", ".svc.cluster.local", ".cluster.local",
                     ".local", ".localdomain", ".internal")


def is_lab_host(host: str) -> bool:
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


def tls_context(args: argparse.Namespace):
    """The one TLS context this run uses, for the one endpoint it was given.

    Verification starts on and is turned off only by --insecure, which main()
    has already refused for anything but a lab address. Built once, here,
    rather than per request, so the relaxed context belongs to the endpoint
    the operator named and cannot end up on some other connection.
    """
    if not args.es.startswith("https"):
        return None
    ctx = ssl.create_default_context(cafile=args.ca_cert)
    if args.insecure:
        print(f"# TLS verification is OFF for "
              f"{urllib.parse.urlsplit(args.es).hostname}, by --insecure",
              file=sys.stderr)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def http_get(path: str, args: argparse.Namespace) -> dict:
    """GET one path from the cluster --es names.

    A path, never a whole URL. The host is the one the operator gave and
    nothing passed in here can move the request to a different one.
    """
    req = urllib.request.Request(args.es + path)
    if args.user:
        tok = base64.b64encode(args.user.encode()).decode()
        req.add_header("Authorization", f"Basic {tok}")
    elif args.api_key:
        req.add_header("Authorization", f"ApiKey {args.api_key}")
    with urllib.request.urlopen(  # nosec B310
            req, context=getattr(args, "tls", None), timeout=120) as r:
        return json.load(r)


def fmt(n: float) -> str:
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or u == "TiB":
            return f"{n:,.1f} {u}"
        n /= 1024
    raise AssertionError("unreachable: TiB is the terminal unit")


def period_key(ms: int, group: str) -> str:
    d = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)
    if group == "day":
        return d.strftime("%Y-%m-%d")
    if group == "week":
        iso = d.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return d.strftime("%Y-%m")


def p95(values: list[int]) -> float:
    """Nearest-rank 95th percentile (upper-biased for small samples)."""
    vs = sorted(values)
    return float(vs[max(0, math.ceil(0.95 * len(vs)) - 1)])


# --- snapshot-class separation (--split-frozen) -----------------------------
#
# TELLING BACKUPS APART FROM MOUNTED (STORAGE) SNAPSHOTS
# ------------------------------------------------------
# Two authoritative signals. No name heuristics.
#
#   * BACKUPS: SLM stamps its policy id into snapshot metadata at creation
#     (`metadata.policy`, read via GET _snapshot/<repo>/*). A snapshot with a
#     policy is a backup the retention machinery owns.
#   * MOUNTED: every searchable-snapshot index records the snapshot backing it
#     in its own index settings (`index.store.snapshot.snapshot_name` /
#     `repository_name` / `partial`, read via GET */_settings). If any index
#     names a snapshot there, that snapshot is storage a live index reads at
#     query time, whatever it is called and whoever created it.
#     `partial: true` = frozen tier (shared_cache); absent/false = cold tier.
#
# A snapshot matching BOTH signals (someone mounted a policy snapshot) is
# labelled slm+mounted but BUCKETS as frozen-pinned. Treating it as a backup
# would count its total as growth and, worse, imply retention may reap it,
# which would destroy the mounted index (ES does not block that deletion).

CLASS_SLM = "slm"
CLASS_FROZEN = "frozen-pinned"
CLASS_BOTH = "slm+mounted"       # a label, not a bucket: buckets to frozen
CLASS_OTHER = "other"
CLASS_ORDER = (CLASS_SLM, CLASS_FROZEN, CLASS_OTHER)

FROZEN_FOOTPRINT_LABEL = (
    "frozen footprint (pinned mount snapshots; totals may overlap if "
    "mounts share lineage)"
)

# Same shape as the existing http_get call sites, plus ValueError for a body
# that is not JSON (json.JSONDecodeError subclasses ValueError).
FETCH_ERRORS = (urllib.error.URLError, OSError, ssl.SSLError, ValueError)


def index_store_snapshot(settings_body: dict | None) -> dict | None:
    """The index.store.snapshot subtree of one index, if it has one."""
    store = (((settings_body or {}).get("settings") or {})
             .get("index") or {}).get("store") or {}
    snap = store.get("snapshot")
    return snap if isinstance(snap, dict) else None


def is_partial_mount(snap: dict) -> bool:
    """True for a frozen-tier shared_cache mount, false for a cold-tier copy."""
    return str(snap.get("partial", "false")).lower() == "true"


def fetch_mounted_set(args: argparse.Namespace) -> dict[str, dict]:
    """Discover snapshots pinned by mounted searchable-snapshot indices.

    Every mounted index carries index.store.snapshot.{repository_name,
    snapshot_name,snapshot_uuid,partial}. `partial` is "true" for frozen-tier
    shared_cache mounts and absent/false for cold-tier full-copy mounts. The
    filter_path below selects the whole index.store.snapshot subtree, so
    snapshot_uuid arrives with it and needs no extra request or filter term.

    Returns {snapshot_name: {"partial": bool, "full": bool, "indices": [...],
    "uuid": str|None}} restricted to snapshots in args.repo. A snapshot backing
    both a partial and a full mount reports both flags true.
    """
    data = http_get(
        "/*/_settings?filter_path=*.settings.index.store.snapshot", args)
    mounted: dict[str, dict] = {}
    for index, body in (data or {}).items():
        snap = index_store_snapshot(body)
        name = snap.get("snapshot_name") if snap else None
        if not name or snap.get("repository_name") != args.repo:
            continue
        entry = mounted.setdefault(
            name, {"partial": False, "full": False, "indices": [],
                   "uuid": None})
        tier = "partial" if is_partial_mount(snap) else "full"
        entry[tier] = True
        entry["indices"].append(index)
        if not entry["uuid"]:
            entry["uuid"] = snap.get("snapshot_uuid") or None
    return mounted


def open_emit_sink(args: argparse.Namespace):
    """Where an emit mode's machine-readable output goes.

    Returns (file, close_it). Without --out that is stdout, so the old
    `--emit-mounted > file.txt` pipe still works. `getattr` rather than
    attribute access keeps the emit functions callable with a Namespace that
    predates the flag. Raises OSError if FILE cannot be opened; the caller
    reports it.
    """
    path = getattr(args, "out", None)
    if not path:
        return sys.stdout, False
    return open(path, "w", encoding="utf-8"), True


def emit_mounted(args: argparse.Namespace) -> int:
    """Print the snapshots pinned by mounted searchable-snapshot indices.

    One tab-separated line per snapshot on stdout (or on --out FILE):
      snapshot_name, snapshot_uuid (or '-'), 'partial'|'full', mounting indices

    Diagnostics go to stderr so the machine-readable stream stays clean. This
    The consumer reads the FIRST whitespace-separated token of each line. The
    tools that consumed it are retired; the format is kept because the set it
    names is a fact about the cluster rather than about any tool.

    A snapshot backing both a partial and a full mount reports 'partial': the
    frozen-tier (shared_cache) reading is the conservative one, matching how
    split_totals() buckets it.
    """
    # Resolve the repository first. A name that does not exist answers this
    # question with an empty list rather than an error, because the mount
    # discovery below filters on repository_name and simply matches nothing.
    # An empty list is what a passed gate looks like to anything consuming
    # this file, and a `> mounted.txt` redirect creates the file whatever
    # happens, so a single wrong character in a repository name disarms the
    # mounted-snapshot check and reports success. Elasticsearch forces index
    # names lowercase and repository names are not, which is exactly how the
    # wrong character gets typed.
    try:
        registered = http_get(f"/_snapshot/{args.repo}", args)
    except FETCH_ERRORS as e:
        print(f"repository {args.repo!r} could not be resolved: {e}\n"
              f"List what exists with: GET {args.es}/_snapshot/_all\n"
              f"Repository names are case sensitive and are not forced "
              f"lowercase the way index names are.",
              file=sys.stderr)
        return 1
    if not isinstance(registered, dict) or args.repo not in registered:
        print(f"repository {args.repo!r} is not registered on this cluster "
              f"(the answer named {sorted(registered) if isinstance(registered, dict) else registered}).\n"
              f"List what exists with: GET {args.es}/_snapshot/_all\n"
              f"Refusing to print an empty pinned-snapshot list, because an "
              f"empty list is indistinguishable from a repository with no "
              f"mounted searchable snapshots, and anything that reads this "
              f"file would treat it as a passed check.",
              file=sys.stderr)
        return 1

    try:
        mounted = fetch_mounted_set(args)
    except FETCH_ERRORS as e:
        print(f"mounted-index discovery (_settings) failed: {e} "
              f"(check the URL, --user/--api-key and --ca-cert/--insecure)",
              file=sys.stderr)
        return 1
    try:
        sink, close_it = open_emit_sink(args)
    except OSError as e:
        print(f"cannot open --out file for writing: {e}", file=sys.stderr)
        return 1
    repo_uuid = (registered.get(args.repo) or {}).get("uuid")
    try:
        # Provenance, for whatever reads this back: a file generated against
        # one repository and fed to a pass over another has to be refused
        # rather than matched by name. Snapshot names collide across
        # repositories routinely, uuids do not.
        print(f"# repository: {args.repo}"
              + (f" {repo_uuid}" if repo_uuid else ""), file=sink)
        for name in sorted(mounted):
            e = mounted[name]
            print(f"{name}\t{e.get('uuid') or '-'}\t"
                  f"{'partial' if e['partial'] else 'full'}\t"
                  f"{','.join(sorted(e['indices']))}", file=sink)
    finally:
        if close_it:
            sink.close()
    print(f"# {len(mounted)} snapshot(s) in {args.repo} pinned by mounted "
          f"searchable-snapshot indices", file=sys.stderr)
    return 0


def mounted_not_in_listing(mounted: dict, names: list[str]) -> list[str]:
    """Pinned snapshots that the repository listing does not contain.

    Non-empty means a searchable-snapshot index is mounted against a snapshot
    that has been deleted from the repository. See print_mounted_danger.
    """
    return sorted(set(mounted) - set(names))


def print_mounted_danger(missing: list[str], mounted: dict, repo: str,
                         file=None) -> None:
    """Announce the deleted-while-mounted state as loudly as it deserves.

    `file` resolves at call time, never as a default argument: binding
    sys.stderr at import would ignore any later redirection and send the
    loudest message this tool emits to the wrong place.
    """
    file = file or sys.stderr
    print("", file=file)
    print("!!! DANGER: mounted snapshot(s) MISSING from the repository !!!",
          file=file)
    for name in missing:
        idx = ", ".join(sorted((mounted.get(name) or {}).get("indices") or []))
        print(f"    {name}  (mounted by: {idx or 'unknown'})", file=file)
    print(f"  These snapshots are gone from {repo}'s listing, yet indices are",
          file=file)
    print("  still mounted against them. Elasticsearch does not block deleting",
          file=file)
    print("  a snapshot that backs a mounted searchable-snapshot index (only",
          file=file)
    print("  repository UNREGISTRATION checks mounts), so the delete went",
          file=file)
    print("  through and the index is now reading blobs that no live snapshot",
          file=file)
    print("  references. Any reachability sweep will classify those blobs",
          file=file)
    print("  ORPHAN and deleting them DESTROYS the index.",
          file=file)
    print("  Do not sweep this repository until you remount/restore the index",
          file=file)
    print("  from a snapshot that still exists, or unmount it.", file=file)
    print("", file=file)


def fetch_slm_policies(args: argparse.Namespace) -> dict[str, str]:
    """Return {snapshot_name: slm_policy} for SLM-created snapshots.

    SLM stamps the policy id into snapshot metadata; manual and ILM-mount
    snapshots have no metadata.policy.
    """
    data = http_get(
        f"/_snapshot/{args.repo}/*"
        f"?filter_path=snapshots.snapshot,snapshots.metadata.policy", args)
    out: dict[str, str] = {}
    for s in (data or {}).get("snapshots") or []:
        name = s.get("snapshot")
        pol = (s.get("metadata") or {}).get("policy")
        if name and pol:
            out[name] = pol
    return out


def classify_snapshot(name: str, policies: dict, mounted: dict) -> str:
    """Label one snapshot: slm / frozen-pinned / slm+mounted / other."""
    is_slm = name in policies
    is_mounted = name in mounted
    if is_slm and is_mounted:
        return CLASS_BOTH
    if is_mounted:
        return CLASS_FROZEN
    if is_slm:
        return CLASS_SLM
    return CLASS_OTHER


def class_bucket(label: str) -> str:
    """Collapse a label to an accounting bucket.

    frozen-pinned wins over slm: a mounted snapshot is a footprint floor
    whose total must not be mistaken for backup growth, even if SLM created
    it.
    """
    return CLASS_FROZEN if label == CLASS_BOTH else label


def build_split(args: argparse.Namespace,
                names: list[str]) -> tuple[dict | None, str | None]:
    """Build the class map, or (None, reason) if discovery is unavailable."""
    try:
        mounted = fetch_mounted_set(args)
    except FETCH_ERRORS as e:
        return None, f"mounted-index discovery (_settings) failed: {e}"
    try:
        policies = fetch_slm_policies(args)
    except FETCH_ERRORS as e:
        return None, f"SLM policy metadata fetch failed: {e}"
    return {
        "labels": {n: classify_snapshot(n, policies, mounted) for n in names},
        "mounted": mounted,
        "policies": policies,
    }, None


def split_totals(rows: list[tuple], split: dict) -> tuple[dict, dict]:
    """Aggregate rows per class bucket, plus the partial/full mount breakdown.

    Returns (agg, frozen) where agg[bucket] = {"n","inc","tot"} and frozen
    carries the partial-vs-full split of the frozen-pinned totals. A mount
    that is partial anywhere counts as partial (frozen tier).
    """
    labels = split["labels"]
    mounted = split.get("mounted", {})
    agg = {c: {"n": 0, "inc": 0, "tot": 0} for c in CLASS_ORDER}
    frozen = {"total": 0, "partial_n": 0, "partial_tot": 0,
              "full_n": 0, "full_tot": 0, "both_n": 0}
    for _ms, name, inc, tot, _state in rows:
        label = labels.get(name, CLASS_OTHER)
        bucket = class_bucket(label)
        a = agg[bucket]
        a["n"] += 1
        a["inc"] += inc
        a["tot"] += tot
        if bucket != CLASS_FROZEN:
            continue
        frozen["total"] += tot
        if label == CLASS_BOTH:
            frozen["both_n"] += 1
        if (mounted.get(name) or {}).get("partial"):
            frozen["partial_n"] += 1
            frozen["partial_tot"] += tot
        else:
            frozen["full_n"] += 1
            frozen["full_tot"] += tot
    return agg, frozen


def print_class_summary(rows: list[tuple], split: dict) -> None:
    """Per-class counts, incrementals, and the frozen footprint."""
    agg, frozen = split_totals(rows, split)
    print("\n=== Snapshot classes (--split-frozen) ===")
    slm, oth = agg[CLASS_SLM], agg[CLASS_OTHER]
    print(f"  {CLASS_SLM:<14} {slm['n']:>4} snapshot(s), "
          f"incrementals (real repo growth): {fmt(slm['inc'])}")
    print(f"  {CLASS_FROZEN:<14} {agg[CLASS_FROZEN]['n']:>4} snapshot(s), "
          f"{FROZEN_FOOTPRINT_LABEL}: {fmt(frozen['total'])}")
    print(f"  {'':<14} {'':>4}   partial mounts (frozen tier, shared_cache): "
          f"{frozen['partial_n']} snapshot(s), {fmt(frozen['partial_tot'])}")
    print(f"  {'':<14} {'':>4}   full mounts (cold tier, full copy)       : "
          f"{frozen['full_n']} snapshot(s), {fmt(frozen['full_tot'])}")
    if frozen["both_n"]:
        print(f"  {'':<14} {'':>4}   {frozen['both_n']} snapshot(s) here are "
              f"also SLM-created ({CLASS_BOTH}); classed frozen-pinned.")
    print(f"  {CLASS_OTHER:<14} {oth['n']:>4} snapshot(s), "
          f"incrementals: {fmt(oth['inc'])} (manual / ILM-orphaned)")
    print("  note: summed incrementals are only meaningful for the slm class.")
    print("  A regular backup snapshot uploads ZERO bytes for shards already")
    print("  mounted as searchable snapshots, so the frozen tier shows up only")
    print("  as the pinned mount snapshots' totals above (a floor, not growth).")


def print_period_table(rows: list[tuple], group: str) -> None:
    """The original, class-blind per-period table."""
    agg: dict[str, dict] = {}
    for ms, _name, inc, tot, _state in rows:
        a = agg.setdefault(period_key(ms, group),
                           {"n": 0, "inc": 0, "max_tot": 0})
        a["n"] += 1
        a["inc"] += inc
        a["max_tot"] = max(a["max_tot"], tot)

    print(f"\n{'period':<12} {'snaps':>5} {'added (incremental)':>20} "
          f"{'largest snapshot (total)':>26}")
    grand = 0
    for k in sorted(agg):
        a = agg[k]
        grand += a["inc"]
        print(f"{k:<12} {a['n']:>5} {fmt(a['inc']):>20} {fmt(a['max_tot']):>26}")
    print(f"{'SUM':<12} {len(rows):>5} {fmt(grand):>20}")


def print_period_table_split(rows: list[tuple], group: str,
                             split: dict) -> None:
    """Per-period table with a class dimension, plus per-class SUM rows."""
    labels = split["labels"]
    agg: dict[tuple[str, str], dict] = {}
    for ms, name, inc, tot, _state in rows:
        key = (period_key(ms, group),
               class_bucket(labels.get(name, CLASS_OTHER)))
        a = agg.setdefault(key, {"n": 0, "inc": 0, "max_tot": 0})
        a["n"] += 1
        a["inc"] += inc
        a["max_tot"] = max(a["max_tot"], tot)

    print(f"\n{'period':<12} {'class':<14} {'snaps':>5} "
          f"{'added (incremental)':>20} {'largest snapshot (total)':>26}")
    for period in sorted({k[0] for k in agg}):
        for cls in CLASS_ORDER:
            a = agg.get((period, cls))
            if not a:
                continue
            print(f"{period:<12} {cls:<14} {a['n']:>5} "
                  f"{fmt(a['inc']):>20} {fmt(a['max_tot']):>26}")
    for cls in CLASS_ORDER:
        parts = [a for k, a in agg.items() if k[1] == cls]
        n = sum(a["n"] for a in parts)
        inc = sum(a["inc"] for a in parts)
        print(f"{'SUM':<12} {cls:<14} {n:>5} {fmt(inc):>20}")
    print(f"{'SUM':<12} {'(all)':<14} {len(rows):>5} "
          f"{fmt(sum(r[2] for r in rows)):>20}")


# --- per-snapshot classified export (--emit-classified) ---------------------

CLASSIFIED_HEADER = ("snapshot", "class", "policy", "tier", "mounted_by",
                     "state", "start_time_utc", "incremental_bytes",
                     "total_bytes")

MISSING_STATE = "MISSING-FROM-CATALOG"


def parse_class_filter(value: str | None) -> list[str] | None:
    """Validate a --class list. None (the default) means every class.

    Only the accounting buckets are selectable: slm+mounted is a LABEL that
    buckets to frozen-pinned, so selecting it separately would imply a split
    the rest of the tool does not make. Raises ValueError naming the valid
    set, for the caller to hand to parser.error().
    """
    if value is None:
        return None
    wanted, seen = [], set()
    for part in value.split(","):
        name = part.strip()
        if name and name not in seen:
            seen.add(name)
            wanted.append(name)
    bad = [c for c in wanted if c not in CLASS_ORDER]
    if bad or not wanted:
        raise ValueError(
            f"unknown --class value(s): {', '.join(bad) or '(empty)'}; "
            f"valid classes are {', '.join(CLASS_ORDER)}")
    return wanted


def iso_utc(ms: int) -> str:
    """ISO-8601 UTC for epoch milliseconds; '-' when there is no stamp."""
    if not ms:
        return "-"
    return dt.datetime.fromtimestamp(
        ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mount_tier(entry: dict | None) -> str:
    """'partial' | 'full' | '-' for a mounted-set entry.

    Partial wins over full for a snapshot backing both, matching emit_mounted()
    and split_totals(): the frozen-tier (shared_cache) reading is conservative.
    """
    if not entry:
        return "-"
    return "partial" if entry.get("partial") else "full"


def mount_indices(entry: dict | None) -> str:
    return ",".join(sorted((entry or {}).get("indices") or [])) or "-"


def classified_rows(rows: list[tuple], split: dict,
                    missing: list[str]) -> list[tuple]:
    """One export tuple per snapshot: catalog rows plus the danger rows.

    `missing` is mounted_not_in_listing()'s output: snapshots a mounted index
    still pins that the repository no longer lists. They have no _status entry
    (nothing left to ask about), so state is MISSING-FROM-CATALOG and every
    measured field is '-'. Leaving them out would make the export claim a
    repository is clean when its riskiest state is exactly what is absent.

    Sorted by start time then name; the danger rows sort first (no stamp).
    """
    labels = split["labels"]
    mounted = split.get("mounted", {})
    policies = split.get("policies", {})
    out = []
    for start_ms, name, inc, tot, state in rows:
        entry = mounted.get(name)
        out.append((start_ms, name, labels.get(name, CLASS_OTHER),
                    policies.get(name, "-"), mount_tier(entry),
                    mount_indices(entry), state, iso_utc(start_ms),
                    str(inc), str(tot)))
    for name in missing:
        entry = mounted.get(name)
        out.append((0, name, CLASS_FROZEN, "-", mount_tier(entry),
                    mount_indices(entry), MISSING_STATE, "-", "-", "-"))
    out.sort(key=lambda r: (r[0], r[1]))
    return [r[1:] for r in out]


def filter_classified(rows: list[tuple],
                      classes: list[str] | None) -> list[tuple]:
    """Keep rows whose class BUCKET is selected (None keeps everything).

    Bucket, not label: --class frozen-pinned must also catch slm+mounted, and
    --class slm must not.
    """
    if classes is None:
        return list(rows)
    wanted = set(classes)
    return [r for r in rows if class_bucket(r[1]) in wanted]


def print_classified_summary(rows: list[tuple], missing: list[str],
                             classes: list[str] | None, written: int,
                             file=None) -> None:
    """Class counts for the WHOLE repository, then the filter, on stderr.

    The counts are deliberately unfiltered: a reader handed a --class subset
    needs to know what the file leaves out, not just what it contains.
    """
    file = file or sys.stderr
    counts = {c: 0 for c in CLASS_ORDER}
    for r in rows:
        counts[class_bucket(r[1])] += 1
    parts = ", ".join(f"{c}={counts[c]}" for c in CLASS_ORDER)
    print(f"# classified: {len(rows)} snapshot(s) total ({parts})", file=file)
    print(f"# {len(missing)} mounted snapshot(s) {MISSING_STATE}", file=file)
    if classes is not None:
        print(f"# filtered to: {','.join(classes)} "
              f"({written} row(s) written)", file=file)


def fetch_snapshot_listing(args: argparse.Namespace) -> list[str] | None:
    """Snapshot names from the repository listing, or None after reporting.

    HTTPError is caught first on purpose: it subclasses URLError and OSError,
    and its status code is the actionable half of the message.
    """
    try:
        listing = http_get(f"/_snapshot/{args.repo}/*?verbose=false", args)
    except urllib.error.HTTPError as e:
        print(f"ES returned HTTP {e.code} for {args.es}: {e.reason} "
              f"(check --user/--api-key and the repo name)", file=sys.stderr)
        return None
    except (urllib.error.URLError, OSError, ssl.SSLError) as e:
        print(f"cannot reach {args.es}: {e} "
              f"(check the URL, port-forward, and --ca-cert/--insecure)",
              file=sys.stderr)
        return None
    return [s["snapshot"] for s in listing.get("snapshots", [])]


def fetch_status_rows(args: argparse.Namespace,
                      names: list[str]) -> list[tuple] | None:
    """(start_ms, name, incremental, total, state) per snapshot, or None.

    Partial results are discarded on any batch failure: a report or an export
    built from some of the snapshots is worse than an error.
    """
    rows = []
    for i in range(0, len(names), args.batch):
        chunk = names[i : i + args.batch]
        try:
            st = http_get(
                f"/_snapshot/{args.repo}/{','.join(chunk)}/_status", args)
        except (urllib.error.URLError, OSError, ssl.SSLError) as e:
            print(f"_status fetch failed for batch {i//args.batch + 1}: {e} "
                  f"(partial results discarded)", file=sys.stderr)
            return None
        for s in st.get("snapshots", []):
            stats = s.get("stats", {})
            rows.append((
                stats.get("start_time_in_millis", 0),
                s.get("snapshot", "?"),
                stats.get("incremental", {}).get("size_in_bytes", 0),
                stats.get("total", {}).get("size_in_bytes", 0),
                s.get("state", "?"),
            ))
        print(f"# fetched {min(i + args.batch, len(names))}/{len(names)}",
              file=sys.stderr)
    return rows


def emit_classified(args: argparse.Namespace,
                    classes: list[str] | None = None) -> int:
    """Export one classified TSV row per snapshot in the repository.

    Combines the two populations --split-frozen reports in aggregate into a
    per-snapshot table: the backup (slm) snapshots and the mount-pinned
    (frozen-pinned) ones, with the SLM policy, the mount tier, the mounting
    indices, the state, the start time and both byte counts on every row.

    Unlike --split-frozen this does NOT fall back when discovery fails: an
    export missing the mount linkage would silently misclassify every pinned
    snapshot as a plain backup, which is exactly the mistake the file exists
    to prevent. Discovery failure is exit 1 and no file is written.
    """
    names = fetch_snapshot_listing(args)
    if names is None:
        return 1
    if not names:
        print("no snapshots found", file=sys.stderr)
        return 1
    print(f"# {len(names)} snapshots in {args.repo}", file=sys.stderr)

    split, split_error = build_split(args, names)
    if split_error:
        print(f"--emit-classified aborted: {split_error} "
              f"(check the URL, --user/--api-key and --ca-cert/--insecure). "
              f"An incomplete classified export is worse than none, so "
              f"nothing was written.", file=sys.stderr)
        return 1
    print(f"# --emit-classified: {len(split['mounted'])} snapshot(s) pinned "
          f"by mounted indices, {len(split['policies'])} SLM-created",
          file=sys.stderr)

    rows = fetch_status_rows(args, names)
    if rows is None:
        return 1

    # The banner is unconditional: a --class filter changes what the FILE
    # holds, never whether the operator hears about a deleted-while-mounted
    # snapshot.
    missing = mounted_not_in_listing(split["mounted"], names)
    if missing:
        print_mounted_danger(missing, split["mounted"], args.repo)

    every = classified_rows(rows, split, missing)
    export = filter_classified(every, classes)
    try:
        sink, close_it = open_emit_sink(args)
    except OSError as e:
        print(f"cannot open --out file for writing: {e}", file=sys.stderr)
        return 1
    try:
        print("\t".join(CLASSIFIED_HEADER), file=sink)
        for r in export:
            print("\t".join(r), file=sink)
    finally:
        if close_it:
            sink.close()
    print_classified_summary(every, missing, classes, len(export))
    return 0


# Every number a sizing recommendation prints, computed once so the printing
# below reads what it reports rather than recomputing it.
Sizing = collections.namedtuple(
    "Sizing",
    "baseline_row baseline days samples median mean p95 growth growth_p95 "
    "headroom frozen frozen_total total total_margin total_p95 "
    "total_p95_margin skipped partial excluded first_snapshot_day")

OPERATIONAL_MARGIN = 1.2

RECOMMEND_HEADING = "\n=== Repository sizing recommendation ==="


def slm_pool(rows: list, split: dict | None) -> list:
    """The snapshots whose sizes may feed a recommendation.

    IN_PROGRESS snapshots report partial totals and would pollute both the
    baseline and the growth samples. Under --split-frozen this narrows again
    to the slm class: a pinned mount snapshot is a footprint floor, not growth.
    """
    usable = [r for r in rows if r[4] in ("SUCCESS", "PARTIAL")]
    if not split:
        return usable
    labels = split["labels"]
    return [r for r in usable
            if class_bucket(labels.get(r[1], CLASS_OTHER)) == CLASS_SLM]


def measure_sizing(rows: list, retention_days: int,
                   split: dict | None) -> Sizing | None:
    """Work out the recommendation's arithmetic, or None with nothing to size.

    Growth is aggregated per calendar day (UTC). Several snapshots on one day,
    SLM dailies plus ILM mounts, would otherwise shrink the window.
    """
    usable = [r for r in rows if r[4] in ("SUCCESS", "PARTIAL")]
    pool = slm_pool(rows, split)
    if not pool:
        return None

    daily: dict[str, int] = {}
    for ms, _name, inc, _tot, _state in pool:
        day = period_key(ms, "day")
        daily[day] = daily.get(day, 0) + inc
    days = sorted(daily)[-retention_days:]
    samples = [daily[d] for d in days]

    # Baseline is the LARGEST snapshot total, not the newest row: whatever
    # finished last may be a per-index mount snapshot. The repository floor is
    # the union of every retained snapshot's referenced bytes, and the largest
    # single total is a lower bound on that union.
    baseline_row = max(pool, key=lambda r: r[3])
    baseline = baseline_row[3]
    median = statistics.median(samples)
    growth = retention_days * median
    growth_p95 = retention_days * p95(samples)
    frozen = split_totals(usable, split)[1] if split else None
    frozen_total = frozen["total"] if frozen else 0
    # An upgrade day rewrites segments, so the next snapshot re-uploads far
    # more than a normal day. One full baseline is the heuristic for that.
    total = baseline + growth + baseline + frozen_total
    total_p95 = baseline + growth_p95 + baseline + frozen_total
    return Sizing(
        baseline_row=baseline_row, baseline=baseline, days=days,
        samples=samples, median=median, mean=statistics.fmean(samples),
        p95=p95(samples), growth=growth, growth_p95=growth_p95,
        headroom=baseline, frozen=frozen, frozen_total=frozen_total,
        total=total, total_margin=total * OPERATIONAL_MARGIN,
        total_p95=total_p95,
        total_p95_margin=total_p95 * OPERATIONAL_MARGIN,
        skipped=len(rows) - len(usable),
        partial=sum(1 for r in pool if r[4] == "PARTIAL"),
        excluded=len(usable) - len(pool),
        first_snapshot_day=period_key(min(pool)[0], "day"))


def print_frozen_caveat(split: dict | None) -> None:
    """Say what the frozen tier does to these numbers, measured or not."""
    if split:
        print("\nNOTE: --split-frozen is active. Baseline and growth below")
        print("come from the slm (regular backup) class ONLY, and the measured")
        print("frozen footprint is added as its own term instead of being an")
        print("unquantified undercount. A byte count over the blobs the")
        print("repository's own metadata still reaches remains the ground")
        print("truth for total repository capacity: the repo floor is the UNION of")
        print("all retained snapshots, which these per-snapshot totals can")
        print("only bound from below.")
        return
    print("\nWARNING (precondition): if this repository backs searchable")
    print("snapshots (frozen tier), regular snapshots upload ZERO files for")
    print("already-mounted indices, so the baseline below UNDERCOUNTS by the")
    print("entire frozen footprint (it lives in separate pinned per-index")
    print("mount snapshots). For such repositories a byte count over the")
    print("reachable blobs is the sizing source of truth, not this")
    print("recommendation.")


def print_measured_inputs(sizing: Sizing, split: dict | None) -> None:
    """The baseline, the frozen footprint, and what was left out of both."""
    frozen = sizing.frozen
    print("\nMeasured inputs (from _snapshot/<repo>/_status):")
    if split:
        print(f"  largest SLM snapshot total ({sizing.baseline_row[1]}) : "
              f"{fmt(sizing.baseline)}")
        print(f"  {FROZEN_FOOTPRINT_LABEL}: {fmt(sizing.frozen_total)}")
        print(f"    partial mounts (frozen tier) : {frozen['partial_n']} "
              f"snapshot(s), {fmt(frozen['partial_tot'])}")
        print(f"    full mounts (cold tier)      : {frozen['full_n']} "
              f"snapshot(s), {fmt(frozen['full_tot'])}")
        if sizing.excluded:
            print(f"  note: {sizing.excluded} non-slm snapshot(s) excluded "
                  f"from baseline/growth")
            print("  (frozen-pinned mounts are a footprint floor, not growth;")
            print("  'other' snapshots have no policy and no mount pinning "
                  "them).")
    else:
        print(f"  largest snapshot total ({sizing.baseline_row[1]}) : "
              f"{fmt(sizing.baseline)}")
    if sizing.skipped:
        print(f"  note: {sizing.skipped} snapshot(s) excluded (not "
              f"SUCCESS/PARTIAL,")
        print("  e.g. IN_PROGRESS, whose partial totals would pollute them).")
    if sizing.partial:
        print(f"  warning: {sizing.partial} PARTIAL snapshot(s) included; "
              f"some shards")
        print("  failed, so their incrementals may understate real growth.")


def print_growth_window(sizing: Sizing, split: dict | None) -> None:
    """The daily growth samples, and what would make them misleading."""
    window = (f"{len(sizing.days)} day(s) with data "
              f"({sizing.days[0]} .. {sizing.days[-1]}):")
    if split:
        print("  growth samples (slm class ONLY): per-calendar-day incremental")
        print(f"  sums over the last {window}")
    else:
        print("  growth samples: per-calendar-day incremental sums over the last")
        print(f"  {window}")
    print(f"    median daily growth : {fmt(sizing.median)}")
    print(f"    mean daily growth   : {fmt(sizing.mean)}")
    print(f"    p95 daily growth    : {fmt(sizing.p95)}")
    # Earliest by timestamp, not input order. This must not depend on callers
    # pre-sorting rows: a reversed list once produced a FALSE "growth is
    # overstated" caveat.
    if sizing.first_snapshot_day in sizing.days:
        print("    note: window includes the repository's FIRST snapshot day,")
        print("    whose incremental == a full upload; growth is overstated.")
    if sizing.samples and sizing.median > 0 and \
            max(sizing.samples) > 3 * sizing.median:
        print(f"    note: outlier day present (max {fmt(max(sizing.samples))} "
              f"> 3x median). Reindex/merge/upgrade days upload far more.")


def print_formula(sizing: Sizing, retention_days: int,
                  split: dict | None) -> None:
    """The addition itself, term by term, in both variants."""
    print(f"\nFormula (retention_days = {retention_days}):")
    if split:
        print(f"  baseline (largest slm snapshot total)     : "
              f"{fmt(sizing.baseline)}")
    else:
        print(f"  baseline (largest snapshot total)         : "
              f"{fmt(sizing.baseline)}")
    print(f"  + retention growth ({retention_days} x median daily)     : "
          f"{fmt(sizing.growth)}")
    print(f"  + upgrade-day headroom (1 x baseline)     : "
          f"{fmt(sizing.headroom)}")
    if split:
        print(f"  + frozen footprint (pinned mounts)        : "
              f"{fmt(sizing.frozen_total)}")
    print(f"  = recommended repository capacity         : {fmt(sizing.total)}")
    print(f"  = with +20% operational margin            : "
          f"{fmt(sizing.total_margin)}")
    print(f"  conservative variant ({retention_days} x p95 daily):")
    print(f"  = recommended repository capacity (p95)   : "
          f"{fmt(sizing.total_p95)}")
    print(f"  = with +20% operational margin (p95)      : "
          f"{fmt(sizing.total_p95_margin)}")


def print_assumptions(split: dict | None) -> None:
    """What the arithmetic above takes on trust, and where it came from."""
    print("\nAssumptions:")
    print("  * Snapshots are incremental: each copies only new segments since")
    print("    the previous snapshot; the first is ~full. [Elastic docs]")
    print("  * The true repo floor is the UNION of all retained snapshots'")
    print("    referenced bytes; the largest single snapshot total is a lower")
    print("    bound on that union, used here as the baseline.")
    print("  * Elastic recommends a fresh snapshot before upgrading, and large")
    print("    segment rewrites (e.g. a version upgrade merging/rewriting")
    print("    segments) make the next snapshot re-upload far more than a")
    print("    normal day. Modeling that as 1x baseline full is a heuristic,")
    print("    not an official Elastic figure.")
    if split:
        print("  * The frozen footprint is MEASURED (sum of pinned mount")
        print("    snapshot totals), not estimated. It is a floor: mounts that")
        print("    share segment lineage double-count, and it excludes any")
        print("    blob the repository retains that no snapshot references.")
        print("  * The +20% margin is applied to the whole figure, frozen term")
        print("    included, so it stays conservative.")
    print("  * The +20% margin is a heuristic, not an official Elastic figure.")
    print("  * Elastic publishes no official repo-capacity formula; sizing here")
    print("    is derived from documented incremental behavior only.")


def print_retention_hint(retention_days: int) -> None:
    """The SLM retention block that matches the window just sized."""
    print(f"\nMatching SLM retention for a {retention_days}-day window, e.g.:")
    print(f'  "retention": {{ "expire_after": "{retention_days}d", '
          f'"min_count": 5 }}')
    print("  (avoid max_count here: with multiple snapshots per day, from SLM")
    print("  dailies plus ILM mount snapshots, a count bound can delete")
    print("  snapshots that are still inside the time window.)")
    print("\nSources (fetched 2026-08-24):")
    print("  https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore")
    print("  https://www.elastic.co/docs/deploy-manage/upgrade/prepare-to-upgrade")
    print("  https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/create-snapshots")


def recommend(rows: list[tuple[int, str, int, int, str]],
              retention_days: int,
              split: dict | None = None) -> None:
    """Print a repository sizing recommendation from measured snapshot stats.

    Grounded in Elastic docs (fetched 2026-08-24):
    * Incremental behavior: "the snapshot only needs to copy any new segments
      created since the repository's last snapshot" (first snapshot ~full,
      later ones incremental):
      https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore
    * Pre-upgrade snapshot: "Take a snapshot of your cluster before starting
      the upgrade":
      https://www.elastic.co/docs/deploy-manage/upgrade/prepare-to-upgrade
    * SLM retention (expire_after / min_count / max_count):
      https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/create-snapshots

    Elastic publishes NO official repository-capacity sizing formula, so the
    combination below (baseline + retention x daily growth + 1x-full upgrade
    headroom, +20% margin) is a heuristic built on the documented behaviors.

    Caveats handled here:
    * Only SUCCESS/PARTIAL snapshots feed the numbers. IN_PROGRESS snapshots
      report partial totals and would pollute both baseline and growth.
    * Baseline = LARGEST snapshot total across rows, not the latest row:
      whatever finished last may be a per-index mount snapshot. Note the repo
      floor is the UNION of all retained snapshots' referenced bytes, which is
      >= the largest single snapshot's total.
    * Frozen tier: snapshots of already-mounted searchable-snapshot indices
      upload zero files for those shards, so the frozen footprint never shows
      up in any regular snapshot's total; the printed warning covers this.
    * Growth is aggregated per calendar day. Multiple snapshots on one day
      (SLM dailies plus ILM mounts) would otherwise shrink the window.

    With `split` (from --split-frozen) the frozen caveat stops being a blanket
    warning and becomes arithmetic: baseline and growth come from the `slm`
    class only, and the measured frozen footprint is added as its own term.
    """
    sizing = measure_sizing(rows, retention_days, split)
    if sizing is None:
        print(RECOMMEND_HEADING)
        which = "slm " if split else ""
        print(f"no SUCCESS/PARTIAL {which}snapshots - cannot recommend a size.")
        return
    print(RECOMMEND_HEADING)
    print_frozen_caveat(split)
    print_measured_inputs(sizing, split)
    print_growth_window(sizing, split)
    print_formula(sizing, retention_days, split)
    print_assumptions(split)
    print_retention_hint(retention_days)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1],
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--es", required=True, help="http(s)://host:9200")
    p.add_argument("--repo", required=True)
    p.add_argument("--group", choices=["day", "week", "month"], default="day")
    p.add_argument("--user", help="basic auth user:password")
    p.add_argument("--api-key", help="ApiKey header value")
    p.add_argument("--ca-cert", help="CA bundle for https")
    p.add_argument("--insecure", action="store_true",
                   help="skip TLS verification of --es, for a lab cluster "
                        "with a self-signed certificate. Accepted only for a "
                        "loopback, private or in-cluster address; anything "
                        "else needs --ca-cert")
    p.add_argument("--batch", type=int, default=20,
                   help="snapshots per _status request")
    p.add_argument("--recommend", action="store_true",
                   help="print a repository sizing recommendation")
    p.add_argument("--retention-days", type=int, default=7,
                   help="retention window in days for --recommend (5-10)")
    p.add_argument("--split-frozen", action="store_true",
                   help="separate SLM backup snapshots from pinned "
                        "searchable-snapshot mount snapshots (frozen/cold "
                        "tier) in the report and in --recommend")
    p.add_argument("--emit-mounted", action="store_true",
                   help="print the snapshots pinned by mounted "
                        "searchable-snapshot indices as TSV (name, uuid, "
                        "partial|full, indices) and exit; this is the set no "
                        "delete may touch while those indices are mounted")
    p.add_argument("--emit-classified", action="store_true",
                   help="write one classified TSV row per snapshot in the "
                        "repository (snapshot, class, policy, tier, "
                        "mounted_by, state, start_time_utc, "
                        "incremental_bytes, total_bytes) and exit")
    p.add_argument("--class", dest="classes", metavar="NAMES",
                   help="restrict --emit-classified rows to a comma-separated "
                        "subset of the classes (slm, frozen-pinned, other); "
                        "default is every class")
    p.add_argument("--out", metavar="FILE",
                   help="write the emit mode's machine-readable output to "
                        "FILE instead of stdout (requires --emit-mounted or "
                        "--emit-classified)")
    return p


def check_arguments(parser: argparse.ArgumentParser,
                    args: argparse.Namespace):
    """Refuse argument combinations that cannot mean anything.

    Returns the set of accounting classes --emit-classified may export.
    """
    if not 5 <= args.retention_days <= 10:
        parser.error(f"--retention-days must be between 5 and 10 "
                     f"(got {args.retention_days}); site snapshot policy is "
                     f"5-10 days max")

    # --es comes from configuration, and configuration is not the same as
    # trusted: urlopen will happily open file:// or ftp://. This tool only
    # ever reads over http or https.
    split = urllib.parse.urlsplit(args.es)
    if split.scheme not in ("http", "https"):
        parser.error(f"--es is {args.es!r}; only http and https are accepted, "
                     f"so a {split.scheme or '(no scheme)'!r} value cannot "
                     f"be opened")
    # --insecure is for an ECK or lab cluster serving a certificate it signed
    # itself. Against a cluster anything can route to, an unverified
    # connection means the numbers in this report describe whichever host
    # answered, and the basic-auth header has already been sent to it.
    if args.insecure and not is_lab_host(split.hostname):
        parser.error(f"--insecure was passed for {split.hostname!r}, which is "
                     f"not a loopback, private or in-cluster address. It "
                     f"exists for a lab cluster serving its own certificate, "
                     f"not for a cluster anything can route to. Pass "
                     f"--ca-cert with the CA that certificate chains to "
                     f"instead.")

    if args.emit_mounted and args.emit_classified:
        parser.error("--emit-mounted and --emit-classified are mutually "
                     "exclusive; pick one export mode")
    if args.out and not (args.emit_mounted or args.emit_classified):
        parser.error("--out requires an emit mode (--emit-mounted or "
                     "--emit-classified); the report tables are written for "
                     "humans and are not redirected into a file")
    if args.classes is not None and not args.emit_classified:
        parser.error("--class only applies to --emit-classified")
    try:
        return parse_class_filter(args.classes)
    except ValueError as e:
        parser.error(str(e))


def print_split_header(args: argparse.Namespace, names: list, split: dict):
    """What --split-frozen found, and a banner if a mount is unbacked."""
    print(f"# --split-frozen: {len(split['mounted'])} snapshot(s) pinned by "
          f"mounted indices, {len(split['policies'])} SLM-created",
          file=sys.stderr)
    # A mount pinning a snapshot the repository no longer lists is the
    # deleted-while-mounted state: the index runs on leaked blobs that a
    # reachability sweep would classify ORPHAN.
    gone = mounted_not_in_listing(split["mounted"], names)
    if gone:
        print_mounted_danger(gone, split["mounted"], args.repo)


def period_report(args: argparse.Namespace) -> int:
    """The human-readable per-period table, and the sizing section under
    --recommend."""
    names = fetch_snapshot_listing(args)
    if names is None:
        return 1
    if not names:
        print("no snapshots found", file=sys.stderr)
        return 1
    print(f"# {len(names)} snapshots in {args.repo}", file=sys.stderr)

    split = None
    split_error = None
    if args.split_frozen:
        split, split_error = build_split(args, names)
        if split_error:
            print(f"# --split-frozen skipped: {split_error}", file=sys.stderr)
        else:
            print_split_header(args, names, split)

    rows = fetch_status_rows(args, names)  # (start_ms, name, inc, total, state)
    if rows is None:
        return 1

    rows.sort()
    if split:
        print_period_table_split(rows, args.group, split)
    else:
        print_period_table(rows, args.group)
    print("\n'added' sums incremental bytes = real repo growth per period."
          "\n'total' is what one snapshot references; totals across snapshots"
          " overlap, so never sum them.")
    if split:
        print_class_summary(rows, split)
    elif args.split_frozen and split_error:
        print(f"\nNOTE: --split-frozen was requested but skipped "
              f"({split_error}); the unsplit report above and the "
              f"frozen-tier warning below still apply.")
    if args.recommend:
        recommend(rows, args.retention_days, split)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.es = args.es.rstrip("/")
    class_filter = check_arguments(parser, args)
    args.tls = tls_context(args)

    # Needs only --es/--repo/auth: no snapshot listing, no _status pass.
    if args.emit_mounted:
        return emit_mounted(args)
    # Needs the same discovery fetches as --split-frozen, plus _status, but
    # short-circuits the human report the same way --emit-mounted does.
    if args.emit_classified:
        return emit_classified(args, class_filter)
    return period_report(args)


if __name__ == "__main__":
    sys.exit(main())
