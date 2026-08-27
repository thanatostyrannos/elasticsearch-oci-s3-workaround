# The churn rig: a repository that misbehaves on schedule

`snapshot_churn_rig.py` stands up an Elasticsearch snapshot repository that
churns continuously, so that tooling which classifies and reclaims leaked
objects can be measured against something that behaves like production. The
script is the reproduction recipe: anyone with an Elasticsearch cluster that
has a frozen-capable node and an S3 style bucket can rebuild the same
environment from a copy of the file and take the same measurements.

## What it builds

Everything lives under one namespace prefix (default `churnrig`) and one
`base_path` in the bucket, so the rig can share a cluster and a bucket with
other work and teardown can remove exactly what it created.

* A data stream that the script feeds continuously at a configurable rate
  and document size. A data stream rather than an alias with a write index,
  because it is what production log ingestion creates, and rollover needs no
  bootstrap step that the script would have to fabricate by hand.
* An ILM policy that rolls backing indices out of hot, converts them to
  partial searchable snapshots in the frozen tier, and later deletes both
  the index and its backing snapshot.
* An SLM policy that snapshots the stream on a schedule (default every 15
  minutes) and expires snapshots past a retention window (default one
  hour), with the retention check itself rescheduled to run every few
  minutes instead of the cluster default of once a day.

Elasticsearch manufactures every object in the repository and issues every
delete. The script never places or removes a blob during the
run. Against a store that rejects the batch `DeleteObjects` call, every
expiry and every ILM cleanup silently leaves its blobs behind, so the
lifecycle becomes a generator that produces one leak per snapshot interval,
indefinitely. The store's rejection usually shows up before the first
snapshot: repository registration verifies itself by writing test blobs and
batch-deleting them, and when that delete fails the script records the
rejection and re-registers with `verify=false`, the same registration
Elastic support prescribes for frozen repositories on such stores.

## Why churn instead of a fixture

A hand-built fixture proves the tool handles the bytes somebody thought to
put in it. A churning repository proves it handles the states Elasticsearch
actually passes through, including the ones nobody thought of: a snapshot
mid-write during a listing, a generation chain that grows while the tool
walks it, a mounted searchable snapshot whose source snapshot has already
been expired, stale root generations accumulating next to the live one.
Several of those states are exactly the hazards the reclaim tooling exists
around, and the rig produces them on a clock rather than by luck. When one
appears (a mount pinning a snapshot that is gone, for instance) the rig
reports it as a hazard event rather than preventing it, so a measurement can
be tied to the state that was live while it ran.

Reproducibility is the other half. A measurement taken against an
environment nobody can rebuild is an anecdote. This script, its arguments,
and its report file are the environment's full description.

## What to measure while it runs

The rig emits one JSON report per interval (and on demand via `status`) to
stdout and a JSONL file. A measurement campaign should cite these fields:

* `repository.object_count` and `bytes`: what actually exists in the store,
  from a signed listing, not from what Elasticsearch believes.
* `repository.root_generation_count`: stale `index-N` roots pile up beside
  the live one when deletes leak. On a healthy store this stays at 1.
* `repository.expired_snapshot_metadata_still_present`: `snap-*.dat` blobs
  whose snapshot Elasticsearch already deleted. Each one is a leaked delete.
* `snapshots.expired_total` against the repository counts above: the gap
  between what Elasticsearch removed and what the store gave back is the
  leak, measured from both sides.
* `snapshots.observed_start_deltas_s`: the snapshot cadence as observed,
  which is the check that the schedule fires at the rate intended.
* `mounted_searchable.hazards`: mounted indices whose source snapshot no
  longer exists. Run the classifier while one is live and record whether it
  protects those blobs.

Teardown removes only what the script created, restores the cluster
settings it changed, verifies both, and reports what it could not remove:
the leaked blobs themselves, which survive on purpose unless
`--purge-bucket` removes them with single-object deletes (the call the
store does accept). Keeping them is the point; a classification campaign
runs against exactly that corpus.

The state file written by `run` is what makes that first sentence true.
It records the names and the base path, so teardown deletes things this
harness is known to have made rather than things that happen to answer
to `--prefix`. Without it every name is a guess: the repository would be
`<prefix>-repo`, which is an ordinary name someone else may already have
taken. Teardown therefore refuses to run without a state file and prints
the names it would have used, and `--derive-from-prefix` is the opt-in
for the case where the state file is genuinely lost.

Purging the bucket is held to a stricter rule and stays refused even
under that opt-in, unless `--base-path` states the path outright. A
wrong index or repository can be rebuilt from the cluster. Objects
deleted out of this store cannot, which is the reason this repository
exists.
