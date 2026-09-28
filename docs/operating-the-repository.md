# Operating a repository whose deletes fail

What `?verify=false` does and does not change, what to check after any
deletion traffic, and the one repository setting that decides what every
tool here can see.

## Keeping the repository operational in detail

This is the workaround Elastic support provides. Re-register the repository with
verification skipped, keeping your existing settings unchanged:

```text
PUT /_snapshot/<your_repository_name>?verify=false
{
  "type": "s3",
  "settings": {
    <your existing repository settings>
  }
}
```

`<your existing repository settings>` means every key the current registration
has, copied across. `PUT` replaces the settings block; it does not merge. Get them
with `GET /_snapshot/<repo>?filter_path=*.settings` first, and check
`_cat/snapshots/<repo>` afterwards to confirm the same snapshots are still listed.
See [base_path](#base_path-the-value-that-decides-what-your-repository-can-see).

Mechanically it skips the registration-time check only, where Elasticsearch
writes a few test blobs and then deletes them. That cleanup delete is what
returns the 400. Runtime behavior does not change, because the SDK puts the same
checksum header on every `DeleteObjects` request it builds afterwards.

What that buys you, and what it does not:

| Operation | Under `?verify=false` | Verified result |
|---|---|---|
| Register the repository | ✅ | Without it, registration returns 500 **and rolls back**, and a follow-up `GET` returns `repository_missing`. You cannot register at all. |
| Take snapshots | ✅ | `state: SUCCESS`, 0 failed shards, repeatedly. |
| Mount searchable snapshot, `shared_cache` (frozen) | ✅ | 2/2 shards, index green. |
| Mount searchable snapshot, `full_copy` (cold) | ✅ | 2/2 shards, index green. |
| Query a mounted index after clearing its cache | ✅ | 3,500/3,500 docs, 0 failed shards, aggregates float-identical to the live index, with 58 blob-store fetches recorded, proving the bytes came from the object store, not cache. |
| Restore | ✅ | 2/2 shards, 3,500 docs. |
| Delete a snapshot / SLM retention | ❌ | `acknowledged: true`, snapshot leaves the catalog, **object count went 29 → 30**. Zero blobs removed. |
| `_cleanup` | ❌ | Returns `{"deleted_bytes": 0, "deleted_blobs": 0}`, indistinguishable from a healthy "nothing to clean", while itself **adding** a blob. |
| `_analyze` | ⚠️ | The one honest diagnostic: fails loudly and names the storage as unsuitable. Also the biggest single leaker (13 objects / 4 MiB in one small run). |

Do not mistake this for a fix, and do not mistake it for a setting. We checked
the ES source, and then measured it against a live cluster: `verify` is a
registration-time argument only. It is never persisted (there is no field for
it on `RepositoryMetadata`), nothing reports it back, and it is never consulted
again by the snapshot, restore,
delete, cleanup, or mount paths. It skips the registration probe that writes
test blobs and then bulk-deletes them, and nothing else. The runtime delete path
is the same `DeleteObjects` call it always was. The flag does not make deletes
work. It removes the loud startup failure that would have *told* you they are
broken, and turns it into a silent, permanent leak.

Treat such a repository as append-only. Deleting every snapshot *and the
repository itself* still strands every blob: in testing, 58 objects remained
after Elasticsearch reported success on all of it. Deduplication is the more
expensive problem. When a failed delete orphans an index folder, a later
snapshot of that index allocates a new folder UUID, so it cannot deduplicate
against the orphaned segments and re-uploads the data in full. The leak
compounds rather than plateaus.

`?verify=false` is a prerequisite for the audit, for a plainer reason. The audit
reads the repository's own metadata: the root index, the index metadata, the
shard generation documents. It needs the repository reachable and readable, and
a repository Elasticsearch refuses to register is neither.

Keeping it registered has a second effect worth knowing about. Elasticsearch
carries on running retention, attempting the deletes, and logging a WARN line
naming the keys each failed batch could not remove. Nothing in this repository
reads those lines. They are a live measurement of the leak, useful for watching
it grow between audits and for confirming the fault is still the one described
here.

### Do not lower `delete_objects_max_size` to get more keys logged

An earlier version of this document recommended setting
`delete_objects_max_size` to 10 so that failed batches would name every key.
That was wrong. Lowering it names fewer keys, and sometimes none.

Here is what the code does. `S3BlobStore.deleteBlobs` fills a list, sends it
once it reaches `delete_objects_max_size`, then clears it. A rejected batch does
not throw: `deletePartition` catches the error, stores it, and returns. So the
loop always runs to completion, and the buffer always ends holding just the
final short batch. Only then is the accumulated exception thrown, caught one
line later, and wrapped in the message you actually see, which is built from
whatever is still in the buffer at that moment. Abridged, with the elisions
marked:

```java
partition.add(...);
if (partition.size() == bulkDeletionBatchSize) {
    deletePartition(...);      // does not throw
    partition.clear();         // every full batch is discarded
}
...
throw new IOException("Failed to delete blobs " + partition.stream().limit(10).toList(), e);
```

So the count you get is `condemned % delete_objects_max_size`, capped at ten.

| `delete_objects_max_size` | 2,500 blobs condemned | Keys named |
|---|---|---|
| 1000 (default) | remainder 500 | 10 |
| 10 | remainder 0 | 0 |
| 10 | 2,503 condemned, remainder 3 | 3 |

At ten it averages 4.5 keys per call and gives you nothing one time in ten,
while costing 100 times the DeleteObjects requests and 100 times the
per-request charges. Leave the setting alone.

Two things about that message are worth knowing whatever you set it to.

When the count divides evenly, the buffer is empty and the message is literally
`Failed to delete blobs []`. That is not a bug you are hitting, it is the
arithmetic: at the default 1000, exactly 1000 or 2000 or 3000 condemned blobs
name nothing at all.

And the keys it names are not the keys that failed. They are whatever happened
to be last in iteration order. On these stores every batch fails, so the tail is
a subset of the failures and the distinction does not bite. Anywhere the failure
is partial, the log will confidently name keys that were deleted successfully.

One practical note if you were going to change it anyway: `delete_objects_max_size`
is not a dynamic setting. It is read once when the blob store is constructed, so
changing it means re-registering the repository, which on an affected store means
another `?verify=false` round trip.

Log volume does not change either. The per-batch WARN in `S3BlobStore` only
fires when the response is HTTP 200 with per-key errors. On the HTTP 400 these
stores return, the SDK throws first, so that branch is unreachable. One WARN
comes out per failed delete call regardless of batch size.

If you need the full condemned list, TRACE on
`org.elasticsearch.repositories.blobstore.BlobStoreRepository` is the only way
to get it. That logs every blob before every delete attempt, successes
included, so enable it for one retention cycle and turn it off again. The
sweeper's module docstring has the drain procedure.

## Verify the repository after any deletion traffic, not just after the migration

`POST _snapshot/<repo>/_verify_integrity` is the check that catches a bad delete.
It matters more now than it did, not less. The deletes reaching your bucket are
Elasticsearch's own, plus a reclaim run an operator approved by hand, and this
is what catches either of them going wrong.
A snapshot taken after one reports `SUCCESS` and cannot be restored:
Elasticsearch deduplicates shard files on physical name, length and checksum, and
never checks that the blob is still in the store, so the next snapshot reuses a
reference to a blob that is gone and succeeds doing it. Nothing surfaces until
somebody attempts a restore, which on a daily SLM schedule can be weeks of green,
unrestorable backups later.

It has one blind spot, and it is the expensive one. `_verify_integrity` walks the
snapshots the repository currently lists. A snapshot deleted while a searchable
snapshot index was still mounted on it is not in that list, so the blobs that
mount needs are never inspected, and the check reports
`total_anomalies: 0, result: pass` with the index already destroyed. Measured on
the rig: all 5 backing blobs returning 404, index red with
`RecoveryFailedException`, `_verify_integrity` clean. The check that sees that
one compares two answers `_verify_integrity` never looks at side by side:
`GET /*/_settings?filter_path=*.settings.index.store.snapshot_uuid` for the
snapshots mounted indices depend on, against `GET /_snapshot/<repo>/_all` for
the snapshots the catalog still lists. Any mounted snapshot uuid missing from
the second answer is the failure `_verify_integrity` cannot see. Run both
checks. Neither covers the other.

Gate on `results.total_anomalies` and `results.result`. On Elasticsearch 9.5.2
the `results` object contains exactly `status`, `final_repository_generation`,
`total_anomalies` and `result`. It has no `snapshot_restorability` and no
`restorable_snapshot_count`, so a check written against either of those names
matches nothing and reads as a pass forever. Per index restorability entries
appear in the streamed `log` array instead, which is a different part of the
response.

Two limits belong with any sign off that quotes it. `_verify_integrity` is
repository scoped and slow on a large repository. And by default it compares blob
names and lengths without downloading contents, so it catches a missing or wrong
sized blob and not a blob whose bytes are wrong at the right length.

One strength is worth stating as precisely as the limits. The check is sensitive
to the exact bytes of a blob, not to its decoded meaning, so a blob that has been
decoded and re-encoded draws anomalies even when the content it represents is
identical. Re-encoding is what a low-effort tamper or a hand-rolled repair looks
like, and this catches it. State that as what it is: the only modification that
gets past the check is one that preserves Elasticsearch-native bytes exactly. It
is not a claim that tampering is detected.

On a repository serving a frozen tier, do not substitute a search. Clearing the
searchable snapshot cache and running a cold query passes on a mount whose blobs
have been deleted: measured against a mount with all 8 of its data blobs gone,
HTTP 200, `total=200`, `"failed": 0`.

## `base_path`: the value that decides what your repository can see

Neither the sweeper nor the runbooks work correctly if this value is wrong, and it
is the value most likely to be dropped by accident. It gets one definition, here.

`base_path` is an optional `repository-s3` setting naming the prefix inside the
bucket that the repository lives under. A repository with `base_path` of
`vA/prod` keeps everything under `vA/prod/` in the bucket: `vA/prod/index.latest`,
`vA/prod/indices/...`, and so on. A repository with no `base_path` lives at the
bucket root, which is why one bucket can hold several repositories only if each
has its own `base_path`.

Read yours:

```bash
curl -s "$ES_URL/_snapshot/<repo>?filter_path=*.settings" -H "Authorization: ApiKey $ES_API_KEY"
```

Three things follow, and each one has cost somebody their access to a backup.

**Dropping it repoints the repository.** `PUT /_snapshot/<repo>` replaces the
settings block, it does not merge into it. Re-register without `base_path` and the
repository now points at the bucket root: it lists whatever is there, your own
snapshots vanish from `_cat/snapshots`, and restores fail with `snapshot does not
exist`. Nothing is deleted and nothing raises an error. Always `GET` the settings
first and carry every key across.

**Two repositories at the same path share one identity.** They report the same
repository UUID and the same `RepositoryData`, and writing through either can
corrupt the other's. If a re-registration lands you in that state, fix the
registration before you snapshot or delete anything.

**Any tool that reads the bucket needs this value, and getting it wrong is how
a tool ends up looking at the wrong repository.** `generation_chain` takes it as
`--prefix`, and that value defines everything the run is allowed to consider. On
a bucket holding more than one repository, an empty prefix puts every object in
scope. Any other tool you point at this bucket has the same problem, which is
why the value is written down here rather than in a runbook.
