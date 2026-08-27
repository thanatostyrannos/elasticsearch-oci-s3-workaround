# The campaign this came from

`SKILL.md` is the method. This is the case it was extracted from, with the
measurements, kept separate because a skill is loaded every time it is used and
a narrative is not what you want in front of you while you work.

The system: a read-only tool that identifies objects in an Elasticsearch
snapshot repository which a delete should have removed and did not. A wrong
answer gets a human to delete live data from a store with no recovery path. The
rig, a lab that exercises a dangerous tool continuously, was meant to exercise it against a repository real
Elasticsearch writes, standing in for a cluster of about 16 billion documents
across roughly 6,000 shards.

Four things went wrong. None of them looked wrong while it was happening.

## The constraint was not where it looked

The opening question was how to give a Kubernetes cluster more disk. Measuring
the ceilings instead:

| Resource | Headroom | Objects supported |
|---|---|---|
| Repository storage, Longhorn | 1,842 GB | about 235M at 8 KB each |
| Memory on the auditing machine | 38 GB | about 20M at 1.9 KB each |

Memory bound first by more than ten to one. Every plan to add storage would
have bought capacity nothing could process. The 8 KB figure was measured on the
lab's own small blobs rather than estimated, which mattered: a 5x error there
moves the disk ceiling below the memory one and inverts the answer.

The storage layer under consideration was also wrong for a second reason. It
replicates three times by default, which on a single node costs triple for no
redundancy, and its data path must be a real Linux filesystem, which a Windows
drive mounted over 9p is not.

## Synthesizing the corpus was rejected, twice over

The plan was to generate a repository directly into object storage, because 16
billion documents does not fit in a lab.

It was circular. A corpus generated from the project's own model of the format
confirms the model rather than testing it, and reading what Elasticsearch
actually writes is the entire point of the tool.

It was also unnecessary. Object count comes from shards and snapshots, not from
documents. A shard holding one small document still writes a full set of
snapshot blobs, so real Elasticsearch produces real object counts without real
data volume. The frozen tier makes the shard count affordable, because a frozen
shard mounts from the object store and costs almost no heap.

## The first configuration produced an easy graph

Rollover fired every 5 seconds. Snapshots were taken every 2 minutes. Every
index rolled and force merged into its final state long before any snapshot saw
it, so every snapshot captured an identical blob set.

Measured: the deepest index appeared in 3 snapshots, with no evolution between
them.

That is the easy case. A tool reasoning about liveness is hard to break with
inputs where every holder references the same set. The case that finds defects
is partial overlap, where a blob is held by several snapshots and loses them one
at a time, so its liveness depends on which survive. The rig was large and
trivially easy at once.

## Two triggers fired at the same instant

`rollover max_age 30m` and `rollover max_docs 3,600,000`, at 2,000 documents per
second, are the same moment. Any increase in ingest would have made the document
count fire first, shortening the window and collapsing the overlap back to where
it started.

More load would have produced a simpler graph. That is the shape of failure
worth internalising: not a crash, not an error, just a rig quietly measuring
something easier than it was built for while every dial reads as expected.

Raising `max_docs` to a billion left `max_age` governing alone. After that,
ingest changed how large a unit got inside a fixed window instead of changing
the window, and the knobs became independent.

## The rig had not started leaking

The first audits reported zero orphans against a rig whose entire purpose is
manufacturing them. That looked like a result. Snapshot retention was 40
minutes, so nothing had expired, and every audit had run inside that window.

```
retention_runs 16   expired 0
```

The tool was correct. The generator had not begun. A generator that has not
generated reads exactly like a healthy system, which is why counting the
specific event is a step of its own.

Retention was then shortened to 10 minutes, still far too slow: one event a
minute at best, with a ten minute wait for the first. The final shape takes a
snapshot every 15 seconds and expires after one minute:

```
taken 19   deleted 15   deletion_failures 0   retention_runs 31
```

Fifteen deletions, each stranding its blobs, each reported as successful. About
four events a minute.

## Two targets, kept apart

A repository being rewritten while it is read behaves differently from a settled
one, and the numbers are not comparable.

Audits against the live target explained 0 percent of the history, because the
documents they needed were replaced between the listing and the fetch. The tool
refused rather than guessed, which is correct, and it means a target under
active churn cannot be usefully measured at all. That is worth knowing before
someone schedules an audit against a production repository mid-retention.

The quiesced target refused for a different reason, and that turned out to be a
genuine gap in the tool rather than a property of the rig.

## What the rig is now

The environment above was assembled by hand, on Kubernetes, and every knob in
this account had to be found the hard way. This repository now ships a rig of
the same shape as a single script,
[`snapshot_churn_rig.py`](../../snapshot_churn_rig.py):
standard library only, no Kubernetes and no container. It drives a cluster you
already have, creates its own data stream, ILM policy and SLM policy under one
namespace prefix, generates the load itself, and tears down exactly what it
created.

```bash
python3 snapshot_churn_rig.py run \
  --es https://your-cluster:9200 --user elastic --password-file espw \
  --prefix octest --repo-type s3 --bucket your-bucket --base-path octest \
  --docs-per-second 200 --snapshot-interval 30s --retention 5m --duration 2h
```

Each argument this campaign settled by hand is a flag on it:

| What the account above argues about | The flag |
|---|---|
| How often the generator manufactures a leak | `--snapshot-interval` |
| The retention window that was 40 minutes and produced nothing | `--retention` |
| SLM's own retention check, which defaults to once a day | `--retention-check-interval` |
| Ingest rate, the 2,000 documents a second | `--docs-per-second`, `--doc-bytes` |
| The two rollover triggers that fired at the same instant | `--rollover-max-age`, `--rollover-max-docs` |
| ILM's poll interval, which stalls every short-age phase at its 10 minute default | `--ilm-poll-interval` |

Reproduction recipe and what to measure while it runs:
[the churn rig](../../docs/churn-rig-methodology.md).
How to choose a rate, and the poll-interval trap that wastes an hour:
[generating load](../../docs/generating-load.md).
