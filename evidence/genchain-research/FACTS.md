# Established facts, generation-chain audit tool

Everything here was measured or read at source. Nothing inferred.
Written after the session's agents were stopped for context pollution.

## Format facts, confirmed from Elasticsearch source

`server/.../repositories/blobstore/package-info.java` documents the layout:

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

**SMILE** is Jackson's binary JSON encoding ([format specification](https://github.com/FasterXML/smile-format-specification/blob/master/smile-specification.md)). Elasticsearch selects it through [`XContentType.SMILE`](https://github.com/elastic/elasticsearch/blob/main/libs/x-content/src/main/java/org/elasticsearch/xcontent/XContentType.java) and writes these blobs with Jackson, so reading one
means implementing that specification rather than pointing a JSON parser at it.

**Generation lookup, Elasticsearch's own order.** "First, find the most recent
RepositoryData by getting a list of all index-N blobs through listing all blobs
with prefix 'index-' under the repository root and then selecting the one with
the highest value for N." Only "if listing fails: read the highest value of N
from the index.latest blob." LISTING IS PRIMARY. `index.latest` is the fallback.

**Deletion algorithm, Elasticsearch's own words.** "Collect all segment blobs
(identified by having the data blob prefix `__`) in the shard directory which are
not referenced by the new BlobStoreIndexShardSnapshots", then delete them. The
correct answer is shard-local set difference. Nothing outside the shard
directory participates.

`RepositoryData.java` confirms the fields written to `index-N`: `min_version`,
`uuid`, `cluster_id`, `snapshots`, `indices`, `index_metadata_identifiers`.
Per snapshot: `name`, `uuid`, `state`, `index_metadata_lookup`, `version`,
`index_version`, timestamps, `slm_policy`. Per index: `id`, `snapshots`,
`shard_generations`, the last "indexed by shard position". It states there is
NO field referencing a previous generation. `min_version` exists "to make it
impossible for older ES versions to deserialize this object", so it is a
deserialization floor.

There is no published on-disk format specification. The source is the authority.

## Facts measured from the real captured repository

`tests/fixtures/real-es952-repo.tar.gz`, Elasticsearch 9.5.2.

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

"Condemn on presence" is half true. The first half, this delete operation
orphaned this blob, is positive evidence. The second half, no surviving snapshot
references it, is irreducibly an absence test and is where every failure landed.

The generation chain does not make the live set complete. It bounds the
candidate set, so a live-set error can only wrongly condemn a blob the dead
snapshot also named, which means a shared segment. That is a real reduction in
blast radius, not a guarantee.

Chain completeness comes free from the monotonic numbering plus `index.latest`.
Traversal completeness within a generation does not, and that is what
`snap-<uuid>.dat`'s declared extent can establish.

## Measured scale behaviour

Harness at `$WORK/scale-limits`, `run_all.sh`
reproduces. Tool snapshot frozen at `b689f66`.

Cost is linear and fully SERIAL. Concurrency never exceeded 1. 4.50 round trips
per generation, flat 10 to 10,000. 1.00 shard-document GET per shard directory
per generation. 894 generations at 40 ms round trip: 163.5 s, which is 4.3x the
retired containment code. 53,063 objects over 1,000 shard dirs: 48,093 requests,
193.3 s on local MinIO, 32 minutes at 40 ms. 58 percent of traffic is HEAD.

Listing is correct at depth: 133 real MinIO pages and 132 OCI stub pages, key
sets identical, no loss or duplication.

Memory 1.9 KB resident per object, linear to 585,194 objects at 1.55 GB peak.
A 2 GB host dies near 750,000 objects.

> [!IMPORTANT]
> **Three of the figures above describe a build that no longer exists.** They
> were measured against the tool snapshot frozen at `b689f66` and are correct
> for it. They are left in place because they are what was measured; what
> changed is below.
>
> **"Fully SERIAL, concurrency never exceeded 1" no longer holds.** The version
> on main carries a read-ahead layer with `DEFAULT_CONCURRENCY = 8` and a
> ceiling of 32. Reads overlap, and a determinism suite holds the property that
> overlapping them cannot change which answer comes back, only when the bytes
> arrive.
>
> **The wall-clock figures derived from that serial cost are therefore upper
> bounds.** "53,063 objects over 1,000 shard dirs: 48,093 requests ... 32
> minutes at 40 ms" assumes one request outstanding at a time. The request
> count is unchanged, because the tool still reads one shard document per shard
> directory per generation. Only the elapsed time moves.
>
> **The memory figure has been superseded by a measurement at two sizes.** One
> peak divided by one object count cannot separate a fixed baseline from a
> per-object rate. Measured on a live repository:
>
> ```
>  94,600 objects -> 404,904 KB peak
> 209,420 objects -> 526,516 KB peak
>
> marginal cost   1.06 KB per object
> fixed baseline  298 MB
> ```
>
> The marginal cost is below the 1.9 KB the model uses, so the ceiling in
> `sources/budget.py` refuses runs that would in fact fit, which is the safe
> direction. The 298 MB baseline is not modelled at all, which is the unsafe
> direction, and it only matters below roughly 512 MB of available memory.
>
> **What has not changed is the shape.** Cost is still linear in generations and
> in shard directories, requests are still one shard-document GET per shard
> directory per generation, and listing is still correct at depth.

OCI signing costs 16.03 ms of pure-python RSA per request, 2,000x the SigV4
path, and lands on the serial critical path.

**Failure behaviour.** Only three reads are fatal: the listing, `index.latest`,
and the anchor generation. Everything else is local. The manifest NEVER GREW
across 232 single-failure and 200+ multi-failure runs.

    rate        runs completed   manifest recovered      coverage claimed
    1 in 10000  100% (30/30)     99.98% mean/99.79% worst  99.83%
    1 in 1000   100% (30/30)     99.52% mean/96.63% worst  94.89%
    1 in 100    63.3% (19/30)    95.78% mean/90.88% worst  68.96%

So the feared outcome, a guard refusing so often the operator bypasses it, does
NOT occur at realistic rates. Coverage understates, which is the safe direction.
Refusal probability scales with `listing_pages + 2`.

**The one dishonest channel.** `KeyIndex._still_there` catches every exception
from `source.exists` and records False, so "the store said no" and "the store
could not answer" are the same value. Isolating HEAD failures: at 1 in 1000,
99.90% recovered but coverage claims 100.0% and about 31 of 30,938 keys vanish
unreported; at 1 in 100, about 309 vanish and coverage still claims 100.0%. It
leaks rather than deletes, so not the dangerous direction, but it is the only
measured place where the report is wrong rather than conservative.

A 503 at 5 percent per attempt costs zero coverage, absorbed by 8 retries, at
21.2 s of backoff per 712 requests. A 403 is not retried at all: 1 in 100 costs
3.5 percent of the manifest immediately. `--elasticsearch` adds three more fatal
calls with NO retry.

## Live-data defects known open at the time work stopped

A reviewer's REPRO3, four counterexamples, every repository a modern 7.12+ shape:

D1. A document that parses and names nothing satisfies the per-directory subset
test against every directory. `shard_snapshots.py`'s own docstring says such a
document must raise. The retired `s3_repo_sweeper.py:2743` carries that gate;
this package dropped it.

D2. Same hole with a donor Elasticsearch really writes: a document naming only
inline `v__` entries, which is a snapshot of an empty index.

D3. A real document from another shard whose blob set is contained in the victim
directory. Manifest count does not move; a live segment replaces a dead one.

D4. `_blobs_present` builds identity evidence from the listing and never
confirms it, while `KeyIndex._still_there` confirms every key before naming it.
The same listing is distrusted where it could add a key and trusted where it
protects live data.

The `--elasticsearch` veto rescues none of them: `Veto.covers` keys off
`condemnation.snapshot_uuid`, and a mis-attributed row wears the dead snapshot's
uuid. The veto protects by identity, so it cannot catch a failure of attribution.

**Anchor defect, confirmed on the rig before work stopped.** (The rig is the
local test lab reproducing the fault; see [FACTS.md](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/FACTS.md#the-test-lab-henceforth-the-rig).)
A repository left by
an ordinary crash between writing `index-N+1` and updating `index.latest` causes
the tool to name TWO LIVE KEYS. No store misbehaviour, no tampering. This
follows directly from the tool anchoring on `index.latest` while Elasticsearch
lists and takes the highest.

## Test-suite trust, and why it must be revalidated

One reviewer neutered 17 guards against the finished package: 4 went red, 13
changed nothing. A second reviewer neutered more and found seven vacuous, including the
pair that is the entire defence against naming live data.

Removing BOTH the live-blob subtraction and the classification take-back makes
the tool name all six shared blobs on that reviewer's `share` state, with the suite
staying green. **There is currently no test that fails when the tool condemns
live data.**

The three identity checks cover for each other: deleting any ONE leaves 135
tests green; only removing all three fails a single test.

`audit.py`'s `condemned = surviving_orphans(placements, condemned)` can be
deleted with the suite green. Its test imports the functions directly and never
runs `run_audit`, so the veto's wiring is unpinned.

The monotonicity generator could not reach the failing region, TWICE. One source
mutation at a time so listing cases are unreachable; every fixture document names
at least one blob so the empty-document cases have no donor; both same-generation
swaps use donors whose blob sets are not subsets, exercising the subset test only
where it succeeds.

The developer's own neuter harness produced FALSE GREENS until bytecode caching
was disabled: two same-size edits inside one second left a stale `.pyc`, so a
guard reported as proved with nothing rebuilt.

Suite composition, measured: 3,726 test lines against 4,641 source lines, 136
tests, 20 of 269 assertions asserting a literal string (7 percent, against 21
percent in the retired suite).
