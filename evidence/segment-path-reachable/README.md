# What actually stops the segment path, and what does not

Measured on 2026-08-26 and 2026-08-27, first against Oracle Cloud Infrastructure
Object Storage over the Amazon S3 Compatibility API in `us-ashburn-1`, then
against a local MinIO as a control. Both runs drove the shipping audit rather
than hand-built requests. The tenancy namespace, the repository uuid, index
uuids and local paths are replaced with placeholders. Nothing else is edited.

Four terms, because this file is read on its own.

**The rig** is the reproduction of the fault: Elasticsearch 9.5.2 under ECK in
Rancher Desktop, with a load generator writing, an index lifecycle policy
rolling data to object storage, and a snapshot policy on a short cycle. Nothing
is paused while a measurement is taken.

**A shard directory** is `indices/<index-uuid>/<n>/` inside the repository. It
holds one shard's segment blobs and the documents listing them.

The audit has two halves. **The metadata path** condemns snapshot and index
metadata documents and needs only the chain of root generations. **The segment
path** condemns data blobs, needs a complete view of a shard directory, and is
the half with real blast radius, because a wrongly named segment is
unrecoverable.

## The correction, stated first

An earlier version of this file said the governing variable is the NUMBER of
shard directories. **That was wrong, and the number it rested on measured
something else.** The correlation was real. The cause was not.

The MinIO control below holds 22 shard directories and reads 10 of them,
condemning 816 segment blobs. An earlier run on the same repository with only
16 directories read none at all. More directories, better reads. A count that
can move in both directions is not the cause of anything.

The reason the first reading looked convincing is a design error in the first
experiment, and it is worth naming because it is a standard trap. The two
configurations compared there moved TWO lifecycle knobs at once, how far away
rollover sits and how long the delete phase waits. Two knobs, one comparison,
so neither could be attributed. The control added a third arm that moves only
rollover, and that is the arm that separates them.

**The governing variable is whether any directory in a snapshot's declared
extent carries a current shard document with a file-less entry.** Rollover
manufactures those, because a snapshot that catches a freshly rolled backing
index finds a shard with no documents in it yet. Directory count rises with
rollover, which is why it tracked the failure without causing it.

## Why one bad document costs so much

Two channels, both visible in [`drop-reasons.txt`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/segment-path-reachable/minio-control/drop-reasons.txt).

The seed is a shard document listing no files. Elasticsearch writes one when it
snapshots an empty shard, and the parser refuses it, because a document naming
nothing satisfies the subset test against every directory. That refusal is
correct and load bearing, and nothing here relaxes it:

    indices/<index-uuid>/0 was dropped whole: the current document index-<gen>
    could not be read

The contagion is the extent check. A snapshot declares how many shards it
covers, so one refused directory makes the count come up short, and every other
directory that snapshot named drops with it:

    indices/<index-uuid>/1 was dropped whole: snapshot '<name>' declares 12
    shard(s) in total and this run read 2

That is why the failure looks all or nothing per snapshot, and why it appeared
to track directory count. More directories per snapshot means more chances that
one of them is poisoned, and one is enough to take the rest.

## The MinIO control

Same tool, same cluster, a local MinIO pinned to `RELEASE.2025-01-18T00-31-37Z`,
the last release that rejects the batch delete. It genuinely rejected it here,
so the fault was reproduced rather than assumed:

    Missing required header for this request: Content-Md5.
    (Service: S3, Status Code: 400)

Elasticsearch reported 40 snapshots expired with zero deletion failures while
the bucket kept 2,219 objects, which is the silent-success property the whole
project exists for.

| Arm | Rollover | Delete `min_age` | Shard dirs | Read | Segment blobs |
|---|---|---|---|---|---|
| A | 24h / 100000000 | 2m | 2 | 2 of 2 | 120 |
| B | 2m / 5000 | 30m | 4 | 4 of 4 | 120 |
| B | 2m / 5000 | 30m | 8 | **0 of 8** | 0 |
| B | 2m / 5000 | 30m | 12 | **0 of 12** | 0 |
| B | 2m / 5000 | 30m | 16 | **0 of 16** | 0 |
| C | 24h / 100000000 | 30m | 22 | **10 of 22** | 816 |

Arm B and Arm C differ in one thing, rollover. Arm C keeps the long delete
phase, so its directories accumulate and stay; it holds more of them than any
Arm B run and reads better than all of them. Per-run rows in
[`runs.tsv`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/segment-path-reachable/minio-control/runs.tsv), policies in
[`ilm-armA.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/segment-path-reachable/minio-control/ilm-armA.json),
[`ilm-armB.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/segment-path-reachable/minio-control/ilm-armB.json) and
[`ilm-armC.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/segment-path-reachable/minio-control/ilm-armC.json).

**The mechanism is endpoint independent.** Every drop across every run was one
of the two channels above, and both are arithmetic over repository-format data.
`EXTENT_UNREADABLE`, the one reason that fires when the store cannot serve a
blob, fired zero times. The Oracle framing in the first version of this file was
incidental.

## The Oracle runs, and what they do and do not show

These were taken with the rig held to one backing index, which is Arm A's
configuration.

| Run | Root generation | Read | Segment blobs | Orphaned |
|---|---|---|---|---|
| [standalone](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/segment-path-reachable/derive-standalone.txt) | 118 | 2 of 2 | 124 | 283, 5.39 MB |
| [cycle 1](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/segment-path-reachable/derive-cycle1.txt) | 142 | 2 of 2 | 252 | 471, 68.5 MB |
| [cycle 2](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/segment-path-reachable/derive-cycle2.txt) | 155 | 2 of 2 | 380 | 639, 135.16 MB |
| [cycle 3](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/segment-path-reachable/derive-cycle3.txt) | 170 | 2 of 2 | 508 | 807, 197.94 MB |

They remain a true record that the segment path condemns on a real Oracle
bucket, which had never been shown before. They are NOT evidence about
directory count, because that configuration moved two knobs. Read them as "the
segment path works end to end against Oracle", not as "small directory counts
cause reads".

The three cycles ran `--dry-run-only`, so nothing was deleted and the zero
columns are structural. Each cycle inherits the previous cycle's orphans, which
is why the counts climb; the MinIO runs held flat at 120 instead, because
snapshot retention there fires on a five minute cron and orphan creation is
bursty. Neither pattern is a law, both are properties of a cadence.

`unexplained` held at 1,072 objects across all four Oracle runs. Those are
blobs in 24 shard directories whose indices no live snapshot references, so the
audit established no live set there and reports them undecided rather than
condemning them. That is the conservative direction working.

The root generation climbed 118 to 170 in forty minutes. The audit reads one
shard document per directory per generation and never condemns a generation,
because its own derivation reads them, so each pass costs more than the last.
That is [issue #9](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/issues/9).
Oracle derives took over ten minutes at 118 generations; MinIO derives took
under five seconds at fewer than 70. Do not compare those wall clocks. Nothing
here rests on them.

## A defect the Oracle runs exposed, fixed and confirmed

Every Oracle cycle recorded `settle=timeout/audited-anyway`. The wait should
have held until two further snapshots completed, and two complete every two
minutes, so it should never have timed out at five.

It compared two readings of HOW MANY SUCCESS snapshots exist and treated the
difference as how many new ones had completed. That is a level, not a counter.
Snapshot retention removes expired snapshots while new ones are taken, so it
falls as well as rises. Sampled every eighteen seconds on Oracle it went 10, 5,
6. On MinIO, 48 samples over thirteen minutes show the same shape, 10 then 5
fifteen seconds later, then back up through 6, 7, 8:
[`snapshot-count-samples.txt`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/segment-path-reachable/minio-control/snapshot-count-samples.txt).

The bug never involved the object store. The count is a property of the
Elasticsearch catalog under retention.

Comparing snapshot identities instead of counting them subtracts correctly under
retention. With the fix, both MinIO cycles settled in about ninety seconds and
reported `ready`, against three Oracle cycles that timed out at 600 seconds:
[`protocol-cycles.tsv`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/segment-path-reachable/minio-control/protocol-cycles.tsv).

## Open

Six shard directories in Arm C never recovered, stuck on "the current document
could not be read" across five consecutive runs. Their uuids match no live
index, so they are pre-mount backing indices whose directories survive under the
frozen searchable snapshots. Something about the frozen conversion appears to
leave those documents file-less permanently rather than until a later snapshot
supersedes them. If that holds, it is a THIRD source of poisoning, and it means
Arm A's real lever may have been the short delete phase draining frozen indices
rather than the rollover change. Not isolated yet.

The knee was never sampled. Arm B jumped from 4 directories to 8 between
audits, so the count where reads start failing is bracketed, not measured.
