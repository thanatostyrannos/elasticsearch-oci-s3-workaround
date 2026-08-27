# Test cycles against a live Oracle bucket, 2026-08-27

Two runs of the audit-and-reclaim loop, back to back, against Oracle Object
Storage over the S3 compatibility API. The tenancy namespace, index uuids and
local paths are replaced with placeholders. Nothing else is edited.

The second run exists because of what the first one measured.

## What was run

`reclaim_test_protocol.py` in `--mode mixed`, which alternates two things:

- **segment mode** waits until no snapshot is in flight and every primary shard
  holds documents, then audits. It exercises the path that condemns data blobs.
- **metadata mode** audits without waiting, which is the condition the guards'
  refusal behaviour is written for.

Each cycle audits, dry runs, then executes against the digest that dry run
printed. A manifest that changed in between fails the cycle rather than
deleting under a stale approval. Deletes were real.

The repository underneath was manufactured on purpose by
`snapshot_churn_rig.py`: a load generator writing 60 documents a second, ILM
rolling, and SLM snapshotting every 60 seconds with a five minute retention, so
snapshots expire continuously and each expiry strands blobs the store refuses
to delete. Configuration in [`rig-configuration.txt`](rig-configuration.txt),
including the line proving the fault reproduced at registration.

## Run one: 80 cycles, before the reads overlapped

[`cycles-before-readahead.tsv`](cycles-before-readahead.tsv)

    cycles           80
    deleted       2,896
    failed            0
    unconfirmed       0
    non-zero exits    0
    shard directories read    2 of 2, on all 80

Every segment-mode cycle settled `ready`, 40 out of 40, never once timing out.

This run is also the measurement that produced the second one. Cycle time
tracked generation count almost linearly:

    cycles  2-20     2.0 min each
    cycles 21-40     2.6 min
    cycles 41-60     4.1 min
    cycles 61-78     7.1 min

The audit reads one shard document per shard directory per generation, and
nothing ever removes a generation, so the work grows as the fault goes unfixed.
That much was known. What the numbers exposed was that those reads were
**serial**, while a read-ahead layer, a bounded thread pool and a
`--concurrency` flag had existed for some time with exactly one caller: the
root generations. The shard documents, which are the bulk of the work, were
fetched one at a time with eight workers idle.

A separate measurement on that repository at 448 generations with
`--concurrency 1` took 587 seconds.

## Run two: the same thing with the shard reads warmed

[`cycles.tsv`](cycles.tsv), against a repository rebuilt from zero so the two
are comparable.

Durations attributed to the cycle they belong to rather than the one that
follows:

    metadata-mode cycles      26s average   (12s at the fastest)
    segment-mode cycles      124s average

The metadata cycles are the honest measure of the change, because they are
audit and reclaim with no waiting: minutes became seconds. The segment cycles
are dominated by a wait that is deliberate and has nothing to do with reading,
namely holding until two further snapshots complete at a 60 second cadence.

## What these numbers do and do not transfer

**Counts and orderings transfer. Rates do not.** This rig snapshots every 60
seconds, which is roughly sixty times an hourly production schedule, and
manufactures garbage at a rate no real cluster approaches. A per-cycle time
here is a property of that cadence.

What does transfer is the shape: a repository that has been leaking longer
costs more to audit, because generations accumulate and nothing removes them.
That is [issue #9](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/issues/9),
and it is the reason a hundred-cycle run should start from a repository just
created rather than one carried over.

## Run two, at the point the testing release was cut

    cycles                    56
    deleted                  804
    failed                     0
    unconfirmed                0
    non-zero exits             0
    shard directories read    2 of 2, on all 56

    metadata-mode cycles      26s average
    segment-mode cycles      129s average

The run continued past this point. This is the state the v0.8.0-testing
archive was built from, recorded so the release and the evidence describe the
same moment rather than drifting.

## Samples

[`derive-sample.txt`](derive-sample.txt) is one audit's full report, including
its coverage accounting and dispositions. [`exec-sample.txt`](exec-sample.txt)
is one reclaim, showing the manifest digest it was approved against and the
per-key tally it wrote afterwards.
