# What a wrong delete actually costs, measured 2026-08-25

Thirteen of the twenty headline measurements in `docs/blast-radius.md` have no
artifact behind them. This campaign measured the same things again on a live
Elasticsearch 9.5.2 and left a file behind for every number.

Every row below names the artifact it came from. All artifacts are in
`artifacts/` next to this file, and `harness/run_all.sh` rebuilds all of it from
an empty bucket.

## The repositories

`base-s` reproduces the shape the document's headline was measured on: two
indices, `blast-share1` and `blast-share2`, across three snapshots, with
`blast-share2` written once and never touched again. It came out at **30
objects and 1,018,287 bytes**, against the document's 30 objects and 781,970
bytes. The object count matches exactly; the byte count differs because the
documents in this campaign's indices are larger.

Reference counts read out of the shard documents themselves
([`b-base-s-blobrefs.tsv`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b-base-s-blobrefs.tsv)):

| Data blob | Bytes | Snapshots naming it |
|---|---|---|
| `__27_yjuX2QPyUrE-2VlGx9A` (`blast-share2` `_0.cfe`) | 742 | 3 |
| `__6HfSS9VGSUy_86kMSv-_Kw` (`blast-share2` `_0.cfs`) | 486,768 | 3 |
| `__2cCGkm5uQf6Tw1qQ96W_ow` (`blast-share1` `_0.cfe`) | 742 | 3 |
| `__ocOz3nmvQoCISRh0va80RA` (`blast-share1` `_0.cfs`) | 486,768 | 3 |
| `__R1fKKxcwSd6OcfgtNcl99w` (`blast-share1` `_1.cfe`) | 742 | 1 |
| `__qBc58MdXQ2KxZm_FZECRfQ` (`blast-share1` `_1.cfs`) | 24,544 | 1 |

Composition ([`b-base-s-composition.tsv`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b-base-s-composition.tsv)):
6 data blobs are 20.0 percent of the objects and **98.23 percent of the bytes**.
The other 24 objects are 80.0 percent of the count and 1.77 percent of the
bytes. The document says four fifths of the objects carry one fiftieth of the
bytes. Measured: four fifths carry one fifty-sixth.

## Object class against measured damage

Each row is one delete on its own byte-identical clone of the base repository.
"Restores" counts the three snapshots in the repository.

| Exp | Object deleted | Bytes | % of bytes | `total_anomalies` | `result` | Anomaly class | Damaged index-snapshot pairs | Restores | Artifact |
|---|---|---|---|---|---|---|---|---|---|
| b0 | nothing (control) | 0 | 0 | 0 | pass | - | 0 | 3 clean | [`b0-summary.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b0-summary.json) |
| b1 | one **shared** data blob, 3 referrers | 486,768 | 47.803 | 3 | fail | missing blob | 3 | 3 partial, that index red with no documents in all three | [`b1-summary.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b1-summary.json) |
| b2 | one data blob, **1 referrer** | 24,544 | 2.410 | 1 | fail | missing blob | 1 | 2 clean, 1 partial | [`b2-summary.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b2-summary.json) |
| b3 | **every** data blob | 1,000,306 | 98.234 | **14** | fail | missing blob | **6 of 6** | 3 partial, every index red, no documents anywhere | [`b3-summary.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b3-summary.json) |
| b4 | root `snap-<uuid>.dat` | 334 | 0.033 | 1 | fail | failed to load snapshot info | 0 | 2 clean, 1 refused 404; `GET _snapshot/<repo>/_all` 404s for the whole repository | [`b4-summary.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b4-summary.json) |
| b5 | root `meta-<uuid>.dat`, snapshot taken without global state | 212 | 0.021 | **0** | **pass** | - | 0 | 3 clean | [`b5-summary.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b5-summary.json) |
| b6r | shard `index-<gen>`, current | 924 | 0.091 | **0** | **pass** | - | 0 | 3 clean | [`b6r-summary.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b6r-summary.json) |
| b7 | index metadata `indices/<uuid>/meta-<id>.dat` | **558** | **0.055** | 1 | fail | failed to load index metadata | 1 | **3 refused 404.** Every restore naming that index fails | [`b7-summary.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b7-summary.json) |
| b8r | `index.latest` | 8 | 0.001 | **0** | **pass** | - | 0 | 3 clean | [`b8r-summary.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b8r-summary.json) |
| b9r | current root `index-N` | 1,652 | 0.162 | **0** | **pass** | - | 0 | 2 clean, **1 snapshot gone from the catalog entirely** | [`b9r-summary.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b9r-summary.json) |
| b10 | shard `snap-<uuid>.dat` | 916 | 0.090 | 1 | fail | failed to load shard snapshot | 1 | 2 clean, 1 partial, that index red | [`b10-summary.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b10-summary.json) |
| b11 | shard `snap-<uuid>.dat` on a one-snapshot repository | 910 | 0.185 | 1 | fail | failed to load shard snapshot | 1 | the only snapshot restores red with no documents | [`b11-summary.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b11-summary.json) |
| b12 | root `meta-<uuid>.dat`, snapshot taken **with** global state | 39,957 | 6.634 | 1 | fail | failed to load global metadata | 0 | 1 clean (index data unaffected) | [`b12-summary.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b12-summary.json) |

The machine-readable version is
[`blast-radius-table.tsv`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/blast-radius-table.tsv). Each experiment
also left `-before.tsv`, `-after.tsv`, `-deleted.tsv`, `-verify-pre.json`,
`-verify.json`, `-catalog.json`, `-anomalies.json` and `-restores.json`.

b6r, b8r and b9r drop and re-add the repository after the delete. Elasticsearch
caches `RepositoryData` in memory, so without that step a root-level delete is
invisible to a node that has already read the catalog. Run without it, b9
reported the repository fully intact; run with it, a whole snapshot was gone.
That caching is itself worth knowing: **a root-level delete does not surface
until something makes the cluster read the catalog again.**

### Bytes do not predict damage, with numbers

| | Bytes | % of repository | Damage |
|---|---|---|---|
| Smallest delete with total effect | 558 | 0.055% | every restore naming that index fails, on all three snapshots |
| Largest delete with contained effect | 486,768 | 47.803% | one index, three snapshots |
| Quietest catastrophic delete | 1,652 | 0.162% | one snapshot vanishes, `_verify_integrity` says `pass` |
| Largest delete with no effect at all | 924 | 0.091% | nothing measurable |

**The smallest object whose loss causes the largest damage is the 558-byte index
metadata blob.** At 0.055 percent of the repository's bytes and 3.33 percent of
its objects it makes every restore that names that index fail outright, on every
snapshot, with a 404 rather than a red index. A restore naming only the other
index in the same snapshot still succeeds and returns 2,000 documents
([`b7b-selective-restore.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b7b-selective-restore.json)), so the
loss is total for that index and nil for its neighbour. That is a sharper case
than the document's 906-byte pointer, because it is smaller, it takes all three
snapshots rather than one, and it refuses the restore instead of producing a red
index an operator might read as a shard problem.

## The document's unsupported figures, one at a time

### 14 anomalies across three snapshots and all six index-snapshot pairs

**CONFIRMS, exactly.** [`b3-summary.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b3-summary.json),
[`b3-anomalies.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b3-anomalies.json),
[`b3-anomaly-table.txt`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b3-anomaly-table.txt).

Deleting all six `__` data blobs under `indices/` gave `total_anomalies: 14`,
`result: "fail"`, split `blast-snap-1` 4, `blast-snap-2` 4, `blast-snap-3` 6,
naming all six of the six index-snapshot pairs. Every anomaly reads
`"missing blob"`. This is the document's most important number and it reproduces
on an independently built repository of the same shape.

The three snapshots kept reporting `state: SUCCESS` throughout
([`b3-catalog.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b3-catalog.json)), and all three restored to red
indices with no documents.

### The 906-byte pointer, one snapshot, red index, zero documents, 0.196 percent

**CONFIRMS in substance; the two percentages are repository-specific.**
[`b11-summary.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b11-summary.json).

`base-p` came out at nine objects and 491,029 bytes against the document's nine
objects and 462,554 bytes. Its shard-level `snap-<uuid>.dat` is **910 bytes**,
which is **0.185 percent of the repository's bytes and 11.111 percent of its
objects**. The document reports 906 bytes, 0.196 percent and 11.1 percent. The
object-share matches to the digit. The byte-share differs because the two
repositories hold different amounts of data; the four-byte difference in the
object itself is index-name length.

Everything downstream matches: `total_anomalies: 1`, a different anomaly class
(`failed to load shard snapshot`), `blobs.verified` falling from 4 to **0**, the
restore returning HTTP 200 with `"shards":{"total":1,"failed":1,"successful":0}`
and a red index whose `docs.count` is blank, `_count` failing with
`search_phase_execution_exception`, and `GET _snapshot/<repo>/<snap>` still
saying `state: SUCCESS`, `failed: 0`, `failures: []`.

### The verbatim anomaly JSON block

**CONTRADICTS, narrowly but really.** [`b3-anomalies.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b3-anomalies.json).

The block in the document is introduced as "verbatim from the run above ... so
the identifiers are real rather than illustrative". A real 9.5.2 anomaly carries
two fields the quoted block does not have:

- `timestamp_in_millis` at the top level, present in 14 of 14 anomalies here.
- `index.metadata_blob`, present in 14 of 14 anomalies here.

Every other field in the quoted block, and its ordering, matches a real record.
The first anomaly of this campaign's run is the same class of row as the
document's, down to `physical_file_name: "_0.cfe"` and
`file_length_in_bytes: 742`, which is a codec constant rather than a
coincidence. So the block is a real record with two fields removed, not a
verbatim one. Print the whole record or stop calling it verbatim.

### The verify-integrity counter readings

**The headline reading CONFIRMS. The generalisation drawn from it does not.**

After every data blob in the repository was gone, `results.status` read
`snapshots verified 3/3`, `indices verified 2/2`, `index_snapshots verified
6/6`, `blobs verified 27`, **byte for byte identical** to the same repository's
clean run taken minutes earlier
([`b3-verify-pre.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b3-verify-pre.json) against
[`b3-verify.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b3-verify.json)). The document says exactly this,
including the figure 27, and it is right.

What does not hold is "`total_anomalies` and `result` are the only two fields in
that response that carry signal." `blobs.verified` moves for the pointer
classes:

| Experiment | Class | `blobs.verified` clean | damaged |
|---|---|---|---|
| b3 | missing blob | 27 | 27 |
| b10 | failed to load shard snapshot | 27 | 23 |
| b7 | failed to load index metadata | 27 | **15** |
| b11 | failed to load shard snapshot, one-snapshot repository | 4 | **0** |

The document itself reports the b11 case correctly two paragraphs later, so this
is an internal inconsistency rather than a wrong measurement. The honest form is
that the counters are blind to a missing data blob and not blind to a missing
pointer.

The other counter reading, "on the clean 21 object repository in the rig (the
local test lab reproducing the fault) it reported
`blobs verified: 15`", **could not be reproduced** because that repository no
longer exists. The claim underneath it, that `_verify_integrity` reads only what
the live catalog references, is confirmed by a stronger test below.

### The virtual-blob size table

**CONFIRMS.** [`b-base-s-virtual-blobs.tsv`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b-base-s-virtual-blobs.tsv).

Reading every file entry out of the shard documents and asking the bucket
whether an object exists for it, with lengths taken from the live index's own
files on the node:

| Blob name in metadata | Lucene file | Length | Object in the bucket |
|---|---|---|---|
| `v__DDjFUYgeRdiLYVSDObhgag` | `_0.si` | 351 | no |
| `v__cfvLKCsDReqEFvGksxOg2w` | `_0.si` | 351 | no |
| `v__2W-2rSgiSji5SC5k1A36Vw` | `_1.si` | - | no |
| `v__FVoW2jpgRtGhrhCG308elQ` | `segments_3` | - | no |
| `v__vNoVn4CMQ9m32RQ_lQUpAA` | `segments_3` | - | no |
| `v__5-AMsSzAQ7WXAZ9h6GAMYg` | `segments_4` | - | no |
| `__27_yjuX2QPyUrE-2VlGx9A` | `_0.cfe` | 742 | yes, 742 bytes |
| `__2cCGkm5uQf6Tw1qQ96W_ow` | `_0.cfe` | 742 | yes, 742 bytes |
| `__6HfSS9VGSUy_86kMSv-_Kw` | `_0.cfs` | 486,768 | yes, 486,768 bytes |
| `__ocOz3nmvQoCISRh0va80RA` | `_0.cfs` | 486,768 | yes, 486,768 bytes |

Same classes as the document's table, and the same 351-byte `_0.si` with no
object beside a 742-byte `_0.cfe` that has one. The size-threshold explanation
fails on the same pair of numbers it failed on before. Lengths are shown only
for the segment still present on the live node; the older segment's files were
merged away, which is why those cells are blank.

## The three claims

### Does damage propagate forward? Yes. Measured, not derived.

[`b13-forward.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b13-forward.json).

The document derives this from source and says so. It is now measured.

One shared data blob was removed from a clone of `base-s`, 486,768 bytes
belonging to `blast-share2`, whose live index had not changed since the snapshot
that uploaded it. A fresh snapshot was then taken of the same two live indices
into the damaged repository.

- The new snapshot returned `state: "SUCCESS"`, `shards {total 2, failed 0,
  successful 2}`, `failures: []`.
- It wrote 7 new objects and **no data blobs**, so it deduplicated against the
  blob that no longer exists.
- `_status` reported `blast-share2` at `file_count 4, size_in_bytes 488240`,
  which counts 486,768 bytes that are not in the bucket.
- `_verify_integrity` went from 3 anomalies to 4: the new snapshot inherited the
  damage.
- Restoring the new snapshot: `shards {total 2, failed 1, successful 1}`,
  `blast-share2` red with no documents, `blast-share1` green with 2,050.

A snapshot taken after the delete reports SUCCESS and cannot be restored.
Confirmed.

### Is `_verify_integrity` a clean bill of health? No, on both halves.

**Half one, the counters.** Confirmed above: after total data loss the
`results.status` block is byte for byte identical to the clean run.

**Half two, it walks only what the catalog still lists.** Confirmed twice, and
the second one is worse than the document's version.

The document's version: a snapshot deleted from the catalog stops being
verified. Measured in [`b16-mount-catalog.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b16-mount-catalog.json).
Deleting the only snapshot returned `acknowledged: true`, MinIO's rejected batch
delete left every blob in place so the bucket went from 9 objects to 10, and
`_verify_integrity` then returned:

```
snapshots 0/0, indices 0/0, index_snapshots 0/0, blobs verified 0,
total_anomalies 0, result "pass"
```

A repository holding nothing but garbage and a live mounted index depending on
it passes with nothing verified.

The version the document does not have: **deleting the current root `index-N`
takes a whole snapshot out of the repository and `_verify_integrity` reports
`pass`.** [`b9r-summary.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b9r-summary.json). 1,652 bytes, 0.162
percent of the repository. The catalog fell back to generation 1, `snapshots
verified 2/2`, `index_snapshots verified 4/4`, `total_anomalies: 0`, `result:
"pass"`, and `blast-snap-3` returns `snapshot_restore_exception: snapshot does
not exist`. Nothing in the response says a snapshot is missing, because from
generation 1's point of view it never existed. This is the quietest destructive
delete measured in the whole campaign.

### Does a mounted searchable snapshot hide the damage? Yes, and the document's account of when it stops hiding it is wrong for one of the two mount types.

Four experiments, one per state an operator can be in.

| Experiment | Mount | What was removed | After the delete | After a shared-cache clear | After close and reopen | After forced reallocation | After the node loses its local copy |
|---|---|---|---|---|---|---|---|
| [b14](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b14-mount.json) | `full_copy` | data blobs | green, 600 docs, search 600 | green, 600, search 600 | green, 600, search 600 | green, 600, search 600 | not tested |
| [b15](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b15-mount.json) | `shared_cache` | data blobs | green, 600 docs, search 600 | **green, `_cat` 600, `_count` 600, search fails** | green, 600, search fails | green, 600, search fails | n/a |
| [b17](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b17-mount-catalog.json) | `shared_cache` | snapshot from the catalog, then the blobs | green, 600, search 600 | **green, 600, search fails** | green, 600, search fails | green, 600, search fails | n/a |
| [b18](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b18-mount-catalog.json) | `full_copy` | snapshot from the catalog, then the blobs | green, 600, search 600 | green, 600, search 600 | green, 600, search 600 | green, 600, search 600 | see b19 |
| [b19](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b19-mount-nodeloss.json) | `full_copy` | data blobs, then the node's copy of the shard | green, 600, search 600 | n/a | n/a | n/a | **red, ALLOCATION_FAILED, 5 failed attempts, `RecoveryFailedException`** |

Reading those rows against the document:

**"The index stays green": confirmed, and stronger than stated.** A partially
mounted index whose backing blobs are gone stays green through a cache clear, a
close and reopen, and a forced reallocation. `_cat/indices` keeps reporting
`docs.count 600` and `_count` keeps answering 600 the whole time, because both
read metadata. It never goes red at all. The operator-visible signal is a search
that fails with `search_phase_execution_exception` on an index the cluster calls
green and claims holds 600 documents.

**"Survives a cache clear": CONTRADICTS for a partial mount.** On 9.5.2 the
shared-cache clear is exactly what surfaces the damage. Before the clear, search
returns 600 hits from cache. After it, search fails. The index stays green
either way.

**"It goes red on a forced shard reallocation, with `ALLOCATION_FAILED` and a
`RecoveryFailedException`": confirmed only for a fully mounted index, and only
when the node actually loses its local copy.** A `full_copy` mount holds a
complete copy on the node, so removing every blob from the bucket changes
nothing at all: green, 600 documents, searches answering, through cache clears,
closes, reopens and allocation filtering. It went red only when this campaign
deleted that index's own data directory on the node and reopened, which is what
a node replacement does. Then the shard came back `ALLOCATION_FAILED` with
`failed_allocation_attempts: 5` and
`org.elasticsearch.indices.recovery.RecoveryFailedException`, exactly the words
the document uses.

**"The window in which this damage is visible is zero": confirmed.**
`_verify_integrity` on the swept repository in b17 returned `total_anomalies: 0,
result: "pass"` with `snapshots 0/0`, because the snapshot the mounted index
depends on is no longer in the catalog. Nothing printed after the delete helps.

The correction the document needs is that it merges two mount types into one
story. A partial mount fails at the next uncached read and never goes red. A
full mount does not fail at all until the node loses its copy, and then it goes
red the way the document describes. Both are worse than a red index in their own
way: the partial mount lies about its document count indefinitely.

## Where the damage is smaller than the document implies

Three of these, stated as plainly as the ones where it is larger.

**Deleting a shard's current `index-<gen>` costs nothing measurable.**
[`b6r-summary.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b6r-summary.json),
[`b20r-next-snapshot.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b20r-next-snapshot.json). The document says
the restore still works "but the shard has lost the file list every future
snapshot deduplicates against". The first half is right: all three restores came
back green with full document counts. The second half did not happen. With the
repository re-read from the store after the delete, the next snapshot reported
SUCCESS, wrote 7 objects and **zero data blobs**, `_verify_integrity` returned
`total_anomalies: 0, result: "pass"`, and the new snapshot restored green with
2,000 and 2,050 documents. Elasticsearch recovered the shard's file list from
the per-snapshot `snap-<uuid>.dat` blobs. Compare the control, `index.latest`
deleted, which also wrote 7 objects and zero data blobs
([`b21-next-snapshot.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b21-next-snapshot.json)): the two are
indistinguishable.

**Deleting `index.latest` costs nothing.** [`b8r-summary.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b8r-summary.json).
8 bytes, `total_anomalies: 0`, `result: "pass"`, all three restores green with
full document counts, and the next snapshot behaves normally. This confirms the
document's own reading of `latestIndexBlobId`: listing is primary and
`index.latest` is the fallback. Worth stating positively, because a delete
manifest containing `index.latest` reads alarming and is not.

**Deleting a root `meta-<uuid>.dat` costs nothing when the snapshot was taken
without global state.** [`b5-summary.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b5-summary.json). 212
bytes, `total_anomalies: 0`, `result: "pass"`, three clean restores. It matters
when the snapshot did carry global state
([`b12-summary.json`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b12-summary.json)): 39,957 bytes, one anomaly,
`failed to load global metadata`, while index data restores unaffected. The
document's campaign-1 orphan table lists a root `meta-<uuid>` at 38,805 bytes as
10.3 percent of the orphan bytes without saying which of those two situations it
is in. Which one it is decides whether losing it matters.

## Limits of this campaign

- One node, so a genuine relocation to a different node could not be forced. b19
  removes the index's own data directory instead, which is what node replacement
  does to that shard but is not literally a reallocation.
- The `full_copy` mount case in b18 was not driven to failure; b19 is the
  experiment that does that, on a repository whose snapshot is still in the
  catalog. The combination of catalog removal, blob sweep and node loss on a
  full mount was not run.
- b12 measures a missing global metadata blob through `_verify_integrity` and
  through an index-only restore. A restore with `include_global_state: true` was
  not run, because applying cluster state on a rig other agents are working on
  is not a safe experiment.
- Byte percentages here are percentages of these repositories. They are the right
  shape and the wrong absolute value for any other repository, which is the point
  the document is making with them.
- Elasticsearch caches `RepositoryData`. Any measurement of a root-level delete
  that does not force a re-read measures the cache, not the repository. b6, b8
  and b9 are the cached readings and b6r, b8r and b9r are the real ones; both
  sets are kept in `artifacts/` so the difference is visible.

## Provenance

Run on 2026-08-25 against Elasticsearch 9.5.2 (`build_hash
b42549c72e6e040825b13e5d8ebf7ff63886b24d`, Lucene 10.5.1) in namespace `es-rig`
on the `rancher-desktop` context, one node with a 2 GB heap, a licence tier permitting searchable snapshots,
repositories of type `s3` against MinIO `RELEASE.2025-01-18T00-31-37Z` in the
same namespace.

Base repository listings are recorded in
[`b-base-s-listing.tsv`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b-base-s-listing.tsv),
[`b-base-p-listing.tsv`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b-base-p-listing.tsv),
[`b-base-g-listing.tsv`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b-base-g-listing.tsv) and
[`b-base-ms-listing.tsv`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/blast-radius-remeasure/artifacts/b-base-ms-listing.tsv), so the exact bytes
every experiment started from are on file even though the bucket is gone.

`harness/cleanup.sh` ran at the end. The `blastrm` bucket was deleted, all
`blast-*` repositories were unregistered, and all `blast-*`, `bxr*` and `bxms*`
indices were removed. Nothing outside those prefixes was touched. The cluster's
ten unassigned shards belong to `qabb24-restored-victim` and
`qabb24-mtest-mounted`, which were already red before this campaign started.
