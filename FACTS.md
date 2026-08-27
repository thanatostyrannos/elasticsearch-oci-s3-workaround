# Established facts

Everything here was measured or read at source. Nothing inferred.

Two kinds of fact live here and they age differently. The fault below is
settled history: released version numbers, merged pull requests, a published
list of accepted checksum algorithms. It does not drift, so it is written out
in full rather than left as a pointer. Everything after it describes a tool
that is still moving, and each of those sections carries the date it was
measured.

## The fault this repository exists for

Elasticsearch **8.19.17 and later, and 9.5.0 and later**, cannot delete blobs
from an S3-compatible store that requires `Content-MD5` on the batch delete.
Releases 8.19.0 through 8.19.16, 9.1 through 9.4, and everything before the AWS
SDK v2 migration, including 9.0.x and 8.18.x, are unaffected. That boundary is
why an upgrade is usually the moment the problem appears.

**The mechanism.** Since AWS SDK for Java v2.30.0 the SDK sends flexible
checksums, `x-amz-checksum-crc32`, in place of `Content-MD5`, including on
checksum-required operations such as S3 Multi-Object Delete (`DeleteObjects`).
The SDK defaults to CRC32.

Elasticsearch met this in two steps. The AWS SDK v2 migration
([#126843](https://github.com/elastic/elasticsearch/pull/126843), in 8.19.0 and
9.1.0) carried a request-signer override that held the SDK on its pre-SRA code
path, where checksum-required operations still receive `Content-MD5`.
[#150194](https://github.com/elastic/elasticsearch/pull/150194) removed that
override, backported to `8.19` the same day as
[#150237](https://github.com/elastic/elasticsearch/pull/150237). Removing it was
reasonable on its own terms, since it restored the SDK's intended signing for
`PutObject` and `UploadPart`. It also moved `DeleteObjects` onto the SRA path:
`x-amz-checksum-crc32`, no `Content-MD5`.

**Why OCI rejects it.** Oracle's Amazon S3 Compatibility API accepts exactly two
alternatives to `Content-MD5` on this operation: `x-amz-checksum-sha256` and
`x-amz-checksum-crc32c`. CRC32 and CRC32C are different algorithms. The SDK
default is therefore the one algorithm the endpoint will not take, and the whole
batch returns HTTP 400.

Measured against a real Oracle bucket on 2026-08-26, in `us-ashburn-1`, driving
the shipping code rather than hand-built requests. Oracle names the accepted set
itself:

    Missing required header for this request:
    Content-MD5 or x-amz-checksum-sha256 or x-amz-checksum-crc32c

    crc32    0 deleted, 2 failed, 400 InvalidRequest
    crc32c   2 deleted, 0 failed
    sha256   2 deleted, 0 failed
    md5      2 deleted, 0 failed

Three of four work. The batch delete is not the fault; the checksum the client
chooses is. Reproduce this against your own bucket with
`snapshot_churn_rig.py` and the procedure in
[Testing in your own OCI environment](docs/testing-in-your-oci-environment.md):
register a repository, delete a batch with each checksum header in turn, and
compare the responses.

**`ListObjectsV2` is genuinely supported and pages**, measured the same day with
three objects and `--max-keys 2`. `KeyCount` and `NextContinuationToken` both
present. Oracle's published operations list names only `ListObjects`, so this
was worth settling.

`WHEN_REQUIRED` does not help. That setting narrows which *operations* receive a
checksum, never which *algorithm*, so the collision survives it.

**Single-object `DeleteObject` is unaffected**, because it carries no body and
so no checksum. That asymmetry is the whole reason any workaround is possible.

**The blast radius is the endpoint, not the deployment.** The same failure is
reported against NetApp StorageGRID, Hitachi Content Platform, Ceph RADOS
Gateway, and MinIO before its January 2025 fix. Measured directly:
`RELEASE.2025-01-18T00-31-37Z` rejects, `RELEASE.2025-01-20T14-49-07Z` accepts.
AWS S3 itself treats `Content-MD5` as optional and is unaffected. Any
self-managed, ECK or ECE cluster pointing a repository at an affected store hits
this.

**No upstream fix exists.** Elastic declined to expose the checksum algorithm as
a repository setting and declined to document the change as breaking; its
published position is that the storage vendor should fix it. Oracle's
documentation points at `LegacyMd5Plugin`, a client-side remedy, rather than
announcing a server-side change. Neither side has shipped a fix, so the leak
should be planned for as permanent.

Registering with `?verify=false` is the workaround Elastic support gives. It
restores registration and snapshots. **It does not make deletes work**, so the
leak continues underneath a cluster that now looks healthy.

## The test lab, henceforth the rig

Most numbers below were measured against a local reproduction of the fault
rather than a production cluster. It is referred to throughout as **the rig**.

    Elasticsearch    9.5.2 under ECK, in Rancher Desktop
    namespace        es-rig
    object store     MinIO, pinned to RELEASE.2025-01-18T00-31-37Z, and
                     Oracle Object Storage over the Amazon S3 Compatibility
                     API for the 2026-08-27 campaign below

The MinIO pin is load-bearing. That release is the last one that rejects the
batch delete, so it reproduces the fault; the release two days later accepts it
and the rig stops being a reproduction. Do not upgrade it.

Three things run continuously while measurements are taken: a load generator
writing documents, an ILM policy rolling them through to object storage, and an
SLM policy snapshotting on a short cycle. Nothing is paused for a test run.

That last part is deliberate and it is the point of the design. Every guard
concerned with a repository changing underneath the tool had previously been
tested against a repository standing still. A fast lifecycle also manufactures
orphans continuously, so orphan classification can be re-evaluated on demand
instead of against a fixed pile someone built once.

**Its cadence is pathological on purpose, and that is what limits what its
numbers mean.** The snapshot cycle has been run as fast as fifteen seconds,
roughly 240 times a production hourly schedule. Measurements below name the
cadence in force when they were taken. Anything expressed as a rate or a
wall-clock time is therefore a property of the rig, not a prediction. Counts
and orderings transfer; rates do not.

## Unbounded growth, and why nothing stops it

The delete does not fail loudly. `DELETE _snapshot/<repo>/<snapshot>` returns
`acknowledged: true`, the snapshot leaves the catalog, and the blobs stay. The
only trace is a WARN line, `Failed to delete some blobs during snapshot delete`,
capped at ten keys however many actually failed.

So the catalog shrinks while the bucket grows, and the two are never reconciled.

**Nothing reclaims the residue.** `POST _snapshot/<repo>/_cleanup` returns 200
with `"deleted_bytes": 0` and `"deleted_blobs": 0` against a repository where
almost nothing is still referenced. Measured over 30 samples during live churn:
the object count never decreased once, across 109 deletions all reported
successful. There is no background process, no delayed reaper, and no retry that
picks these up later. A blob orphaned this way stays until something outside
Elasticsearch removes it.

**Two things accumulate, not one.** The data blobs are the obvious channel. The
second is the root generations: Elasticsearch removes superseded `index-N`
objects as part of a snapshot delete, so on a store with this fault they survive
like everything else. One leaks per snapshot operation, forever.

That second channel is the one that bites the fix. The audit deliberately never
condemns a root generation, because its own derivation reads them, and its cost
is one shard-document read per shard directory **per generation**. So the
generation count multiplies the whole traversal, and the tool gets slower the
longer the fault goes unfixed. Measured on the rig (2026-08-26, issue #9,
snapshot cycle at fifteen seconds):

    index-N objects on disk    1,205
    current root generation      883
    derive wall clock         over 10 minutes, still running

**What that does and does not predict.** At that cadence the wall-clock figure
is a property of the rig. The accumulation is not. A cluster on
hourly snapshots reaches 883 generations in about five weeks, and nothing
ever brings the number down.

Resist extrapolating a bucket growth rate from rig numbers. One such figure, a
per-year terabyte total, was produced in this project and withdrawn: it
described the rig's synthetic churn rather than any deployment. Growth depends
on document volume, retention and snapshot cadence, and no measurement here
establishes it for a real cluster.

**The batch delete itself works, with the right header.** Proven against the
live bucket on two throwaway objects: without `Content-MD5`, HTTP 400
`MissingContentMD5`; with it, HTTP 200, and both keys return 404 afterwards.
[`generation_chain/reclaim/checksum.py`](generation_chain/reclaim/checksum.py) sends `Content-MD5` and no
`x-amz-checksum-*` header at all. This is why reclaiming 76,656 keys costs 77
requests rather than 76,656.

## Format facts, confirmed from Elasticsearch source


[`.../blobstore/package-info.java`][pkg] documents the
layout:

    STORE_ROOT
    |- index-N            RepositoryData, JSON
    |- index.latest       numeric, latest generation N
    |- snap-<uuid>.dat    SMILE SnapshotInfo
    |- meta-<uuid>.dat    SMILE Metadata
    |- indices/<index-uuid>/
       |- meta-<id>.dat
       |- 0/              shard directory
          |- __<segment>  data blobs
          |- snap-<uuid>.dat
          |- index-<gen>  BlobStoreIndexShardSnapshots

**SMILE** is Jackson's binary JSON encoding ([format specification][smile]).
Elasticsearch selects it through [`XContentType.SMILE`][xcontent] and writes
these blobs with Jackson, so reading one means implementing that specification
rather than pointing a JSON parser at it.

**Generation lookup, Elasticsearch's own order.** "First, find the most recent
RepositoryData by getting a list of all index-N blobs through listing all blobs
with prefix 'index-' under the repository root and then selecting the one with
the highest value for N." Only "if listing fails: read the highest value of N
from the index.latest blob."[[1]][pkg] LISTING IS PRIMARY. `index.latest` is
the fallback.

**Deletion algorithm, Elasticsearch's own words.** "Collect all segment blobs
(identified by having the data blob prefix `__`) in the shard directory which are
not referenced by the new BlobStoreIndexShardSnapshots",[[1]][pkg] then delete
them. The correct answer is shard-local set difference. Nothing outside the
shard directory participates.

[`RepositoryData.java`][repodata] confirms the fields written to `index-N`: `min_version`,
`uuid`, `cluster_id`, `snapshots`, `indices`, `index_metadata_identifiers`.
Per snapshot: `name`, `uuid`, `state`, `index_metadata_lookup`, `version`,
`index_version`, timestamps, `slm_policy`. Per index: `id`, `snapshots`,
`shard_generations`, the last "indexed by shard position". It states there is
NO field referencing a previous generation. `min_version` exists "to make it
impossible for older ES versions to deserialize this object",[[2]][repodata] so it is
a deserialization floor.

There is no published on-disk format specification. The source is the authority.

## Facts measured from the real captured repository

From a real Elasticsearch 9.5.2 repository, captured whole and kept as a
fixture for this project's own test suite.

`min_version` reads `7.12.0` on every generation, not the writing version.

A shard document's top level is exactly `files` and `snapshots`. Nothing names
its own shard, index or generation.

File entries carry: `checksum`, `length`, `meta_hash`, `name`, `part_size`,
`physical_name`, `writer_uuid`, `written_by`.

**`writer_uuid` discriminates shards.** Measured across three indices:
overlap 0 in all three pairwise comparisons, and stable within a shard across
generations (same 9 distinct values in all three of one shard's documents).
NOT yet tested across two shards of the SAME index.

**A shard document holds every snapshot's file list for that shard**, e.g.
`snapshots_in_doc=['v9-snap-1','v9-snap-2']`. The union is stored, not assembled.

**`snap-<uuid>.dat` declares the snapshot's own extent**: `indices` list,
`total_shards`, `successful_shards`, and per-index `index_details` giving
`shard_count`, `size_in_bytes`, `max_segments_per_shard`.

## Facts measured about the Elasticsearch API

`GET /` gives the cluster version. `GET /_snapshot/<repo>` gives type, uuid and
settings, no format information. `GET /_snapshot/<repo>/<snap>/_status` gives
`file_count` and `size_in_bytes`. Every API describing a snapshot reports
cardinality, never identity. The only place Elasticsearch emits blob names is
the failed-delete WARN line, capped at ten keys.

## The safety condition, stated correctly

Let G* be the true reference graph and G the believed one. Condemning blob b is
sound only if deg(b)=0 in G* after the delete. We compute deg(b)=0 in G. That
inference is valid IF AND ONLY IF G contains every edge G* has among surviving
snapshots.

**Extra edges only leak. Missing edges destroy.** Therefore every uncertainty
must resolve toward MORE edges, never fewer. Dropping a shard whose document
will not read is correct because it means condemning nothing there, which is
maximal edge addition.

Every live-data counterexample found in this session was a missing edge.

Two edge types with different completeness conditions: snapshot to segment via
the shard document, and snapshot to index-metadata via `index_metadata_lookup`.
The metadata path produced three counterexamples on its own.

## What the premise does and does not buy

**For segment blobs the rule is Elasticsearch's own, and ours is a subset of
it.** Checked against their source rather than quoted from memory.

Elasticsearch, in [`package-info.java`][pkg]:

> Collect all segment blobs (identified by having the data blob prefix `__`)
> in the shard directory which are not referenced by the new
> `BlobStoreIndexShardSnapshots` that has been written in the previous step as
> well as the previous `index-${uuid}` blob so that it can be deleted at the
> end of the snapshot delete process.

Three lines, side by side:

| | Elasticsearch | This tool |
|---|---|---|
| The set | `__` blobs in the shard directory that the current file list does not name | [`derivation/shards.py`](generation_chain/derivation/shards.py), `frozenset(present_blobs - live_blobs)`, the same difference, shard-local |
| Then | deletes all of them | [`derivation/garbage.py`](generation_chain/derivation/garbage.py), `named & history.collectable`, keeps only those a delete it observed actually named |
| Superseded generation documents | deletes them too | never names them, see [`derivation/classification.py`](generation_chain/derivation/classification.py) |

So the candidate set is computed identically, then intersected with positive
evidence. **An intersection cannot add a member.** Naming a blob Elasticsearch
would keep is therefore not a risk that has to be managed for segments, it is
arithmetic that cannot happen, and the tool is more conservative again by
leaving the generation documents Elasticsearch removes.

Whatever goes wrong here leaks. It does not delete.

**The metadata path is weaker, and that is where every counterexample came
from.** Segment edges are complete inside one shard directory, so the
difference is shard-local and nothing outside can affect it. Index-metadata
edges are assembled from two repository-wide maps, so a read failure elsewhere
does bear on the answer. Treat the two differently; the code already does.

**The one place a live blob could still be removed has nothing to do with any
of the above.** The Elasticsearch veto, which protects blobs backing mounted
searchable snapshots, is applied when the manifest is derived and never again.
Nothing bounds how old a manifest may be when it is executed. Measured:
`generation_chain/reclaim/` contains no reference to Elasticsearch at all, and
nothing in `approval.py` or `manifest.py` reads the file's age. Mount a
searchable snapshot between deriving and executing and the delete proceeds
blind to it. That is a time-of-check gap, not an absence test, and it is the
gap worth closing.

Chain completeness comes free from the monotonic numbering plus `index.latest`.
Traversal completeness within a generation does not, and that is what
`snap-<uuid>.dat`'s declared extent can establish.

## Campaign results, 2026-08-26

Twelve delete cycles run back to back against a repository that was moving the
whole time: a load generator writing, ILM rolling, SLM snapshotting. Nothing was
paused, because orphans created mid-run are simply caught by the next run.

    cycle  manifest    deleted   failed  unconfirmed   objects before -> after
    1      92865       -         -       -             354554 -> 263005
    2      4665        4664      0       0             273448 -> 268784
    3      2337        2336      0       0             276436 -> 274152
    4      2475        2474      0       0             281207 -> 278809
    5      1967        1966      0       0             285661 -> 284258
    6      1963        1962      0       0             293999 -> 292037
    7      2957        2956      0       0             308365 -> 305409
    8      4553        4552      0       0             325027 -> 320475
    9      5565        5564      0       0             341947 -> 337353
    10     7131        7130      0       0             368365 -> 361586
    11     8795        8794      0       0             399668 -> 392311
    12     11539       11538     0       0             459624 -> 466715

The manifest column is a line count of the manifest file, which carries a header
row, so `deleted` is one less than `manifest` throughout. Nothing is being
skipped.

**No cycle failed a delete.** `failed` and `unconfirmed` are zero in every cycle
that preserved its output. Cycle 1's own counts were overwritten by a later run
and are shown as `-`; its object count fell by 91,549 against a manifest of
92,865, which is the expected shape when the generator is writing concurrently.

**Across all twelve cycles the tool removed 146,800 objects** while the rig kept
writing underneath it.

**The store confirms the deletes.** Each cycle re-reads every key it deleted;
`unconfirmed` counts keys the store would not confirm gone. It stayed at zero
across all twelve.

**Cycle 12 is the one cycle the generator won, and the numbers say why.**
Object count rose from 459,624 to 466,715. Eleven of the twelve cycles ran net
negative; this one did not.

Two things combined, neither of them the delete path being slow.

The rig was manufacturing garbage at a rate no production cluster approaches.
SLM was snapshotting every fifteen seconds, about 240 times an hourly schedule,
with the load generator writing and ILM rolling the whole time. Across cycle 12
the rig created 18,629 objects while the tool deleted 11,538. That is roughly
964 new objects a minute, against a previous worst of 214 and a median near
zero.

And the cycles were getting longer. The window per cycle grew steadily from 2.5
minutes at cycle 4 to 19.3 minutes at cycle 12. A longer cycle is simply more
time for the generator to run, so the two effects multiply. The growth is
consistent with the root generations piling up: the audit reads one shard
document per shard directory per generation, and nothing removes a generation,
so each pass costs more than the last. That is issue #9, and cycle 12 is what
it looks like from the outside.

So the honest reading is not that reclaiming loses a race. It is that a
repository being churned at 240 times production speed, audited by a tool that
gets slower as the unfixed fault accumulates generations, can out-produce a
single pass. The design already assumes repeated runs rather than one sweep to
zero, which is why the other eleven cycles look the way they do.

**Nothing was broken.** Restores were taken at cycles 3, 6 and 9, each one
restoring a real data-stream index from a snapshot and counting documents:

    cycle 3    24/24 shards    246,000 documents    integrity anomalies 0    INTACT
    cycle 6    24/24 shards    122,000 documents    integrity anomalies 0    INTACT
    cycle 9    24/24 shards    184,082 documents    integrity anomalies 0    INTACT

The check at cycle 12 did not run. It aborted on a bug in the checker, which
treated `IN_PROGRESS` snapshots as an unexpected state, and on a live rig with
SLM running there are always some. That is a harness defect, not a finding about
the repository: no restore was attempted, so cycle 12 has no restore result
either way. Recorded here rather than quietly dropped, because a check that did
not run is not a check that passed.

A thirteenth run afterwards reclaimed 61,986 keys from a manifest of 61,987,
again with zero failed and zero unconfirmed.

**What Elasticsearch does on its own, for contrast.** Sampled 30 times during
the same churn, the object count never fell once. 109 snapshot deletions
reported successful and reclaimed nothing, and
`POST _snapshot/<repo>/_cleanup` returned `"deleted_bytes": 0` and
`"deleted_blobs": 0` against a repository where almost nothing was still
referenced.

## Campaign results, 2026-08-27, against a real Oracle bucket

The campaign above ran against MinIO. This one ran against Oracle Object
Storage over the Amazon S3 Compatibility API, which is the store the fault
belongs to. The repository was manufactured by `snapshot_churn_rig.py`: 60
documents a second, ILM rolling, SLM snapshotting every 60 seconds with a five
minute retention, so snapshots expired continuously and every expiry stranded
blobs the store refused to delete.

The fault reproduced before any measurement started. Registering the repository
failed its own verification, because verification tries a batch delete and the
store rejects it. That refusal is the first evidence the store leaks, and the
rig continues past it with `verify=false` deliberately.

**Run one: 80 cycles, 2,896 objects deleted.** No failed delete, no unconfirmed
delete, no non-zero exit. Every cycle read both of the shard directories it
depended on, and every segment-mode cycle settled rather than timing out.

That run also measured the thing that produced run two. Cycle time tracked
generation count almost linearly, from 2.0 minutes at cycle 2 to 7.1 minutes by
cycle 78. The audit reads one shard document per shard directory per
generation, nothing ever removes a generation, and those reads were serial: a
read-ahead layer with a bounded thread pool already existed and had exactly one
caller, the root generations. The shard documents, which are the bulk of the
work, were fetched one at a time with eight workers idle.

**Run two: the same rig with the shard reads warmed.** Against a repository
rebuilt from zero, so the two are comparable.

    cycles                    58   (29 segment, 29 metadata)
    deleted                  888
    failed                     0
    unconfirmed                0
    non-zero exits             0
    shard directories read   2 of 2, on all 58
    segment cycles settled  29 of 29

    metadata-mode cycles      26s average, 12s at the fastest
    segment-mode cycles      129s average, 86s at the fastest

**What the campaign reclaimed, and how fast it leaked.** Across 74 minutes the
tool removed 270.2 MB the store had refused to delete, which is 181.9 MB an
hour at that cadence. About 3 MB per snapshot expiry, one expiry a minute.

Those are properties of the rig, not predictions. The cadence is pathological
on purpose. Counts and orderings transfer; rates do not.

**What this campaign does not establish.** One tenancy, one bucket, one
repository shape. It says the paths work against a real Oracle endpoint, not
that they work against every one. Claims about other stores that reject the
same call rest on their published operation lists rather than on a run.


## Sources

Numbered citations above point here. Both are Elasticsearch's own source, which
is the authority: there is no published on-disk format specification.

1. [`server/src/main/java/org/elasticsearch/repositories/blobstore/package-info.java`](https://github.com/elastic/elasticsearch/blob/main/server/src/main/java/org/elasticsearch/repositories/blobstore/package-info.java)
   documents the repository layout, the generation-lookup order, and the
   deletion algorithm.
2. [`server/src/main/java/org/elasticsearch/repositories/RepositoryData.java`](https://github.com/elastic/elasticsearch/blob/main/server/src/main/java/org/elasticsearch/repositories/RepositoryData.java)
   defines the fields written to `index-N`, including `min_version`.

Referenced elsewhere in this file:

- [SMILE format specification](https://github.com/FasterXML/smile-format-specification/blob/master/smile-specification.md),
  the binary JSON encoding Elasticsearch writes `snap-`, `meta-` and shard
  `index-<gen>` blobs in, selected through
  [`XContentType.SMILE`][xcontent].
- Elasticsearch pull requests [#126843](https://github.com/elastic/elasticsearch/pull/126843),
  [#150194](https://github.com/elastic/elasticsearch/pull/150194) and
  [#150237](https://github.com/elastic/elasticsearch/pull/150237), which
  together moved `DeleteObjects` onto the SRA checksum path.

[pkg]: https://github.com/elastic/elasticsearch/blob/main/server/src/main/java/org/elasticsearch/repositories/blobstore/package-info.java
[repodata]: https://github.com/elastic/elasticsearch/blob/main/server/src/main/java/org/elasticsearch/repositories/RepositoryData.java
[smile]: https://github.com/FasterXML/smile-format-specification/blob/master/smile-specification.md
[xcontent]: https://github.com/elastic/elasticsearch/blob/main/libs/x-content/src/main/java/org/elasticsearch/xcontent/XContentType.java
