# Repository layout, and how we decide an object is unreferenced

An Elasticsearch snapshot repository is a flat object store with a naming
convention laid over it. There are no directories underneath, no index, and no
manifest that lists what is alive. A key like
`indices/UmMr6LLaRKiO6Q38EFgv3A/0/__C2GJC5MdR56g9UyMI1j8wQ` tells you which
shard directory an object sits in and nothing else. It does not say what the
object holds, which snapshots need it, or whether any snapshot still exists.

Everything a delete decision rests on has to be read back out of the metadata
blobs that sit beside the data. This page describes those blobs, the graph they
form, and the method we use to decide that a given object is reachable from no
surviving snapshot. It is about the data and the reasoning, not about any
particular program that implements it.

For what a wrong delete actually costs once it happens, read
[Blast radius](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/docs/blast-radius.md). That page covers segment sharing, the byte
level detail of deduplication, and the damage model. This page does not repeat
it.

## A note on where each fact comes from

Elasticsearch publishes no on disk format specification. Its source is the
authority for the format, and a captured real repository is the authority for
what an actual one contains. This project keeps those two apart on purpose,
because a claim read at source and a claim measured in a bucket fail in
different ways, and because at least four times a plausible reading of the
source has been contradicted by running it.

So every format claim below says which it is. **Read at source** means
Elasticsearch 9.5.2 source. **Measured** means observed in
`tests/fixtures/real-es952-repo.tar.gz`, a repository written by a real
Elasticsearch 9.5.2 cluster.

---

# Part one: how the data is structured

## The tree

Elasticsearch documents its own layout in
`server/src/main/java/org/elasticsearch/repositories/blobstore/package-info.java`.
Read at source:

```text
STORE_ROOT
|- index-N               RepositoryData, JSON
|- index.latest          numeric, latest generation N
|- snap-<uuid>.dat       SMILE SnapshotInfo
|- meta-<uuid>.dat       SMILE Metadata
|- indices/<index-uuid>/
   |- meta-<id>.dat
   |- 0/                 shard directory
      |- __<segment>     data blobs
      |- snap-<uuid>.dat
      |- index-<gen>     BlobStoreIndexShardSnapshots
```

**SMILE** is Jackson's binary JSON encoding ([format specification](https://github.com/FasterXML/smile-format-specification/blob/master/smile-specification.md)). Elasticsearch selects it through [`XContentType.SMILE`](https://github.com/elastic/elasticsearch/blob/main/libs/x-content/src/main/java/org/elasticsearch/xcontent/XContentType.java) and writes these blobs with Jackson, so reading one
means implementing that specification rather than pointing a JSON parser at it.

Two prefixes appear at more than one level and mean different things at each.
`index-` at the root is a numeric repository generation; `index-` inside a shard
directory is a shard generation named by a uuid. `snap-<uuid>.dat` at the root
is the whole snapshot's `SnapshotInfo`; the same name inside a shard directory
is that snapshot's file list for that one shard. Blast radius works through both
collisions in detail, with the source constants that produce them.

## The root pointer, and the generation blobs

`index.latest` holds a number. `index-N` for that number is the current
`RepositoryData`, the repository's catalog of what exists.

The important part is which of the two Elasticsearch trusts. Read at source, its
documented order is to list all blobs with the prefix `index-` under the
repository root and select the one with the highest N, and to read `index.latest`
only if that listing fails. Listing is primary. `index.latest` is the fallback.

That ordering is not a detail. It means the highest generation present in the
store is the repository's real state, whether or not the pointer caught up. A
cluster that crashes between writing `index-N+1` and updating `index.latest`
leaves a repository where Elasticsearch reads generation N+1 and a reader that
trusts the pointer reads generation N. The objects that only N+1 references then
look unreferenced to that reader, and they are live.

## What a generation carries

Read at source, from `RepositoryData.java`, an `index-N` blob carries:

| Field | What it is |
|---|---|
| `min_version` | A deserialization floor. It exists "to make it impossible for older ES versions to deserialize this object". |
| `uuid` | The repository uuid. |
| `cluster_id` | The cluster that wrote it. |
| `snapshots` | The live snapshots as of this generation. |
| `indices` | The live indices as of this generation. |
| `index_metadata_identifiers` | Maps an index metadata content identifier to the blob id that holds it. |

Per snapshot, the generation records `name`, `uuid`, `state`,
`index_metadata_lookup`, `version`, `index_version`, timestamps and
`slm_policy`. Per index, it records `id`, `snapshots`, and `shard_generations`,
which source describes as indexed by shard position.

`shard_generations` is the link into the shard directories: position 0 in that
array names the current generation id for shard 0, position 1 for shard 1, and
so on. The array position carries the shard number. Nothing inside the shard
generation blob repeats it.

Measured: `min_version` reads `7.12.0` on every generation in the captured
repository, not the version of the cluster that wrote it. It is a floor, so
treat it as gating which format rules apply, not as telling you which
Elasticsearch produced the blob.

Read at source, `RepositoryData` has no field referencing a previous generation.
This matters more than it looks and comes back in part two.

## What a snapshot declares about itself

The root level `snap-<uuid>.dat` is the snapshot's `SnapshotInfo`. Measured, it
declares the extent of the snapshot: an `indices` list, `total_shards`,
`successful_shards`, and per index an `index_details` entry giving
`shard_count`, `size_in_bytes` and `max_segments_per_shard`.

This is the only place in the repository where a snapshot states how large it is
supposed to be. A traversal of that snapshot can be checked against it.

## The shard directory and the shard document

`indices/<index-uuid>/<shard>/` holds the data blobs for one shard, one
`snap-<uuid>.dat` per snapshot that covered that shard, and the shard
generations `index-<gen>`.

The shard generation blob is a `BlobStoreIndexShardSnapshots`. Measured, its top
level is exactly two members, `files` and `snapshots`, and nothing in it names
its own shard, its own index, or its own generation. A shard document lifted out
of its directory is indistinguishable from any other shard document by
inspection of its contents. Its identity comes from where it was found and from
what it names, never from a field inside it.

Measured, each entry in `files` carries:

| Field | What it is |
|---|---|
| `name` | The blob name in this shard directory, `__<blobid>` or `v__<blobid>`. |
| `physical_name` | The Lucene file name, for example `_0.cfs`. |
| `length` | The file's length. |
| `checksum` | The Lucene checksum. |
| `writer_uuid` | Identifies the writer of the underlying Lucene file. |
| `written_by` | The Lucene version string. |
| `part_size` | Set when a file is split across `.partK` objects. |
| `meta_hash` | For inline files, the file's whole content. |

Measured, `snapshots` names the snapshots whose file lists this document holds,
for example `['v9-snap-1', 'v9-snap-2']`.

## The blobs, and the entries with no blob behind them

An entry whose `name` starts with `__` refers to an object in that shard
directory. An entry whose `name` starts with `v__` does not. Elasticsearch
stores those files inline, in the entry's `meta_hash`, and never writes an
object for them. Blast radius covers which files qualify, quoting the source
predicate that decides, and shows the missing objects against a real listing.

For reachability the consequence is short. A `v__` entry is a reference to
nothing, so it can never make an object reachable, and the absence of an object
for it is not damage.

## The reference structure

The tree is the easy half. The graph over it is what a delete decision actually
runs on.

```text
                        index.latest
                             |  (fallback pointer only)
                             v
   index-0   index-1  ...  index-N          RepositoryData per generation
                             |
        +--------------------+--------------------+
        |                                         |
        v                                         v
   snapshots[]                                indices[]
   name, uuid, state,                         id, snapshots[],
   index_metadata_lookup                      shard_generations[]
        |                                         |
        | index id -> identifier                  | shard position -> gen id
        v                                         v
   index_metadata_identifiers          indices/<uuid>/<shard>/index-<gen>
        |                                         |
        | identifier -> blob id                   | files[] and snapshots[]
        v                                         v
   indices/<uuid>/meta-<blobid>.dat    indices/<uuid>/<shard>/__<blobid>
```

Four properties of that graph drive everything in part two.

**One blob is referenced by many snapshots.** Elasticsearch deduplicates
segments, so a snapshot of an unchanged shard uploads nothing and references the
blobs the earlier snapshot uploaded. The reference count of a data blob is
normally greater than one, and you cannot read it off the key. This is why a
single wrong delete damages several snapshots at once rather than one, and it is
the subject of [Blast radius](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/docs/blast-radius.md).

**The union is stored, not assembled.** Measured, a shard document holds every
snapshot's file list for that shard, not just the newest one. You do not build
the shard's reference set by walking snapshots and merging. It is already
written down in one blob. That saves a great deal of work, and it also
concentrates the risk: one unreadable blob costs you the whole shard's reference
set, not one snapshot's share of it.

**Blobs are shard scoped.** An object under `indices/X/N/` is reachable only
through the documents in that same directory. No document elsewhere in the
repository can name it. That is what makes the delete decision local, and it is
what bounds the damage of a mistake to one shard directory.

**There are two kinds of reference edge, and they behave differently.** A
snapshot reaches a segment blob through the shard document, in one hop from a
shard generation named by `shard_generations`. A snapshot reaches its index
metadata blob through `index_metadata_lookup`, which yields an identifier, which
`index_metadata_identifiers` at the generation level turns into a blob id. The
second path runs through two maps at two different levels of the tree and is
keyed by content rather than by snapshot uuid, so it is deduplicated across
snapshots on a different rule from segments. Its failure modes are its own. In
this project's own history the metadata path produced three separate cases where
a live blob looked unreferenced, all from that second edge type alone.

**Generations carry no pointer to a prior generation.** Read at source, there is
no such field. The "chain" of generations is nothing but an ordering by the
number in the name. What you can know about the set of generations comes from
two places: the numbering is monotonic, so the expected set is contiguous up to
some N, and `index.latest` plus a listing tell you where the top is. Chain
completeness comes free from that. Completeness of a traversal within a single
generation does not, and that is a separate problem taken up below.

---

# Part two: how we determine what can be removed

## Elasticsearch's own algorithm

Elasticsearch states its garbage collection rule in the same package
documentation that gives the layout. Read at source, when it finishes deleting a
snapshot it will:

> Collect all segment blobs (identified by having the data blob prefix `__`) in
> the shard directory which are not referenced by the new
> `BlobStoreIndexShardSnapshots`

and delete those.

That is the whole algorithm. It is a set difference, computed inside one shard
directory, between the `__` objects present there and the objects the current
shard document names. Nothing outside the shard directory participates, and no
other snapshot's opinion is consulted, because the shard document already holds
every snapshot's file list for that shard.

Our job is to reproduce that same difference from outside the cluster, reading
the same blobs, without Elasticsearch's advantage of having just written them.

## The subtraction is not the hard part

Set difference is arithmetic. It is not where this goes wrong.

Every failure this project has found, without exception, came from the live side
of the difference being incomplete. A reference we failed to see is a reference
that does not appear in the live set, and a live blob that appears in no live
set is indistinguishable from garbage. The arithmetic is then performed
correctly on a wrong input and condemns a blob some surviving snapshot still
needs.

So the question worth spending effort on is never "did we subtract correctly".
It is "is our live set complete", and everything below is about that.

## The safety condition

State it precisely, because the loose version invites the wrong tradeoff.

Let G\* be the true reference graph and G the one we believe. Condemning a blob b
is sound only if b has degree zero in G\* once the delete is done. What we
actually compute is that b has degree zero in G. That inference is valid if and
only if G contains every edge that G\* has among the surviving snapshots.

In other words, our believed set of references must be a **superset** of the
true one. It may contain edges that are not really there. It may not miss any.

The asymmetry is total, and it is the reason the whole method can be made safe.
An edge we believe in wrongly costs storage: an object survives that could have
been removed, and the repository keeps leaking, which is the condition it was
already in. An edge we miss destroys data: a live segment goes away and several
snapshots break at once, silently, until somebody tries a restore.

Every design decision therefore resolves the same way. When something is
uncertain, add the reference. When a document will not parse, when a listing is
incomplete, when a field means something we do not recognise, the safe move is
always more edges and never fewer.

The strongest form of this is worth saying out loud, because it looks like
giving up and is not. **A shard we cannot read completely contributes nothing.**
Dropping the whole shard directory out of the candidate set means condemning
nothing in it, which is the maximum possible edge addition, which is the safest
available answer. A tool that instead salvages what it could read from a
partially readable shard has quietly chosen the one direction the asymmetry
forbids.

## What establishes completeness

Four things carry weight, and they answer different questions.

**The anchor is the highest valid generation, found by listing.** Not the
generation `index.latest` points at. This is the same rule Elasticsearch uses
for itself, and it exists because the pointer can lag the store after an
ordinary crash. Anchoring on the pointer instead of on the listing turns a
routine crash into a set of live objects that look dead. Where the two disagree,
the higher one wins, because believing the higher one only ever adds references.

**`min_version` gates the format.** It is a deserialization floor, so it tells
you which format rules the blob was written to expect. It does not tell you
which Elasticsearch wrote it; measured, it reads `7.12.0` on every generation of
a repository written by 9.5.2. Use it to decide whether you are entitled to
parse the blob at all, and refuse when it names a format you do not implement.
Refusing is the safe direction.

**The snapshot's declared extent says how big the traversal should have been.**
Measured, `snap-<uuid>.dat` carries `total_shards`, `successful_shards`, an
`indices` list and per index a `shard_count`. A traversal that visited fewer
shards than the snapshot declares did not finish, and its live set is short by
however many shards it missed. This is the one check that catches an incomplete
traversal from inside a single generation, which the generation numbering cannot
do, because the numbering only tells you that you have all the generations, not
that you read all of any one of them.

**The Lucene commit point corroborates the file list independently of
Elasticsearch.** `segments_N` is Lucene's own record of which segments an
index needs to open. It is written by Lucene, not derived from the snapshot
file list, and Elasticsearch never reads it back to check the two agree.
`generation_chain/formats/lucene_segments.py` decodes it and requires that
every segment it names has some physical file representing it in the
snapshot's own declared list. Measured against the 14 commit points captured
across the real 9.5.2 fixtures, from one to eight segments in one commit,
all of them decode this way, footer checksum included, and all of them are
carried inline in the file entry that names them.

This is what closes [issue #1](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/issues/1)
for the case it names: a tamper, or an upstream format change, that removes
the same live segment from both `index-<gen>` and `snap-<uuid>.dat` at once.
Every other check on this page reads one stored copy against another,
including asking Elasticsearch, so all of them are blind to a change that
moves both copies together. `segments_N` does not move with them. It is a
different source of truth, written by a different layer for a different
reason, and a change to the snapshot file list does not silently move it.

It rests on the commit being carried inline, which is measured true of every
commit point captured so far, not verified as a rule the format guarantees.
When it is not inline, this check has no bytes to compare and does not run;
it falls back to the presence-only gate this page's earlier sections already
assumed, and the run's coverage report counts how often that happened rather
than staying silent about it.

It cannot see a tamper with enough reach to rewrite the commit point along
with both stored copies of the file list. That needs the same object-store
write access an attacker would need to delete the blobs directly, so against
that adversary this raises the bar rather than closing the door. Against the
case that matters more here, an upstream format change nobody staged, the
commit point and the file list are different sources of truth written by
different code, and a change to one does not move the other.

## What establishes identity

Completeness asks whether we saw everything. Identity asks whether what we saw
belongs where we found it. Both have to hold, because a live set assembled from
the wrong shard's document is complete and useless.

Measured, a shard document names neither its shard, its index, nor its
generation. So identity has to be reconstructed from two other things.

**`writer_uuid` discriminates between shards.** Measured across three indices in
the captured repository, the sets of `writer_uuid` values in different shards'
documents had zero overlap in all three pairwise comparisons, and the set was
stable within one shard across its generations, with the same nine distinct
values appearing in all three of that shard's documents. That makes it usable
evidence that a document belongs to the shard you found it in. The measurement
has a stated limit: it has not been tested across two shards of the *same*
index, so treat it as evidence and not as proof.

**A live document's named blobs should be present where it was found.** If the
document you believe is the current one for a shard directory names blobs, those
blobs should exist in that directory. It is a cheap check and it catches a
document that came from somewhere else entirely.

It is necessary and not sufficient, and the gap is worth naming so nobody leans
on it. A document that names no blobs at all satisfies it against every
directory in the repository, and so does a real document from a different shard
whose blob set happens to be contained in this directory's. Elasticsearch really
does write documents that name only inline `v__` entries, which is what a
snapshot of an empty index produces, so the naming-nothing case is not
hypothetical. Presence is a filter. Identity still has to come from somewhere
else.

## What this method cannot see

Three things, stated plainly, because a method that only lists its own strengths
is not worth trusting.

**A snapshot being written right now.** Its blobs are on disk before the shard
document that names them is committed, so from outside the cluster they have
degree zero and are indistinguishable from garbage. Degree zero does not mean
deletable. It means deletable *if the repository is quiet*, and nothing in the
repository tells you whether it is. Establishing that has to come from outside
the object store.

**An aborted snapshot.** It leaves blobs behind that genuinely are referenced by
nothing, and unlike the case above they are never going to be. They are also not
what Elasticsearch's own algorithm reclaims, because that algorithm runs on the
shard document produced by a delete, and no delete happened here. So this debris
sits outside the model on both sides: our method sees no reference to it, and
Elasticsearch never claimed it would clean it up.

Those two cases produce the same observation, an object nothing references, and
have opposite correct answers. That is the honest statement of the limit: degree
zero alone does not separate them.

**A consistent tamper of the stored metadata.** Most checks described on this
page read the same object store. If the stored metadata is changed consistently,
so that the shard document and the per snapshot document agree with each other on
a file list that is missing a live segment, every check that only compares one
stored copy against another stays silent, because the copies agree. Asking
Elasticsearch does not help either, because Elasticsearch reads the same blobs.

The Lucene commit cross-check under "What establishes completeness" is the
one exception, and this section used to describe the attack it catches as
invisible to everything here. That is no longer true, and it is worth being
precise about what changed rather than quietly leaving the older, blunter
claim standing. `segments_N` is not a second stored copy of the file list. It
is a different source, written by Lucene rather than by the code that writes
the snapshot file list, and a tamper that edits only the file list does not
move it. So the specific case this section named, dropping the same live
segment from both stored copies while leaving `segments_N` alone, is caught.

What is left is narrower than what used to be claimed here. A tamper with
enough reach to rewrite the commit point too, consistently with both stored
copies, is still invisible to everything on this page, because all three
now agree. Reaching that needs the same object-store write access that
already lets an attacker delete the blobs directly, so this is a smaller
version of the same limit rather than a different one: the evidence still
lives in one store, and enough care in tampering with that store still
produces a state no amount of care in reading it can tell from a correct one.

This is [issue #1](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/issues/1),
and it stays open for the remainder above. Closing that remainder was priced
and declined before this cross-check existed, on the grounds that defending
against reimplementation drift by adding another reimplementation is
self-defeating, and that reasoning applies just as much to a decoder for
whatever would corroborate the commit point next. Blast radius carries the
fuller pricing history, including the measurement showing why a cheaper,
non-structural version of a commit-point check does not work.

## Sources

Elasticsearch source, version 9.5.2.

- `server/src/main/java/org/elasticsearch/repositories/blobstore/package-info.java`:
  the layout tree, the generation lookup order, and the deletion algorithm
  quoted above.
- `server/src/main/java/org/elasticsearch/repositories/RepositoryData.java`:
  the fields written to `index-N`, the per snapshot and per index members, the
  meaning of `min_version`, and the absence of any pointer to a prior
  generation.

Measurements against a captured repository written by Elasticsearch 9.5.2,
`tests/fixtures/real-es952-repo.tar.gz`: the observed value of `min_version`,
the two member top level of a shard document, the file entry fields, the
`writer_uuid` overlap comparisons, the presence of every snapshot's file list in
one shard document, and the extent fields in `snap-<uuid>.dat`.

Related reading in this repository.

- [Blast radius](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/docs/blast-radius.md): why segments are shared, what a wrong delete
  costs, the inline `v__` files, and the full accounting of issue 21.
