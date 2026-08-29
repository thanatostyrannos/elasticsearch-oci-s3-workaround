#!/usr/bin/env python3
"""Exercise both halves of the audit against a live repository, repeatedly.

WHY THIS EXISTS

The audit has two paths and they fail differently.

The METADATA path condemns snapshot documents, index metadata and global
metadata left behind when a snapshot is deleted. It needs only the generation
chain, so it works on a repository being written to. Every campaign so far has
exercised it, and it has never once condemned a live object.

The SEGMENT path condemns data blobs, and it is the one with real blast radius:
a wrongly named segment is unrecoverable data loss. It needs a COMPLETE view of
a shard directory, and across every campaign run to date it has condemned
exactly zero objects. Not because it is broken. Because something always
stopped it, and until this harness existed nobody had made it run on purpose.

WHAT STOPS THE SEGMENT PATH, MEASURED

Three causes, in the order they were found:

1. A snapshot in flight. It declares more shards than the run has read, so the
   completeness check refuses. Waiting for no IN_PROGRESS snapshot removes this
   one, and on its own it changed nothing, which is how the next cause surfaced.

2. A shard document that lists no files. Elasticsearch writes one whenever it
   snapshots an EMPTY shard, which happens in the window just after a rollover
   creates a fresh backing index. The parser refuses such a document, because a
   document naming nothing satisfies the subset test against every directory
   and that was a real counterexample. The refusal is correct. The consequence
   is that one badly timed snapshot poisons a shard directory until a later
   snapshot supersedes it.

3. The cascade. A dropped shard directory makes its snapshot's declared extent
   come up short, which drops that snapshot's OTHER directories too. Two
   poisoned directories took all eight in the run that found this.

So the segment path is reachable, and reaching it is a scheduling problem in
the RIG: never snapshot a shard that has no documents in it yet.

WHAT THIS HARNESS DOES NOT DO, AND MUST NOT

It does not relax the refusal. A file-less shard document stays unparseable and
its directory stays dropped, because an empty shard may belong to an index
Elasticsearch is about to use and the tool cannot tell the difference from the
bucket alone.

It is tempting to assume the Elasticsearch veto covers that case. It does not,
reliably. The veto protects by snapshot uuid, and by `indices/<index_uuid>/`
for indices that back a MOUNTED searchable snapshot. An ordinary live index
that nothing has mounted is outside both. The thing actually protecting an
empty shard directory is the parser refusing the document, and that refusal is
load bearing rather than incidental.

So this harness changes WHEN the rig takes a snapshot. It never changes what
the audit will condemn.

MEASURED: WAITING IS NOT ENOUGH, AND WHY

Two attempts at reaching the segment path by waiting both failed, and the
second failure is the useful one.

Attempt one waited for no snapshot in flight. No change: 0 of 8 shard
directories read.

Attempt two also waited for every primary shard to hold documents, and for two
further snapshots to supersede any file-less document. Also no change, and the
directories it dropped were named by DIFFERENT documents than the first run.

That is the finding. The poison is not a one-off to be waited out, it is
continuously produced.

The reading offered at the time was that ILM rolls over every ten minutes, that
each rollover leaves a backing index whose shards are briefly empty, and that a
snapshot on a sixty second cycle lands inside that window. That reading is
dead. At the rate the rig ingests, a shard fills in seconds, so a snapshot
should almost never catch an empty one.

DIRECTORY COUNT IS A PROXY, NOT THE CAUSE. It correlated well enough to look
causal: across 82 runs against the real Oracle bucket, 4 directories read 1 of
1, 6 read 4 of 42, and 8 read 1 of 40. A control on MinIO then held 22
directories and read 10 of them, condemning 816 segment blobs, while an earlier
run on that same repository with 16 directories read none. A number that moves
in both directions is not a cause.

THE CAUSE IS A FILE-LESS SHARD DOCUMENT INSIDE A SNAPSHOT'S DECLARED EXTENT.
Two channels do the damage and both are arithmetic over repository format data,
so no object store is involved:

    indices/<index-uuid>/0 was dropped whole: the current document index-<gen>
    could not be read

    indices/<index-uuid>/1 was dropped whole: snapshot '<name>' declares 12
    shard(s) in total and this run read 2

The first is the seed, a document Elasticsearch wrote while the shard was
empty. The second is the contagion: one refused directory shortens the
snapshot's declared extent, which drops every other directory that snapshot
named. That is why the failure is all or nothing per snapshot, and why more
directories per snapshot means worse odds without being the mechanism.

SO THE LEVER IS ROLLOVER, because rollover is what creates a shard with no
documents in it for a snapshot to catch:

    --rollover-max-age 24h --rollover-max-docs 100000000

That is a change to the RIG. The audit's refusal stays exactly as it is, and
must, because it is what protects an empty shard directory.

CONFIRMED. Held to one backing index, four consecutive audits against the live
Oracle bucket each read 2 of 2 and condemned 124, 252, 380 and 508 segment
blobs. On MinIO, rollover held back read 10 of 22 and condemned 816.

ONE ARM IS NOT YET ISOLATED. Six directories in the MinIO control never
recovered across five runs, and they belong to indices already converted to
frozen searchable snapshots. If frozen conversion leaves a document file-less
permanently rather than until a later snapshot supersedes it, that is a third
source, and the short delete phase rather than the rollover change may be what
made the Oracle runs read. Do not treat the rollover lever as the whole story.

HOW THIS HARNESS ADDRESSES IT

`--mode segment` waits for two conditions before each audit: no snapshot in
flight, and every primary shard of the data stream holding at least
`--min-docs-per-shard` documents. The second is the one that matters.

`--mode metadata` does the opposite and audits without waiting, which is the
condition the earlier campaigns ran under and the one that exercises the
guards' refusal behaviour.

`--mode mixed` alternates, because both are worth exercising and a run that
only does one leaves half the tool untested.

WHAT IS DELIBERATELY NOT AUTOMATED

Nothing here decides to delete. Every cycle runs the audit, then the dry run,
then execute against the digest that dry run printed. If the manifest changed
between them the approval no longer matches and the cycle fails, which is the
behaviour that makes an approved manifest mean something.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# The audit is invoked as `python3 -m generation_chain`, which resolves only
# from the directory holding that package. This harness is routinely launched
# by absolute path from a scratch directory, so the cwd cannot be inherited:
# every audit then fails with "No module named generation_chain".
ROOT = os.path.dirname(os.path.abspath(__file__))

# The path refusals the audit already applies to --manifest and
# --credentials, applied to this harness's own paths as well. Imported
# rather than spelled out again, on the sibling package this harness ships
# next to in the release archive and already cannot run without: every
# cycle shells out to `python3 -m generation_chain` in ROOT. Two copies of
# one refusal drift, and then one bad path gets two different answers.
sys.path.insert(0, ROOT)
from generation_chain.paths import PathRefused, checked_path  # noqa: E402

COLUMNS = ["cycle", "utc", "mode", "settle", "shards_read",
           "segments_condemned", "deleted", "failed", "unconfirmed",
           "reclaimable", "exit"]

DELETED = re.compile(r"^deleted:\s*(\d+)", re.M)
FAILED = re.compile(r"^failed:\s*(\d+)", re.M)
UNCONFIRMED = re.compile(r"^unconfirmed:\s*(\d+)", re.M)
DIGEST = re.compile(r"approve-digest ([0-9a-f]{64})")
ROWS = re.compile(r"approve-rows (\d+)")
# The capture starts at the first non-space on purpose. With `\s+(.+)`
# the two halves both match spaces, so there are as many ways to split a run
# of them as there are spaces, and the engine tries them all on a line that
# does not end the way it expects.
RECLAIMABLE = re.compile(r"^Reclaimable\n[ \t]+(\S.*)$", re.M)
SEGMENTS_READ = re.compile(r"shard directories read: (\d+) of (\d+)")

# Consecutive subprocess executions are spaced by this many seconds. The audit,
# the dry run and the execute all talk to the same endpoint, and running them
# back to back gives it no gap at all.
EXECUTION_GAP_SECONDS = 1


def artifact(outdir, name):
    """The path of one run artifact under --out, refusing to leave it.

    Every name comes from this file and carries a cycle number, so the check
    is an invariant rather than a guess about the caller. It holds --out
    itself to the directory the operator named, which is the part that comes
    from outside.
    """
    directory = os.path.realpath(outdir)
    path = os.path.realpath(os.path.join(directory, name))
    if os.path.dirname(path) != directory:
        raise ValueError(f"{name!r} would be written to {path!r}, which is "
                         f"outside --out {directory!r}")
    return path


def read_text(path):
    """A whole artifact file, closed before the caller reads a line of it."""
    with open(path) as handle:
        return handle.read()


def read_secret_file(path, what):
    """The one line in a secret file, or a refusal naming what would not open.

    The path is resolved before anything opens, and the RESOLVED path is what
    opens, so the file a refusal names is the file that was tried.

    There is no directory to hold this one inside: an operator keeps their own
    secret where they keep it. What the check can still do is refuse a path
    that names nothing and say which flag carried it, which beats a ValueError
    raised from inside `open` with no flag attached.

    Every message quotes the path and never the contents, because the contents
    are the secret.
    """
    try:
        resolved = checked_path(path, what)
    except PathRefused as refusal:
        raise ValueError(str(refusal)) from refusal
    try:
        with open(resolved) as handle:
            return handle.read().strip()
    except OSError as problem:
        raise ValueError(f"{what} {resolved!r} could not be read: "
                         f"{problem.__class__.__name__}: "
                         f"{problem.strerror or problem}")


def counted(pattern, text):
    """The integer a reclaim summary line reports, or zero if it said nothing.

    Every pattern it is called with is anchored at the start of a line on
    purpose. A loose substring match over this output produced a wrong
    scoreboard once, and the wrong number reached three issues before anyone
    caught it.
    """
    match = pattern.search(text)
    return int(match.group(1)) if match else 0


# --elasticsearch comes from configuration, not from the network, but
# configuration is not the same as trusted: it can be wrong, templated from
# somewhere else, or a copy-paste of the wrong value. urlopen does not care,
# and will happily open file:// or ftp://. This harness's own calls only ever
# need http or https, so anything else is refused before it is opened.
_ALLOWED_ES_SCHEMES = ("http", "https")


def refuse_non_http_scheme(url, what):
    scheme = urllib.parse.urlsplit(url).scheme
    if scheme not in _ALLOWED_ES_SCHEMES:
        raise ValueError(
            f"{what} is {url!r}; only http and https are accepted, so a "
            f"{scheme or '(no scheme)'!r} value cannot be opened")


def es_call(args, path):
    url = args.elasticsearch.rstrip("/") + path
    refuse_non_http_scheme(url, "--elasticsearch")
    req = urllib.request.Request(url)
    token = base64.b64encode(
        f"{args.es_user}:{args.es_password}".encode()).decode()
    req.add_header("Authorization", "Basic " + token)
    # refuse_non_http_scheme() above already confirmed only http or https
    # reaches this call.
    with urllib.request.urlopen(req, timeout=60) as r:  # nosec B310
        return json.loads(r.read())


def snapshots_in_flight(args):
    d = es_call(args, f"/_snapshot/{args.repository}/_all?ignore_unavailable=true")
    return sum(1 for s in d.get("snapshots", []) if s.get("state") == "IN_PROGRESS")


def emptiest_shard(args):
    """Documents in the least populated primary shard of the data stream.

    A shard with no documents is the one that produces a file-less shard
    document when a snapshot catches it, so this is the number the segment
    path actually depends on.
    """
    raw = es_call(args, f"/_cat/shards/{args.data_stream}*?format=json&h=prirep,docs")
    counts = [int(r.get("docs") or 0) for r in raw if r.get("prirep") == "p"]
    return min(counts) if counts else 0


def snapshot_uuids(args):
    """The uuid of every SUCCESS snapshot the repository currently lists.

    A count would be simpler and would be wrong. SLM retention removes expired
    snapshots while new ones are being taken, so the count is a level that
    falls as well as rises, and subtracting two readings of it answers "how
    many more are there now", not "how many new ones completed". Identities
    subtract correctly under retention; counts do not.
    """
    d = es_call(args, f"/_snapshot/{args.repository}/_all?ignore_unavailable=true")
    return {s["uuid"] for s in d.get("snapshots", [])
            if s.get("state") == "SUCCESS"}


def wait_until_ready(args, log):
    """Hold until every shard directory has a current document naming files.

    Populated shards are necessary and NOT sufficient, which a smoke test
    caught. A shard directory whose current document was written while the
    shard was empty stays poisoned after the shard fills, because that
    document is still the current one. Only a LATER snapshot supersedes it.

    So this waits for three things in order: no snapshot in flight, every
    primary shard populated, and then at least one further snapshot completed
    after that point. The third is what replaces the file-less documents.
    """
    deadline = time.time() + args.settle_timeout
    baseline = None
    while time.time() < deadline:
        flight = snapshots_in_flight(args)
        docs = emptiest_shard(args)
        populated = docs >= args.min_docs_per_shard
        if populated and baseline is None:
            baseline = snapshot_uuids(args)
            log(f"    shards populated ({docs} docs); waiting for "
                f"{args.fresh_snapshots} snapshot(s) to supersede any "
                "file-less documents")
        if populated and baseline is not None:
            fresh = len(snapshot_uuids(args) - baseline)
            if flight == 0 and fresh >= args.fresh_snapshots:
                return True, f"ready/docs={docs}/fresh={fresh}"
        log(f"    waiting: inflight={flight} emptiest={docs} "
            f"want>={args.min_docs_per_shard}")
        time.sleep(args.settle_poll)
    return False, "timeout/audited-anyway"


def run(cmd, out_path, timeout):
    """Run one subprocess, keeping BOTH streams in the file the counts come from.

    The audit writes its report to stderr and the reclaim writes its tally,
    `deleted:`, `failed:` and `unconfirmed:`, to stdout. Capturing only stderr
    meant every execute reported zero deleted while objects really were being
    removed, confirmed against a store answering 404 afterwards. Worse, the
    check that ends a run reads those same three numbers, so a batch with
    failed or unconfirmed keys reported zero and the run carried on deleting.

    Written stderr first, then stdout, rather than interleaved, so the file is
    the same every time and a later reader is not at the mercy of scheduling.
    """
    completed = subprocess.run(cmd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=timeout,
                               text=True, cwd=ROOT)
    with open(out_path, "w") as fh:
        fh.write(completed.stderr or "")
        fh.write(completed.stdout or "")
    return completed.returncode, completed.stdout


def transport_flags(args):
    if args.transport == "oci":
        return ["--transport", "oci", "--namespace", args.namespace,
                "--oci-region", args.region]
    return ["--transport", "s3", "--endpoint", args.endpoint,
            "--region", args.region]


def reclaim_command(args, manifest):
    """The reclaim invocation, including the corroboration choice it requires.

    `--execute` refuses unless it is told whether the Elasticsearch veto was
    re-checked against the cluster as it is NOW, because the manifest's
    protection was decided when it was derived and a searchable snapshot
    mounted since would not be in it.

    This harness did not pass either flag once that gate existed, so every
    execute refused and every cycle reported deleted=0 while the audit
    underneath was working perfectly. The tell was an execute file that
    existed and held a refusal rather than a tally.
    """
    command = [sys.executable, "-m", "generation_chain.reclaim",
               "--manifest", manifest, "--endpoint", args.endpoint,
               "--region", args.region, "--bucket", args.bucket,
               "--prefix", args.prefix, "--credentials", args.credentials]
    if args.elasticsearch and args.repository:
        command += ["--elasticsearch", args.elasticsearch,
                    "--es-repository", args.repository]
    else:
        command.append("--without-elasticsearch")
    return command


def cycle(args, n, mode, outdir, log):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    note = "not waited"
    if mode == "segment":
        ok, note = wait_until_ready(args, log)

    manifest = artifact(outdir, f"manifest-{n}.tsv")
    derive = artifact(outdir, f"derive-{n}.txt")
    cmd = [sys.executable, "-m", "generation_chain"] + transport_flags(args) + [
        "--bucket", args.bucket, "--prefix", args.prefix,
        "--credentials", args.credentials, "--manifest", manifest]
    if args.repository and args.elasticsearch:
        cmd += ["--elasticsearch", args.elasticsearch,
                "--es-repository", args.repository]
    rc, _ = run(cmd, derive, args.timeout)

    report = read_text(derive)
    m = SEGMENTS_READ.search(report)
    shards_read = f"{m.group(1)}/{m.group(2)}" if m else "?"
    r = RECLAIMABLE.search(report)
    reclaimable = r.group(1).strip() if r else ""

    segs = 0
    if os.path.exists(manifest):
        with open(manifest) as fh:
            next(fh, None)
            segs = sum(1 for line in fh if "/__" in line.split("\t")[0])

    deleted = failed = unconfirmed = 0
    time.sleep(EXECUTION_GAP_SECONDS)
    dry = artifact(outdir, f"dry-{n}.txt")
    base = reclaim_command(args, manifest)
    run(base, dry, args.timeout)
    text = read_text(dry)
    dg, rw = DIGEST.search(text), ROWS.search(text)
    if dg and rw and not args.dry_run_only:
        time.sleep(EXECUTION_GAP_SECONDS)
        ex = artifact(outdir, f"exec-{n}.txt")
        run(base + ["--execute", "--approve-digest", dg.group(1),
                    "--approve-rows", rw.group(1),
                    "--report", artifact(outdir, f"report-{n}.jsonl")],
            ex, args.timeout)
        got = read_text(ex)
        deleted = counted(DELETED, got)
        failed = counted(FAILED, got)
        unconfirmed = counted(UNCONFIRMED, got)

    return {"cycle": n, "utc": stamp, "mode": mode, "settle": note,
            "shards_read": shards_read, "segments_condemned": segs,
            "deleted": deleted, "failed": failed, "unconfirmed": unconfirmed,
            "reclaimable": reclaimable, "exit": rc}


def corroboration_credential_problem(args):
    """Why the audit's Elasticsearch corroboration cannot work, or None.

    `--es-user` and `--es-password-file` authenticate THIS harness's own calls,
    the ones driving the settle wait. They do not reach the audit, which is a
    separate process reading its cluster credential from the `elasticsearch`
    section of the file named by `--credentials`. That function does not fall
    back to the environment once a file is given, so a file without the
    section refuses on cycle 1, every time.

    Checked before the first cycle rather than discovered during it, and the
    message never quotes the file, which is full of secrets.
    """
    if not getattr(args, "elasticsearch", None):
        return None
    path = getattr(args, "credentials", None)
    advice = ("Add an 'elasticsearch' section holding either 'api_key', or "
              "'username' and 'password'. --es-user and --es-password-file "
              "authenticate this harness only; they never reach the audit.")
    try:
        with open(path) as handle:
            section = json.load(handle).get("elasticsearch")
    except (OSError, ValueError) as exc:
        return (f"--elasticsearch was given, so {path} must be readable JSON "
                f"carrying the audit's cluster credential ({exc.__class__.__name__}). "
                + advice)
    if not isinstance(section, dict) or not (
            "api_key" in section
            or ("username" in section and "password" in section)):
        return (f"--elasticsearch was given, but {path} has no usable "
                "'elasticsearch' section, so the audit will refuse on cycle 1 "
                "and the whole run will be wasted. " + advice)
    return None


def run_cycles(args, tsv, columns, log):
    """Drive the cycles, and stop the moment a cycle stops meaning anything.

    Three conditions end a run early and all three are the same kind of thing:
    a cycle whose result can no longer be trusted. A failed or unconfirmed
    delete says the repository has a problem. A non-zero exit says the AUDIT
    has a problem, and that one was missed once at real cost: launched from
    outside the repository the audit could not import its own package, every
    cycle exited 1, and the loop carried on writing tidy rows of zeroes. A
    hundred of those read exactly like a hundred cycles that found nothing.
    """
    totals = {"deleted": 0, "failed": 0, "unconfirmed": 0, "segments": 0}
    for n in range(args.start, args.start + args.cycles):
        mode = args.mode
        if mode == "mixed":
            mode = "segment" if n % 2 else "metadata"
        log(f"=== cycle {n} [{mode}] ===")
        row = cycle(args, n, mode, args.out, log)
        with open(tsv, "a") as fh:
            fh.write("\t".join(str(row[c]) for c in columns) + "\n")
        totals["deleted"] += row["deleted"]
        totals["failed"] += row["failed"]
        totals["unconfirmed"] += row["unconfirmed"]
        totals["segments"] += row["segments_condemned"]
        log(f"  shards {row['shards_read']}  segments {row['segments_condemned']}"
            f"  deleted {row['deleted']}  failed {row['failed']}"
            f"  unconfirmed {row['unconfirmed']}")
        if row["exit"]:
            log(f"  STOPPING: the audit exited {row['exit']} on cycle {n}. "
                f"Its output is in {args.out}/derive-{n}.txt. Nothing was "
                "audited, so the zeroes above mean nothing.")
            break
        if row["failed"] or row["unconfirmed"]:
            log(f"  STOPPING: failed={row['failed']} "
                f"unconfirmed={row['unconfirmed']} on cycle {n}")
            break
        time.sleep(args.sleep)
    return totals


def main():
    p = argparse.ArgumentParser(
        description="Exercise the metadata and segment paths of the audit "
                    "against a live repository, repeatedly.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    p.add_argument("--cycles", type=int, default=100,
                   help="how many audit-then-reclaim cycles to run. Tens of "
                        "thousands is fine; state is per cycle and the run "
                        "resumes with --start (default: 100)")
    p.add_argument("--start", type=int, default=1,
                   help="first cycle number, to resume an interrupted run")
    p.add_argument("--mode", choices=("mixed", "metadata", "segment"),
                   default="mixed",
                   help="segment waits for a complete shard view before each "
                        "audit; metadata does not; mixed alternates, which is "
                        "the only setting that exercises both (default: mixed)")
    p.add_argument("--min-docs-per-shard", type=int, default=1000,
                   help="in segment mode, hold until the emptiest primary "
                        "shard holds this many documents. A shard with none "
                        "produces a file-less shard document that the parser "
                        "refuses, and that one document drops its whole "
                        "directory. 1000 is comfortably past the rollover "
                        "window at any sane ingest rate (default: 1000)")
    p.add_argument("--settle-timeout", type=int, default=600,
                   help="give up waiting after this many seconds and audit "
                        "anyway, recording that it did not settle. A harness "
                        "that waits forever reports nothing (default: 600)")
    p.add_argument("--fresh-snapshots", type=int, default=2,
                   help="in segment mode, how many snapshots must complete "
                        "AFTER the shards are populated. A shard directory "
                        "whose current document was written while the shard "
                        "was empty stays unreadable until a later snapshot "
                        "replaces that document, so waiting for populated "
                        "shards alone is not enough. Two gives margin for a "
                        "snapshot that starts before the shard fills "
                        "(default: 2)")
    p.add_argument("--settle-poll", type=int, default=15)
    p.add_argument("--sleep", type=int, default=30,
                   help="seconds between cycles. Below the retention period "
                        "most cycles find nothing, because no snapshot has "
                        "expired since the last one (default: 30)")
    p.add_argument("--transport", choices=("s3", "oci"), default="s3")
    p.add_argument("--endpoint", required=True)
    p.add_argument("--region", required=True)
    p.add_argument("--namespace", help="required for --transport oci")
    p.add_argument("--bucket", required=True)
    p.add_argument("--prefix", required=True)
    p.add_argument("--credentials", required=True,
                   help="the JSON credentials file the AUDIT reads. With "
                        "--elasticsearch it must also carry an "
                        "'elasticsearch' section, because the audit takes no "
                        "cluster credential from this harness")
    p.add_argument("--elasticsearch",
                   help="ask the cluster what to protect while deriving. "
                        "Needs an 'elasticsearch' section in --credentials; "
                        "checked before the first cycle rather than "
                        "discovered during it")
    p.add_argument("--es-user", default="elastic",
                   help="user for THIS harness's own calls to the cluster, "
                        "the ones driving the segment-mode wait. It does not "
                        "reach the audit (default: elastic)")
    p.add_argument("--es-password-file",
                   help="a PATH, for this harness's own calls only. A secret "
                        "in argv is visible in ps. The audit reads its "
                        "cluster credential from --credentials instead")
    p.add_argument("--repository")
    p.add_argument("--data-stream", default="",
                   help="data stream whose shards are checked in segment mode")
    p.add_argument("--out", required=True)
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--dry-run-only", action="store_true",
                   help="audit and dry run, never execute")
    args = p.parse_args()

    if args.transport == "oci" and not args.namespace:
        p.error("--transport oci needs --namespace")
    if args.elasticsearch:
        try:
            refuse_non_http_scheme(args.elasticsearch, "--elasticsearch")
        except ValueError as exc:
            p.error(str(exc))
    if args.mode != "metadata" and not args.data_stream:
        p.error("segment mode needs --data-stream to check shard population")
    args.es_password = ""
    if args.es_password_file:
        try:
            args.es_password = read_secret_file(args.es_password_file,
                                                "--es-password-file")
        except ValueError as exc:
            p.error(str(exc))

    problem = corroboration_credential_problem(args)
    if problem:
        p.error(problem)

    # Checked and resolved once, here, so every artifact path below is a join
    # onto a directory that exists and has already been through the
    # filesystem, and so the directory that gets created is the one a refusal
    # here would have named.
    try:
        args.out = checked_path(args.out, "--out")
    except PathRefused as refusal:
        p.error(str(refusal))
    try:
        os.makedirs(args.out, exist_ok=True)
    except OSError as exc:
        p.error(f"--out {args.out!r} could not be created: "
                f"{exc.__class__.__name__}: {exc.strerror or exc}")
    tsv = artifact(args.out, "cycles.tsv")
    if not os.path.exists(tsv):
        with open(tsv, "w") as fh:
            fh.write("\t".join(COLUMNS) + "\n")

    def log(msg):
        print(msg, flush=True)

    totals = run_cycles(args, tsv, COLUMNS, log)

    log(f"=== totals over {args.cycles} cycles ===")
    for k, v in totals.items():
        log(f"  {k}: {v}")


if __name__ == "__main__":
    main()
