# Evidence

Everything behind the claims in the [main README](../README.md). Nothing here is
summary. If a number appears in the documentation, its source is in this
directory.

> [!IMPORTANT]
> **New evidence added here must exercise live code.**
>
> The three original sweepers, `s3_repo_sweeper.py`, `oci_repo_sweeper.py` and
> `es_log_driven_sweeper.py`, are retired and removed. Their operating records
> went with them: both sweeper runbook transcripts, the test-results ledger and
> the live-blob-deletion reproduction. Why they were retired is in
> [the main README](../README.md#the-three-tools-that-were-removed), and git
> history keeps the removed files for anyone auditing the change.
>
> **Measurements of consequence were kept**, and the distinction is the whole
> point. What a wrong delete costs, what a mounted searchable snapshot is linked
> to, what a restore returns, what `_verify_integrity` fails to notice: these are
> properties of Elasticsearch and the object store, not of the tool that made the
> delete. The cost is the same whichever tool made it. Those files carry a banner
> saying which parts are history and which are current.

| File | What it is |
|---|---|
| [delete-campaign/](delete-campaign/README.md) | Twelve reclaim cycles run by `generation_chain` against a live, moving repository. Audit, dry run, execute and restore check per cycle. Evidence of the tool that ships. |
| [regression-10/](regression-10/README.md) | Ten audit-then-delete cycles against a live repository after a week of changes. 31,534 objects deleted, zero failed, zero unconfirmed, restore intact. |
| [oci-s3-compatibility/](oci-s3-compatibility/README.md) | What Oracle's endpoint accepts and rejects, measured on a real bucket. The fault reproduced, and the three algorithms that work. |
| [blast-radius-remeasure/](blast-radius-remeasure/README.md) | What a wrong delete costs, measured rather than argued. Tool-agnostic: it deletes blobs directly and observes what Elasticsearch then reports. |
| [methodology.md](methodology.md) | How the validation was done, written so someone else can run it. The sweep commands are history; the mounted-index and restore measurements are current. |
| [campaign-data.md](campaign-data.md) | Raw data from the two live-rig campaigns: every orphan key with its shard path, size, dates and classification, plus sizing, mounted linkage and restore proof. |
| [campaign-artifacts/](campaign-artifacts) | The files those campaigns produced: manifests, sizing output, mounted-snapshot exports, restore counts, the retention unlink proof. |
| [genchain-research/](genchain-research/README.md) | The derivation research behind `generation_chain`: ground truth, reproducers, and the harness that produced them. |
| [runbook-transcript-migrate-backups.md](runbook-transcript-migrate-backups.md) | A real terminal session running the migration runbook against a live cluster. Failures left in. |
| [capture-harness.sh](capture-harness.sh) | The wrapper that captured that transcript, so it can be reproduced. |

## What the evidence establishes

Each measured rather than argued, each with its source in this directory.

**A delete can be acknowledged and do nothing.** Elasticsearch reports a snapshot
deletion successful while the store has rejected the underlying batch call, so
the repository grows on a delete. This is the fault the whole project exists for.

**Nothing reclaims the residue on its own.** Sampled thirty times during live
churn, the object count never fell once, and Elasticsearch's own cleanup endpoint
returned zero bytes and zero blobs against a repository where almost nothing was
still referenced.

**The batch delete is not the fault.** It works exactly as documented when the
client sends `Content-MD5` instead of the CRC32 checksum header the SDK defaults
to. That is why reclaiming is a batch operation rather than one request per key.

**A repository can look healthy and be unrestorable.** A mounted index with all
eight of its data blobs deleted returned HTTP 200 with `total=200` and
`"failed": 0`. A `size=1` query missed the loss entirely, a `max` aggregation
returned a value from a destroyed segment, and closing and reopening the index
detected nothing.

**A restore is the only check that matters.** Every cheaper check has a way of
passing on a repository that cannot be restored, which is why
[delete-campaign/](delete-campaign/README.md) ends its cycles by restoring a real
index and counting documents.

## Corrections

Where something here turned out to be wrong, the correction sits next to the
original with the original left in place. A record edited to remove a wrong turn
stops being evidence of how anything was found.

Three are worth reading as a set. A defect filed as production-blocking and
reproduced fourteen times was a shell mistake, `$?` after a pipe reporting `tee`
rather than the tool. An extrapolation to 158 TB a year described the rig's
deliberately pathological cadence rather than any deployment. A memory finding
filed with confidence reversed on a second measurement, because one data point
cannot separate a fixed baseline from a per-object cost.

All three looked like findings. None survived being measured again.

## Issue numbers inside captured output

The `.txt` files here are the raw output of runs, kept exactly as the tool
produced them. Some of them print an issue number, and those numbers refer to
the private repository this project was developed in, not to the tracker on
this repository. Editing a captured run to renumber it would falsify the
record, so they are left alone. Prose that links to an issue has been moved to
the current numbering.
