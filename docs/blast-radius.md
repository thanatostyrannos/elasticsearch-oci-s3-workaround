# Blast radius: what a wrong delete costs in a snapshot repository

> [!IMPORTANT]
> **This document was written against three tools that are now retired and no
> longer in this repository.** They classified objects LIVE, ORPHAN or
> PROTECTED by reimplementing Elasticsearch's on-disk format, then deleted the
> orphans, condemning a blob for being absent from a live set the tool
> computed itself. A failed read and an unparseable document both resolved
> toward deleting. `generation_chain`, the tool this repository ships today,
> replaced them: it reproduces Elasticsearch's own shard-local set difference,
> condemns on PRESENCE rather than absence, and its audit half has no delete
> path at all.
>
> What follows is an account of what a wrong delete costs in a snapshot
> repository, which is why this document exists, and it applies to
> `generation_chain.reclaim` exactly as it applied to the retired tools: any
> tool that deletes objects out of a live repository can cause this damage.
> Passages that described the retired tools' own guards, flags or internals
> have been removed or replaced with what `generation_chain` does instead;
> everything else is Elasticsearch and Lucene format fact, unchanged by which
> tool reads it.

Every question an operator asks about a delete tool comes down to one thing. If
it deletes the wrong object, what breaks, and how much of it.

For an Elasticsearch snapshot repository that question has an answer you cannot
read off the key. An object called `__mBaXrJyaTAKMhSTKN2mMJw` gives you nothing.
It does not say which snapshots reference it, and the number is usually more
than one, because Elasticsearch snapshots share segment data. A snapshot that
changes nothing in a shard does not copy that shard's segments. It references
the ones the previous snapshot uploaded.

That sharing is where the danger comes from. It is also where the best defence
comes from, and this document is about both.

The tool this repository ships, `generation_chain.reclaim`, deletes objects
out of production snapshot repositories. Read this before you trust it with
yours.

## Contents

- [Why one object belongs to many snapshots](#why-one-object-belongs-to-many-snapshots)
- [What every key is, and what it is worth](#what-every-key-is-and-what-it-is-worth)
- [The files that have no object at all](#the-files-that-have-no-object-at-all)
- [What a wrong delete costs](#what-a-wrong-delete-costs)
- [Sharing is also the best defence](#sharing-is-also-the-best-defence)
- [What changed in how this is guarded](#what-changed-in-how-this-is-guarded)
- [What none of this covers](#what-none-of-this-covers)
- [Before you run a delete](#before-you-run-a-delete)
- [Sources](#sources)

## Why one object belongs to many snapshots

Lucene segments are immutable. Once written, a segment file never changes, so a
second snapshot of an unchanged shard has nothing new to upload. Elastic's own
documentation puts it plainly: "Snapshots are automatically deduplicated to save
storage space and reduce network transfer costs. To back up an index, a snapshot
makes a copy of the index's segments and stores them in the snapshot repository.
Since segments are immutable, the snapshot only needs to copy any new segments
created since the repository's last snapshot."

The decision happens per file, in
[`BlobStoreRepository.doSnapshotShard`](https://github.com/elastic/elasticsearch/blob/v9.5.2/server/src/main/java/org/elasticsearch/repositories/blobstore/BlobStoreRepository.java#L3557):

```java
BlobStoreIndexShardSnapshot.FileInfo existingFileInfo = snapshots.findPhysicalIndexFile(md);
```

`snapshots` there is the shard's current `BlobStoreIndexShardSnapshots`, which is
the file list of every snapshot the shard already holds.
[`findPhysicalIndexFile`](https://github.com/elastic/elasticsearch/blob/v9.5.2/server/src/main/java/org/elasticsearch/index/snapshots/blobstore/BlobStoreIndexShardSnapshots.java#L143)
builds a map from Lucene physical name to every `FileInfo` any snapshot in that
shard recorded under that name, then asks each candidate whether it describes the
same file. If one does, the new snapshot records a reference to the existing blob
and uploads nothing.

So a `__<blobid>` object is reachable from every snapshot whose shard file list
names it, and it becomes garbage only when the last of those snapshots goes away.

### What that looks like in a bucket

Reasoning from source has given this project the wrong answer at least four times
where running it gave the right one, so everything below was measured on the rig,
the local test lab that reproduces the fault (Elasticsearch 9.5.2 under ECK in
Rancher Desktop against a pinned MinIO, defined in [FACTS.md](../FACTS.md#the-test-lab-henceforth-the-rig)):
Elasticsearch 9.5.2 under ECK, snapshots on MinIO through the S3 API.

One index, one shard, 2,000 documents, force-merged to a single compound segment.
Snapshot `br-snap-1` left the repository at 9 objects and 463,930 bytes, of which
two `__` data blobs held 460,478. Then `br-snap-2` ran with nothing at all
changed in the index. It added exactly this:

```text
914  index-1
885  indices/UmMr6LLaRKiO6Q38EFgv3A/0/index-IdMvhTCFQfqvWzgVGywEZg
878  indices/UmMr6LLaRKiO6Q38EFgv3A/0/snap-yoFZ8XoQTD20O94grpChXw.dat
212  meta-yoFZ8XoQTD20O94grpChXw.dat
312  snap-yoFZ8XoQTD20O94grpChXw.dat
```

Five objects, 3,201 bytes, every one of them metadata. The 459,736-byte `_0.cfs`
was not re-uploaded, nothing was removed, and no object changed size. Decoding
the shard generation blob shows why: the file arrays `br-snap-1` and `br-snap-2`
record are identical, naming the same `__C2GJC5MdR56g9UyMI1j8wQ` and
`__UdeH6t9fQdmBZHJzgZ7-hA`.

Adding 50 documents and taking `br-snap-3` uploaded two new data blobs and kept
both old ones. After three snapshots the repository held 21 objects and 496,069
bytes, of which four data blobs carried 485,386. Two of those four, holding
460,478 bytes between them, are referenced by all three snapshots. Delete either
one and you have not damaged a snapshot, you have damaged three.

### What "the same file" means, exactly

The comparison is
[`StoreFileMetadata.isSame`](https://github.com/elastic/elasticsearch/blob/v9.5.2/server/src/main/java/org/elasticsearch/index/store/StoreFileMetadata.java#L133),
and it is worth reading rather than summarising, because the summary that
circulates in this repository's own docs is not quite right. Three paths:

1. If the file's content is stored inline (`hashEqualsContents()`, which covers
   `.si` and `segments_N`), the comparison is byte for byte on the content.
   Length and checksum are not consulted at all.
2. Otherwise, if both sides carry a `writerUuid`, that field decides. A mismatch
   returns false immediately.
3. Otherwise, length, checksum and hash must all match.

The physical name is matched one level up, by the map lookup in
`findPhysicalIndexFile`. The `writtenBy` field, the Lucene version string, is
serialised and printed and never compared.

The part that matters for blast radius is what is missing from all three paths.
Nothing asks the object store whether the blob is still there. Deduplication is
a pure metadata operation. If a blob has been deleted out from under the
repository, the next snapshot will still match against its `FileInfo`, still
record a reference to it, and still report `SUCCESS`.

## What every key is, and what it is worth

The sharing is not uniform. Some keys are one per snapshot, some are one per
repository, and the ones holding almost all the bytes are shared. Every row
below was checked against Elasticsearch 9.5.2 source, which is the version the
rig runs.

| Key | Container | Content | How many snapshots reference it |
|---|---|---|---|
| `index.latest` | repository root | 8 bytes, big-endian generation number | one per repository |
| `index-<N>` | repository root | `RepositoryData` as JSON: live snapshot uuids, live index uuids, per shard generation ids, the index metadata identifier map | one per repository generation |
| `snap-<snapshot-uuid>.dat` | repository root | `SnapshotInfo` | one per snapshot |
| `meta-<snapshot-uuid>.dat` | repository root | global cluster metadata | one per snapshot |
| `indices/<index-uuid>/meta-<content-uuid>.dat` | index | `IndexMetadata` | shared by every snapshot whose settings, mappings and aliases version are unchanged |
| `indices/<index-uuid>/<shard>/index-<generation>` | shard | `BlobStoreIndexShardSnapshots`, the whole shard's file list across all its snapshots | one per shard generation |
| `indices/<index-uuid>/<shard>/snap-<snapshot-uuid>.dat` | shard | `BlobStoreIndexShardSnapshot`, one snapshot's file list for that shard | one per snapshot, per shard |
| `indices/<index-uuid>/<shard>/__<blobid>[.part<K>]` | shard | Lucene segment data | **shared, by every snapshot whose shard file list names it** |
| `incompatible-snapshots` | repository root | legacy marker | one per repository |

The prefixes are constants in `BlobStoreRepository`: `INDEX_FILE_PREFIX`,
`INDEX_LATEST_BLOB`, `METADATA_PREFIX`, `SNAPSHOT_PREFIX`,
`SNAPSHOT_INDEX_PREFIX`, `UPLOADED_DATA_BLOB_PREFIX` and
`VIRTUAL_DATA_BLOB_PREFIX`.

Four things in that table trip people up, and three of them are corrections to
the shorter version of it that has been passed around this project.

**`snap-` and `meta-` each name two different things.** At the repository root,
`snap-<uuid>.dat` is the `SnapshotInfo` and `meta-<uuid>.dat` is the global
cluster metadata, both keyed by the snapshot uuid, both genuinely one per
snapshot. Under `indices/<index-uuid>/<shard>/`, `snap-<uuid>.dat` is that
snapshot's file list for that one shard. Same prefix, same uuid, different
container, different payload class. A taxonomy row that does not say which
container it means is ambiguous enough to be dangerous.

**Index metadata is deduplicated, not one per snapshot.** In
[`finalizeSnapshot`](https://github.com/elastic/elasticsearch/blob/v9.5.2/server/src/main/java/org/elasticsearch/repositories/blobstore/BlobStoreRepository.java#L2011)
Elasticsearch computes a content identifier and reuses an existing blob if it has
one already:

```java
final String identifiers = IndexMetaDataGenerations.buildUniqueIdentifier(indexMetaData);
String metaUUID = existingRepositoryData.indexMetaDataGenerations().getIndexMetaBlobId(identifiers);
if (metaUUID == null) {
    metaUUID = UUIDs.base64UUID();
    INDEX_METADATA_FORMAT.write(indexMetaData, indexContainer(index), metaUUID, compress);
    ...
```

[`buildUniqueIdentifier`](https://github.com/elastic/elasticsearch/blob/v9.5.2/server/src/main/java/org/elasticsearch/repositories/IndexMetaDataGenerations.java#L185)
is the index uuid, the history uuid, and the settings, mapping and aliases
versions. The uuid in that filename is not a snapshot uuid, which is why the
sweeper resolves those blobs through the root catalog's
`index_metadata_identifiers` map rather than against the live snapshot set.

The rig shows the difference between the two `meta-` blobs directly. Three
snapshots of one unchanged index produced three root-level `meta-<uuid>.dat`
objects, one per snapshot, each named by that snapshot's uuid, and exactly one
index-level blob:

```text
556  indices/UmMr6LLaRKiO6Q38EFgv3A/meta-AGpTOaABbI494YOgaJdG.dat
```

The root `RepositoryData` carries the mapping that makes it shared, in two of its
members:

```json
{
  "index_metadata_lookup": {"UmMr6LLaRKiO6Q38EFgv3A": "Ac02iTU6R_OG49YND4gBcg-_na_-1-2-1"},
  "index_metadata_identifiers": {"Ac02iTU6R_OG49YND4gBcg-_na_-1-2-1": "AGpTOaABbI494YOgaJdG"}
}
```

All three snapshots point at the same identifier. Adding one field to the
mapping bumped `mapping_version`, and the next snapshot wrote a second blob,
`meta-AWpWOaABbI494YOgGZcF.dat`, while still uploading no new data blobs. A tool
that treats an index-level `meta-` blob as belonging to one snapshot will delete
metadata that other snapshots still need.

**The shard generation is a uuid, not a number.**
[`ShardGeneration.newGeneration()`](https://github.com/elastic/elasticsearch/blob/v9.5.2/server/src/main/java/org/elasticsearch/repositories/ShardGeneration.java#L38)
returns `UUIDs.randomBase64UUID()`, and the class javadoc records the change:
"Before 7.6 these generations were numeric, but recent versions use a UUID
instead." So a shard directory holds `index-<base64 uuid>`, while the repository
root holds a numeric `index-<N>`. Both use the literal prefix `index-`, from two
separate constants that happen to have the same value. Only the root one is a
number, and only the root one is ordered.

**`index.latest` is not guaranteed to exist.**
[`maybeWriteIndexLatest`](https://github.com/elastic/elasticsearch/blob/v9.5.2/server/src/main/java/org/elasticsearch/repositories/blobstore/BlobStoreRepository.java#L3251)
writes it only when the repository's `support_url_repo` setting is on. It
defaults to on, so in practice it is there, and its content really is exactly
eight bytes holding a big-endian long. It is not, however, the authoritative
generation pointer: `latestIndexBlobId` prefers listing `index-*` and falls back
to reading `index.latest` only when listing is unsupported.

## The files that have no object at all

Some files named in the shard metadata have no object behind them. Elasticsearch
stores their content inline, under the `v__` prefix, and the javadoc on
`VIRTUAL_DATA_BLOB_PREFIX` says so: these are "data blobs that were not actually
written to the repository physically because their contents are already stored
in the metadata referencing them."

Which files qualify is decided by
[`Store.MetadataSnapshot.isReadAsHash`](https://github.com/elastic/elasticsearch/blob/v9.5.2/server/src/main/java/org/elasticsearch/index/store/Store.java#L957):

```java
public static boolean isReadAsHash(String file) {
    return SEGMENT_INFO_EXTENSION.equals(IndexFileNames.getExtension(file)) || file.startsWith(IndexFileNames.SEGMENTS + "_");
}
```

That is every `.si` file and every `segments_N` file, and nothing else. There is
no size threshold. The 1 MB figure people quote is an `assert` on line 989 of the
same file, active only under `-ea`, and it is an expectation rather than a gate.
`StoreFileMetadata` is blunt about what the field holds: "not really a 'hash',
it's either the exact contents of certain small files or it's empty."

The rig repository from above held four of them, decoded straight out of the
shard generation blob:

| Blob name in the metadata | Lucene file | Length | Object in the bucket |
|---|---|---|---|
| `v__GFjmOwdaSoy4Zdkp4y_Jng` | `_0.si` | 351 | no |
| `v__dGtXrgCaQ0qeCAflcyw36A` | `segments_3` | 339 | no |
| `v__xc8aHfCwTX-xA5O89zf3Ug` | `_1.si` | 351 | no |
| `v__FSW-Dui1R_uJo80yH3HERA` | `segments_4` | 465 | no |
| `__C2GJC5MdR56g9UyMI1j8wQ` | `_0.cfe` | 742 | yes, 742 bytes |
| `__UdeH6t9fQdmBZHJzgZ7-hA` | `_0.cfs` | 459,736 | yes, 459,736 bytes |

Scanning the full 21-key listing for `v__` returns nothing, and `mc stat` on one
of those keys answers "Object does not exist." Each entry carries the file's
whole content in its `meta_hash` field: the `_0.si` hash starts with that file's
own Lucene codec header.

The last two rows are the reason to trust the source over the folklore. `_0.cfe`
is 742 bytes and lives in the bucket as a real object, while `_0.si` at 351 bytes
does not. If a size threshold were doing the work, both would be inlined. The
selector is the file name.

This matters for any tool reading this format in two directions. A naive
implementation reads the absence of a `v__` object as damage and reports
phantom missing blobs, and a shape check that rejected `v__` entries as
malformed would fail on every healthy repository it was pointed at. A reader
of this format has to accept both prefixes deliberately.

It also matters for a guard described further down. Because `.si` files are
inlined, a segment's own list of its files is sitting in the shard metadata
already, readable with no extra request and no codec decoder.

## What a wrong delete costs

### Bytes do not predict damage

The first instinct on reading a delete manifest is to look at the byte total. On
this repository shape that instinct is backwards.

Start with the small clean repository from the sharing measurement. After three
snapshots it held 21 objects and 496,069 bytes, split like this:

| | Objects | Share of objects | Bytes | Share of bytes |
|---|---|---|---|---|
| `__` data blobs | 4 | 19.05% | 485,386 | 97.85% |
| everything else | 17 | 80.95% | 10,683 | 2.15% |

Four fifths of the objects carry one fiftieth of the bytes. A percentage of bytes
therefore tells you almost nothing about how many of the objects that make
snapshots readable a run is about to take.

The same asymmetry shows up in a real orphan set.
Campaign 1 on the rig audited a repository of 67 objects and 1,120,400 bytes and
classified 28 objects, 378,142 bytes, as orphaned: 41.8 percent of the objects
and 33.8 percent of the bytes. Breaking those orphans down by
what kind of object they are:

| Verdict reason | Objects | Bytes | Share of orphan bytes |
|---|---|---|---|
| segment vs current shard file set | 4 | 315,897 | 83.5% |
| root `meta-<uuid>` | 1 | 38,805 | 10.3% |
| shard `index-<gen>` vs current gen | 15 | 14,563 | 3.9% |
| shard `snap-<uuid>` | 5 | 4,382 | 1.2% |
| stale root generation | 2 | 4,027 | 1.1% |
| root `snap-<uuid>` | 1 | 468 | 0.1% |

Four objects carry five sixths of the bytes. Fifteen objects, more than half the
count, carry under four percent. The largest single orphan was 157,511 bytes,
while the pointer objects run from 468 to 2,334 bytes.

The pointers are what make snapshots readable. Delete a segment and you lose that
segment. Delete a shard's `snap-<uuid>.dat` and you lose the only list a restore
consults for that shard:
[`restoreShard`](https://github.com/elastic/elasticsearch/blob/v9.5.2/server/src/main/java/org/elasticsearch/repositories/blobstore/BlobStoreRepository.java#L3812)
calls `loadShardSnapshot`, which reads `INDEX_SHARD_SNAPSHOT_FORMAT` keyed by the
snapshot uuid, and nothing else in that path can tell it which blobs to fetch.
Delete a shard's current `index-<gen>` instead and nothing breaks. A later
re-measurement removed one from a live repository and found the next snapshot
succeeded writing zero data blobs, `_verify_integrity` passed, and every restore
came back green, indistinguishable from a control that deleted nothing. An
earlier version of this section claimed the shard lost the file list every future
snapshot deduplicates against. That did not happen, and the correction stands
here. The point the passage was making still holds through other objects: a 558-byte
index metadata blob, 0.055 percent of its repository's bytes, makes every restore
naming that index fail on every snapshot.

Report this out loud rather than leaving it to the reader. Never round a real
quantity to `0.0%`: a run that removed 11.9 KiB of metadata and made 150,000
documents unrestorable printed `0.0%` twice in the retired tools, in the two
messages written to make an operator stop. `generation_chain`'s own report
prints both the object share and the byte share, then the breakdown by
disposition, on every run, with no threshold gating it (see the sample report
in [Using it](../README.md#what-the-output-looks-like) in the README).

### One delete, every snapshot in the chain

Sharing means damage does not stay where you put it. To measure how far it
travels, the rig held two indices, `br-share` and `br-share2`, across three
snapshots, so the repository carried six index-snapshot pairs. `br-share2` was
written once and never touched again, which means its blobs were uploaded during
snapshot 1 and referenced by the other two.

Before any damage: 30 objects, 781,970 bytes, `_verify_integrity` clean with
`total_anomalies: 0`, `result: "pass"`, `index_snapshots verified 6/6`.

Then one delete pass removed every `__<blobid>` object under `indices/`, six
objects and 764,112 bytes, leaving every `snap-`, `meta-` and `index-` object
untouched. `_verify_integrity` came back with `total_anomalies: 14` and
`result: "fail"`, spread like this:

| Snapshot | Anomalies |
|---|---|
| `br-snap-1` | 4 |
| `br-snap-2` | 4 |
| `br-snap-3` | 6 |

Every anomaly reads `"missing blob"`, and between them they name all six of the
six index-snapshot pairs. Deleting blobs uploaded during snapshot 1 broke
snapshots 2 and 3, because those snapshots reference the blobs rather than
holding copies. One delete pass cut the whole retention chain.

An individual anomaly names the blob and the Lucene file behind it. This one is
verbatim from the run above, on a throwaway rig repository, so the identifiers
are real rather than illustrative:

```json
{
  "anomaly": "missing blob",
  "snapshot": {
    "snapshot": "br-snap-1",
    "uuid": "Km_Ug04tQh2xMl0tWSTt1A"
  },
  "index": {
    "name": "br-share2",
    "uuid": "u8jkcpyFRLGswjiuh6owWw",
    "shards": 1
  },
  "shard_id": 0,
  "blob_name": "__k38FFmR6SrCBudP3kdqr6A",
  "physical_file_name": "_0.cfe",
  "part_index": 0,
  "part_count": 1,
  "file_length_in_bytes": 742,
  "part_length_in_bytes": 742
}
```

Two things about that run are worth more than the anomaly count.

**The `results.status` counters do not move.** After every data blob in the
repository was gone, they read `snapshots verified 3/3`, `indices verified 2/2`,
`index_snapshots verified 6/6`, `blobs verified 27`, byte for byte the same as
the clean run. "Verified" means the API walked the object, not that the object is
intact. `total_anomalies` and `result` carry the clearest signal, and a later
re-measurement found `blobs.verified` moves as well, from 27 to 23, 27 to 15 and
4 to 0 across three cases, so it is not inert. The counters that stayed frozen
after total data loss are the snapshot, index and index-snapshot tallies.

**The snapshot APIs stay green.** With every byte of data gone,
`GET _snapshot/br-share-repo/br-snap-*` reported all three snapshots
`state: SUCCESS`, `shards {total 2, failed 0, successful 2}`, `failures: []`, and
`_status` still reported the original byte totals, 740,664 bytes for `br-snap-1`,
for data that no longer existed. Both read metadata only. A destroyed repository
looks healthy through them.

### One pointer, one dead snapshot

The extreme case is one small object. A fresh repository on the rig held one
index of 2,000 documents in one snapshot,
across 9 objects and 462,554 bytes. A restore was confirmed working first: green
index, 2,000 documents.

Then one object went, the shard-level `snap-<uuid>.dat`, 906 bytes. That is
0.196 percent of the repository's bytes and 11.1 percent of its objects. Both
data blobs survived, so 99.24 percent of the repository's bytes were still there
and still correct.

`_verify_integrity` returned `total_anomalies: 1`, and a different anomaly class
from the one above: `"failed to load shard snapshot"`, with
`blobs.verified` dropping to 0, because it can no longer reach the blob list
at all. The underlying cause is a plain 404:

```text
snapshot_missing_exception: [br-pointer-repo:br-p-1/hu2SRr6NTDecS6xqus8eDA] is missing
caused by no_such_file_exception: Blob object [.../0/snap-hu2SRr6NTDecS6xqus8eDA.dat]
not found: The specified key does not exist. (Service: S3, Status Code: 404 ...)
```

The restore that had worked minutes earlier now returns HTTP 200 with
`"shards":{"total":1,"failed":1,"successful":0}` and creates a red index whose
`docs.count` and `store.size` are blank. A `_count` against it does not return
zero; it fails with `search_phase_execution_exception` and
`no_shard_available_action_exception`, after five failed allocation attempts.
`GET _snapshot/br-pointer-repo/br-p-1` still says `state: SUCCESS`,
`failed: 0`, `failures: []`.

An earlier rig repository produced the same shape at a different scale: 23
objects and 44.3 KiB, one live snapshot, and one 905-byte shard-level
`snap-<uuid>.dat` that was 2.0 percent of the bytes and 4.3 percent of the
objects, taking that repository to one anomaly and a red index too. The
measurement stands on its own and is reproduced here.

Both fractions read as small in both repositories. The loss was total in both.
What separated 906 fatal bytes from 906 bytes of garbage was not size, it was
kind: the object was a pointer.

`_verify_integrity` did catch this one, which is worth holding on to, because the
next two cases are the ones where it does not.

### The damage moves forward into snapshots you have not taken

A snapshot taken after a bad delete reports `SUCCESS` and cannot be restored.

The mechanism is the deduplication path above. `findPhysicalIndexFile` matches
the new commit's files against the shard's existing file lists and never asks the
store whether the blob those lists name is still present, so the next snapshot
records a reference to a blob that is gone and succeeds doing it. Nothing
surfaces until somebody attempts a restore. On a daily SLM schedule that is weeks
of green, unrestorable backups, and the segments only stop being wrong when they
are rewritten by a merge.

This one is derived from the source rather than measured on the rig, from the
deduplication mechanism above. It is why [Testing in your own OCI environment](testing-in-your-oci-environment.md)
calls for a repository verification after every reclaim run rather than once
at the end.

### A mounted searchable snapshot hides it longest

A searchable snapshot index reads its data straight out of the repository, so
deleting the wrong object there breaks a live index rather than a backup. It does
not break it now.

Three different failures were measured here, and merging them would blur what
each one actually shows, so they stay apart.

**Snapshot removed from the catalog while an index is mounted on it.** A
captured transcript has the whole sequence. The delete returns `acknowledged: true` and HTTP 200. The
mounted index answers 3,500 documents with zero failed shards. `_cat/indices`
reports it green. The repository at that point holds two objects, `index-7` and
`index.latest`. Clearing the shared cache returns success and the index still
answers 3,500 documents. It goes red on a forced shard reallocation, with
`ALLOCATION_FAILED` and a `RecoveryFailedException`. On a real cluster that
reallocation is a node restart days later.

**Data blobs deleted while the snapshot stays in the catalog.** The harder
version, also measured: with eight data blobs removed, both mount types return
HTTP 200 and their document counts, and closing and reopening the index
detects nothing on either mount type. Both return to green with no error in
the cluster log. The failure surfaces on the first document fetch that misses
cache, as a 404 against the object store.

The S3 rig produced the same shape at a different scale. A green partial mount
answering 600 documents, whose backing snapshot Elasticsearch had removed from
the catalog without complaint, kept five backing blobs unreachable from
`index.latest`. All five classify ORPHAN, and that classification is not wrong.
Deleting them left the index green, still answering 600 documents, still green
after the shared cache was cleared. It went red at the next close and reopen.

The window in which this damage is visible is zero. No exit code, no
post-sweep verification and no `_verify_integrity` run catches it, because
`_verify_integrity` walks the catalog and the catalog is exactly where the
snapshot no longer appears. Nothing printed after the delete can help. That is
why an unanswered mount question refuses an `--execute` run rather than warning
through it.

## Sharing is also the best defence

A segment becomes garbage for a reason. It becomes garbage because the last
snapshot referencing it was deleted. On a repository that leaks, which is the
only kind a reclaim tool is pointed at, that deleted snapshot's own shard document
is still sitting in the shard directory, and it still names the blob.

The leak is easy to watch happen. The rig's MinIO rejects the S3 batch delete
with the same `Missing required header for this request: Content-Md5` that
started this project, so `DELETE _snapshot/br-share-repo/br-snap-N` returns
`{"acknowledged":true}` and Elasticsearch's catalog empties out while the bucket
keeps everything. Deleting all four snapshots took the bucket from 27 objects to
34, because every data blob, `snap-`, `meta-` and shard `index-` stayed put
and four new root generations were written on top.

So an orphan has a paper trail. Some `snap-<uuid>.dat` in its own shard directory
should name it, whether that snapshot is live or not. An orphan that no snapshot
document in its own shard directory references was never referenced by anything,
and that is not a state Elasticsearch can produce. This is the rule
`generation_chain` builds its condemnation on directly, rather than as a
cross-check applied after the fact: a blob is named only if some deleted
snapshot's own shard document names it.

That single rule is what defeats format drift, and it works because of the
sharing rather than in spite of it. A format change rewrites both stored copies
of a shard's file list from the same in-memory list, so the two copies agree with
each other and every consistency check between them stays silent. What the change
cannot do is invent history. If the file list shrank because a decoder misread
it, the blobs that dropped out are still named by the snapshot documents that
were written before the drift.

Two measurements on the rig separate the cases that no ratio can:

| Case | Segments condemned | Named by some snapshot document |
|---|---|---|
| Healthy sweep after a forcemerge, old snapshot deleted | 16 of 18 in the shard, 89% | 16 of 16 |
| Injected drift that halved both stored copies | 6 | 0 of 6 |

Any threshold on the orphan ratio puts the legitimate case above the attack. The
explanation rule puts them at opposite ends.

## What changed in how this is guarded

The retired tools carried a long list of guards on top of their LIVE/ORPHAN/
PROTECTED classification: a shape gate that refused to trust a decode that
succeeded but returned nonsense, a rule that a condemned segment had to be
named by some snapshot document or the whole shard was protected, a rule that
a shard condemning all of its own segments was not believed, structural
checks that a segment could produce the files it claimed, an abort if the
root pointer was stale, mount-awareness read from the cluster, a
corroboration pass against Elasticsearch, a volume cap, and a workflow that
made a human type the delete count at a terminal. Their flags and internals
went with them; this section used to describe those flags, and no longer
does.

`generation_chain` does not carry an equivalent guard for each one, because
its algorithm removes the failure mode most of them existed to catch. It
never builds a LIVE/ORPHAN/PROTECTED classification to begin with: it
computes Elasticsearch's own shard-local set difference and condemns a blob
only when some deleted snapshot's own shard document names it (the rule in
[Sharing is also the best defence](#sharing-is-also-the-best-defence) above,
built into the algorithm rather than checked afterward), a read that fails
shrinks the manifest instead of guessing (see
[the safety condition](../FACTS.md#the-safety-condition-stated-correctly) in
FACTS.md), and its audit half has no delete path at all. Two properties of
the retired guards do carry forward, in different shape: mount-awareness is
now `--elasticsearch`/`--es-repository` corroboration, read from the cluster
and applied automatically rather than as an opt-in file; and approval is
still a ceiling rather than a floor, now enforced by matching a manifest's
sha256 digest and row count (`--approve-digest`, `--approve-rows`) rather
than by re-reading the root pointer, so an edited or stale manifest cannot
execute.

## What none of this covers

A document that lists only defences reads like marketing. These two are the
reason to believe the rest.

### Issue #1: consistent drift, and how much of it is still invisible

[Issue #1](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/issues/1)
is open, and this section's answer changed since it was first written.

A shard's file list lives in two blobs Elasticsearch keeps in sync: the current
`index-<gen>` and the per-snapshot `snap-<uuid>.dat`. Drop the same live segment
from both, keep `segments_N`, and patch the file count and total size to match.
Every guard that works by comparing those two copies to each other stays
silent, because the two copies agree, and Elasticsearch's own corroboration
stays silent too, because it reads the same blobs. This is not only an
attacker scenario: a genuine upstream format change writes both copies from
one in-memory list and produces exactly the same invisible drift. That is the
version which reaches a user who simply upgrades.

This document used to say the fix, an independent oracle reading `segments_N`
(Lucene's own commit point, stored inline and never round-tripped through
either of the two copies above), had been priced twice and declined both
times. `generation_chain` built it anyway:
[`formats/lucene_segments.py`](../generation_chain/formats/lucene_segments.py)
decodes the commit point, and the audit's report line
`Lucene commit cross-check (issue #1)` runs it against every snapshot file
list on every audit (see [the sample report](../README.md#what-the-output-looks-like)
in the README). A file list that under-references what the commit point
requires is now drift this reader detects with no cluster contact, closing
the realistic case: an upstream format change nobody staged, where the two
copies and the commit point are written by different code for different
reasons and a change to one does not silently move the other.

**State the limit precisely, because it is narrower than "closed" and wider
than "open."** Against the realistic case above, this closes the gap. Against
an adversary who already has the object-store write access needed to alter
the two shard-document copies, the same adversary could alter `segments_N`
too; reaching this state needs that write access regardless. A related, still
open corner: `generation_chain` also cross-references the root catalog
against each snapshot's own declared extent
([`formats/snapshot_document.py`](../generation_chain/formats/snapshot_document.py)),
which catches a short list or a missing entry, but by its own documentation
does not defend against a tamper that adjusts the catalog and the snapshot
document consistently, so issue #1 is exactly as open as it was for that
narrower, coordinated case.

One further gap travels with the same issue. Elasticsearch corroboration
(`--elasticsearch`) runs once, when the manifest is derived. It is not
repeated at delete time; what runs there instead is a manifest-age check
(`generation_chain.reclaim`'s staleness check, an hour by default) that
refuses a manifest old enough that the cluster could plausibly have mounted a
searchable snapshot since. That catches a repository that moved. It would not
catch metadata that was changed for the audit and put back before the delete.

So the honest statement, updated: this tool now detects format drift that
removes a whole segment consistently from both stored copies, for the
realistic case of an unstaged upstream change, and it does not close the
narrower case of a coordinated tamper by an adversary who already holds
object-store write access. If you are upgrading Elasticsearch across a major
version, the commit-point cross-check runs automatically; verifying a real
restore afterward is still the check that means the most.

### There is no recovery path through the Amazon S3 Compatibility API

This question is settled, and the answer is worse than the runbooks used to
imply.

Oracle's
[supported operations list](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/s3compatibleapi_topic-Amazon_S3_Compatibility_API_Support.htm)
enumerates what the Amazon S3 Compatibility API accepts. The bucket operations are
`DeleteBucket`, `GetLocation`, `HeadBucket`, `GetService`, `ListObjects` and
`PutBucket`. The object operations are `BulkDelete`, `DeleteObject`, `GetObject`,
`HeadObject`, `PutObject` and `RestoreObjects`.

`ListObjectVersions` is not on it. Neither is `GetBucketVersioning`, nor
`PutBucketVersioning`. None of those three strings appears anywhere on that page.

The page does say "Amazon S3 Compatibility API supports version ids", and that
one sentence is the whole of what it says about versioning. It names no operation
that accepts a `versionId`, and it is easy to misread as recovery being
available. A web search returns that misreading, because the Object Storage
API's `ListObjectVersions` gets conflated with the compatibility surface.

**Accepting version ids is useless without being able to list them.** Recovery
means finding the version that existed before a delete. The only operation that
enumerates versions is absent. An operator restricted to this endpoint cannot
discover the id to pass, cannot confirm versioning is enabled, and cannot enable
it. Whichever operations do accept a version id, the page does not name them,
and it makes no difference: the id has to come from somewhere first.

Any tool working over this endpoint has to say "cannot confirm" rather than
"versioning is off," because `GetBucketVersioning` is not there to answer and
an absent answer is not the same as a negative one. Object versions still
exist on an Object Storage bucket with versioning enabled and are still
restorable through the Object Storage API and the Console. Only the S3
surface cannot see them.

**State the real answer plainly: the recovery path is a backup held somewhere
else.** Not versioning, not a delete marker, not the console. If the data matters
and the sweep is wrong, what gets it back is a copy that was never in that
bucket.

**Replication is not that copy, and it is the first thing a competent operator
reaches for.** Oracle's replication policies carry the delete across: "Objects
deleted from the source bucket after policy creation are automatically deleted
from the destination bucket." A wrong sweep replicates faithfully, and you end up
holding two copies of the damage. The feature answers a regional outage rather
than a mistake, and its other limits agree: one policy per source bucket, a
read-only destination that cannot itself be a source, no chaining, and nothing at
all for objects uploaded before the policy existed. Retention rules are no better,
because they are mutually exclusive with versioning and they block modification,
and Elasticsearch overwrites `index.latest` and `index-<gen>` on every commit.
What is left is an independent copy, and that is a job rather than a setting: a
scheduled copy into a separate bucket, better still a separate tenancy, made by
something that does not propagate deletes.

That is less alarming than it sounds for one specific configuration, and it is
worth knowing which one you are in. This project's owner moved backups to shared
storage and left the frozen tier on object storage. That decision does most of
the work, because a wrong delete's blast radius becomes a tier that can be
rebuilt from the live cluster, and the data whose loss would be unrecoverable is
no longer in the bucket the reclaim tool touches. An operator who has not made
that move is in a materially different position. Work out which one you are
before the first `--execute`, not after.

## Before you run a delete

Short, and none of it is optional on a repository you care about.

1. Know whether your bucket holds the only copy of anything. If it does, and you
   reach it through the Amazon S3 Compatibility API, you have no recovery path.
   Read [the recovery path finding](#there-is-no-recovery-path-through-the-amazon-s3-compatibility-api)
   again.
2. Pass `--elasticsearch` and `--es-repository` to the audit. Its absence is not
   detected, and the failure it prevents has a detection window of zero.
3. Run the dry run and read the manifest by kind, not by percentage. A small byte
   share is not a small risk.
4. Pass `--elasticsearch`. It is optional, and without it nothing corroborates the
   manifest against the cluster.
5. Keep the manifest you approved. Recovering an object needs its key.
6. Verify a real restore afterwards, with `verify_restorable.py`.
   `POST _snapshot/<repo>/_verify` fails on these endpoints with the original
   Content-Md5 error, before and after any reclaim run, so it proves nothing
   here. `_verify_integrity` catches a lot, reads only the live catalog, and
   misses the mounted case entirely. A restore is the check that means
   something.

These six questions are about the repository rather than about any tool, and
`generation_chain` is built to answer all six: items 2 and 4 are both
`--elasticsearch`/`--es-repository`, item 3 is the disposition breakdown in
[its report](../README.md#what-the-output-looks-like), item 5 is the manifest
`--approve-digest` and `--approve-rows` bind to, and item 6 is
`verify_restorable.py`, shipped alongside it for exactly this step.

## Sources

Elasticsearch source, at tag `v9.5.2`, which is the version the rig runs. Line
anchors were checked against that tag rather than against `main`.

- [`BlobStoreRepository.java`](https://github.com/elastic/elasticsearch/blob/v9.5.2/server/src/main/java/org/elasticsearch/repositories/blobstore/BlobStoreRepository.java):
  blob name constants, `doSnapshotShard` (deduplication and blob naming),
  `finalizeSnapshot` (index metadata dedup), `writeIndexGen` and
  `maybeWriteIndexLatest` (root generation and pointer).
- [`BlobStoreIndexShardSnapshots.java`](https://github.com/elastic/elasticsearch/blob/v9.5.2/server/src/main/java/org/elasticsearch/index/snapshots/blobstore/BlobStoreIndexShardSnapshots.java):
  `findPhysicalIndexFile`, the cross-snapshot lookup that produces sharing.
- [`StoreFileMetadata.java`](https://github.com/elastic/elasticsearch/blob/v9.5.2/server/src/main/java/org/elasticsearch/index/store/StoreFileMetadata.java):
  `isSame`, the three comparison paths.
- [`Store.java`](https://github.com/elastic/elasticsearch/blob/v9.5.2/server/src/main/java/org/elasticsearch/index/store/Store.java):
  `MetadataSnapshot.isReadAsHash`, which files are stored inline.
- [`IndexMetaDataGenerations.java`](https://github.com/elastic/elasticsearch/blob/v9.5.2/server/src/main/java/org/elasticsearch/repositories/IndexMetaDataGenerations.java):
  `buildUniqueIdentifier`, the index metadata dedup key.
- [`ShardGeneration.java`](https://github.com/elastic/elasticsearch/blob/v9.5.2/server/src/main/java/org/elasticsearch/repositories/ShardGeneration.java):
  `newGeneration`, why a shard generation is a uuid.
- [`S3BlobStore.java`](https://github.com/elastic/elasticsearch/blob/v9.5.2/modules/repository-s3/src/main/java/org/elasticsearch/repositories/s3/S3BlobStore.java):
  `deleteBlobs` and `deletePartition`, the batch delete this whole project works
  around.

Elastic documentation.

- [Snapshot and restore](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore):
  how snapshots deduplicate segments, and the warning that nothing but
  Elasticsearch should modify a repository.
- [Searchable snapshots](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/searchable-snapshots):
  fully mounted and partially mounted indices, and the shared cache.
- [Verify repository integrity API](https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-snapshot-repository-verify-integrity):
  `POST /_snapshot/{repository}/_verify_integrity`. Distinct from the
  [repository analysis API](https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-snapshot-repository-analyze).
- [Restore a snapshot](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/restore-snapshot)
  and the [restore API](https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-snapshot-restore).
- [S3 repository](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/s3-repository).

Apache Lucene.

- [Index file formats](https://lucene.apache.org/core/10_4_0/core/org/apache/lucene/codecs/lucene104/package-summary.html):
  the file naming rules and the extension table, including `segments_N`, `.si`,
  `.cfs` and `.cfe`.
- [`Lucene99SegmentInfoFormat`](https://lucene.apache.org/core/10_4_0/core/org/apache/lucene/codecs/lucene99/Lucene99SegmentInfoFormat.html):
  the on-disk `.si` layout, still in the `lucene99` package in the Lucene 10
  javadoc because the format has not been revised. "Files is a list of files
  referred to by this segment."
- [`SegmentInfos`](https://lucene.apache.org/core/10_4_0/core/org/apache/lucene/index/SegmentInfos.html)
  and [`SegmentInfo`](https://lucene.apache.org/core/10_4_0/core/org/apache/lucene/index/SegmentInfo.html).

Oracle.

- [Amazon S3 Compatibility API support](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/s3compatibleapi_topic-Amazon_S3_Compatibility_API_Support.htm):
  the operations list.
- [Amazon S3 Compatibility API](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/s3compatibleapi.htm).
- [Object Storage versioning](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/usingversioning.htm):
  the Object Storage API, where `ListObjectVersions` does exist.
- [Object Storage replication](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/usingreplication.htm):
  where the deletes propagate, and the rest of the reason it is not a backup.

Measurements taken for this document. The rig is Elasticsearch 9.5.2 under ECK on
a single node, with snapshot repositories of type `s3` pointed at MinIO through
the S3 API, path style access, and a `base_path` per experiment. Every
repository, index and snapshot created for these runs was named with a `br-`
prefix and removed afterwards. Registering a repository there needs
`?verify=false`, because MinIO rejects the batch delete that repository
verification uses with the same `Content-Md5` error this project exists to work
around. The object listings and byte counts quoted above came from `mc ls` and
`mc stat` against the bucket, and the decoded shard documents came from the SMILE
(Jackson's binary JSON encoding, [format specification](https://github.com/FasterXML/smile-format-specification/blob/master/smile-specification.md))
codec now shipped read-only in [`generation_chain/formats/`](../generation_chain/formats/).

Reproduce a campaign of your own, with the object and byte breakdown by
verdict reason that a real audit report gives you, using
[Testing in your own OCI environment](testing-in-your-oci-environment.md) and
`snapshot_churn_rig.py`.
