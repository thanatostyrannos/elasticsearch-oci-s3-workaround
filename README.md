# elasticsearch-oci-s3-workaround

**Elasticsearch snapshot cleanup and migration for Oracle Cloud Infrastructure Object Storage and other S3-compatible endpoints that reject `DeleteObjects`.**

Tooling and runbooks for Elasticsearch snapshot repositories on S3-compatible
object storage (Oracle's Object Storage service, Dell ECS, Hitachi HCP and
similar) where snapshot deletion silently fails and the bucket grows forever.

Object Storage offers two APIs and Oracle names both, so this document uses
Oracle's names rather than its own. They are the names you will meet in
Oracle's documentation, and the ones that will match when you search or open a
support ticket. The **Amazon S3 Compatibility API** is the AWS S3 surface, and
it is the one that carries the bug below. The **Object Storage API** is
Oracle's own, and it is where the sweepers send their deletes.

*Not affiliated with, endorsed by, or sponsored by Elastic N.V. or Oracle
Corporation. Elasticsearch is a trademark of Elastic N.V.; Oracle and OCI are
trademarks of Oracle Corporation. Product names are used only to identify the
software this tooling interoperates with.*

## Is this your bug?

You are on Elasticsearch **8.19.17+ or 9.5.0+**, your snapshot repository is on
S3-compatible storage that is not AWS, and one or more of these is true:

- Registering or verifying the repository fails with
  `repository_verification_exception ... cannot delete test data at ...`, caused
  by **`Missing required header for this request: Content-Md5`**.
- `DELETE _snapshot/<repo>/<snapshot>` returns `acknowledged: true`, the
  snapshot leaves the catalog, and **not one byte is reclaimed**.
- SLM retention reports success while the bucket only grows. The logs repeat
  `Failed to delete blobs [ObjectIdentifier(Key=...)]` or
  `Failed to delete some blobs during snapshot delete`.
- `POST _snapshot/<repo>/_cleanup` returns `200` with `"deleted_bytes": 0` and
  frees nothing.

`PUT _snapshot/<repo>` and `POST _snapshot/<repo>/_verify` return this.
Reproduced on Elasticsearch 9.5.2, identifiers replaced with placeholders:

```json
{
  "error": {
    "type": "repository_verification_exception",
    "reason": "[<repo-name>] cannot delete test data at ",
    "caused_by": {
      "type": "i_o_exception",
      "reason": "Failed to delete blobs [ObjectIdentifier(Key=tests-<uuid>/master.dat), ...]",
      "caused_by": {
        "type": "s3_exception",
        "reason": "Missing required header for this request: Content-Md5. (Service: S3, Status Code: 400, Request ID: <request-id>)"
      }
    }
  },
  "status": 500
}
```

The wording varies, which matters if you are grepping. The innermost `type` is
`s3_exception` on some endpoints and `invalid_request_exception` on others. The
header appears as both `Content-Md5` and `Content-MD5`. On snapshot *deletion*
the error never reaches the API response at all. The delete returns
`acknowledged: true`, and the only trace is a log line:

```
[WARN ][o.e.r.b.BlobStoreRepository] [<node>] [<snapshot-name>/<snapshot-uuid>] Failed to delete some blobs during snapshot delete
java.io.IOException: Failed to delete blobs [ObjectIdentifier(Key=<base-path>/indices/<index-uuid>/<shard>/__<blob-id>), ...]
```

If that is you, keep reading. The affected version boundary, the mechanism and the upstream history are in
[The failure in detail](#the-failure-in-detail) below.

## Start here

**[Reading a leaking repository, without deleting anything](docs/quickstart-read-only.md)**
is the shortest path to a real answer: how many objects a failed delete has
stranded in your bucket, how much space they occupy, and a file naming every
one. It deletes nothing and cannot.

**[Running the test rig against your own cluster](docs/quickstart-test-rig.md)**
is next, if you want to watch the whole thing work on a repository you can
afford to lose before pointing it at one you cannot. It covers standing up a
load generator that manufactures a leaking repository on purpose, and
`scripts/run-test-cycle.sh`, which drives the audit-and-reclaim loop from a
config file and checks the things that otherwise fail confusingly later.

## The fix

Three things work. One of them was always the better answer, and one of them
only came back recently.

Keep the repository in service with `?verify=false`, which is the first step
below and takes a minute. Then move the backups off the broken delete path onto
a filesystem repository, where retention is an ordinary unlink and no tooling
sits in the loop at all. That is the split-repo migration, and it is the fix
rather than the mitigation.

The third answer is reclaiming what already leaked, and it is available again.
Two published runbooks used to do that, one over each API, and both drove a
sweeper retired for deciding what to delete by absence from a set it computed
itself. Do not follow those runbooks. What replaced them derives the same answer
the way Elasticsearch derives it, condemning a blob on its presence in a deleted
snapshot's file list rather than on its absence from a live one, and it splits
the work in two: an audit that cannot delete, and a separate tool that deletes
only what a human approved. See [Using it](#using-it).

Which credential you hold still decides what you can reach, so settle that
first. The two APIs take different credentials, and holding one gets you
nothing on the other:

- The **Object Storage API** takes an API signing key, the RSA key pair named
  by `~/.oci/config`. A working `oci` CLI already has one.
- The **Amazon S3 Compatibility API** takes a **Customer Secret Key**, which
  consists of an Access Key/Secret Key pair. Nothing reads `~/.oci/config` for
  it and a working `oci` CLI proves nothing about it. You generate it in the
  Console under Identity, your user, Customer Secret Keys, and you present it
  the way every S3 client expects: `AWS_ACCESS_KEY_ID` and
  `AWS_SECRET_ACCESS_KEY`, or a profile in `~/.aws/credentials`.

| What you can do | The API it needs | Where your backups end up |
|---|---|---|
| Keep the repository registered and serving, with `?verify=false` | neither; this is an Elasticsearch call | Wherever they are now. Deletes still fail silently. |
| [Find what leaked](#using-it), with `generation_chain` | Amazon S3 Compatibility API | Unchanged. It reads and writes a manifest; it cannot delete. |
| [Reclaim what leaked](#step-two-delete-once-you-have-read-the-manifest), with `generation_chain.reclaim` | Amazon S3 Compatibility API | Unchanged, and the bucket stops growing. Deletes only what you approved. |
| [Move backups to shared storage](https://gist.github.com/thanatostyrannos/cb7ccafece8d74be125edc9b7fa77f14), the split-repo migration, minus its two cleanup steps | Object Storage API for the audit calls | **A filesystem repository.** Elasticsearch reclaims space on its own again. |
| Size what is there, with `snapshot_sizes.py` | neither; it reads Elasticsearch | Unchanged. It is read-only. |

**There is no undo in this bucket, which is why deleting is gated behind a
manifest a human reads and an approval bound to that exact file.** Oracle's supported-operations list for the
Amazon S3 Compatibility API carries no `ListObjectVersions`, no
`GetBucketVersioning` and no `PutBucketVersioning`, so a reader who can only
reach that API cannot turn versioning on, cannot confirm it is on, and cannot
discover a version id to ask for. Bucket versioning does protect objects on an
Object Storage bucket, and Oracle's own API and the Console can see those
versions. The S3 surface cannot, and a version id nobody can discover is not a
recovery path. See
[Blast radius](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/docs/blast-radius.md#there-is-no-recovery-path-through-the-amazon-s3-compatibility-api).

The sweepers aim at orphans, meaning blobs no live snapshot references, and a
backup that classifies LIVE is never touched. Be precise about what that
guarantee rests on, because it is not absolute. Both reachability sweepers
decide what is LIVE by reimplementing Elasticsearch's on disk format. A decode
that *fails* degrades the affected scope to PROTECTED, and nothing is deleted. A
decode that *succeeds and returns a wrong file list* is the expensive case: one
renamed field in an Elasticsearch upgrade deleted 96.4% of a repository in the
test lab (henceforth **the rig**: Elasticsearch 9.5.2 under ECK in Rancher
Desktop against a MinIO pinned to the last release that reproduces this fault) by
bytes. Five guards stood in that path in the retired sweepers. They caught a
decoded document that is not a file list, a whole shard condemned
at once, a stale root pointer, and a shard file entry count that disagrees with
Elasticsearch. What they did not catch is a wrong file list that keeps the count
right. Every one of those guards also had an off-switch, and each switch removed
the guard next to it. None of them was a tuning knob.

That gap is the retirement in one paragraph. The guards defended the decode. The
decision underneath them was a set difference over a live set the tool computed
by reading the whole repository, so a read that failed and a document that would
not parse both landed on the same side of it, which is the side that deletes.
The answer is not a sixth guard. It is to compute the difference the way
Elasticsearch computes it, inside a single shard directory, and to have nothing
to delete with.

Leaving backups on a repository whose deletes fail carries real risks, and they
compound:

- **Storage grows monotonically between sweeps.** Retention reclaims nothing on
  its own, so cost has no natural ceiling.
- **Deletion stops meaning destruction.** A snapshot leaves the catalog while
  its data stays in the bucket. If anyone relies on deletion for records
  retention, data minimisation or spillage remediation, that guarantee is gone.
- **Your monitoring lies.** SLM reports success. Dashboards and alerts built on
  it are green while nothing is reclaimed, and the ambient WARN noise trains
  people to ignore the log lines that would carry the next real failure.
- **You depend on the tooling indefinitely.** If the cron breaks or drifts
  behind an Elasticsearch format change, growth resumes silently.
- **Nothing here reclaims the space any more.** The tools that did are retired,
  so between now and the replacement the only lever on growth is how much
  deletion traffic the repository sees. That is what the migration below is
  for: it moves the traffic somewhere deletes work.

Move the backups and all of that ends for them. Retention becomes filesystem
unlinks, deletion means destruction again, and no tooling sits in the loop. The
risks persist only for whatever stays behind, which is usually the frozen tier,
at far lower volume than daily backups.

There is no upstream fix to wait for. Elastic declined one and considers this
the storage vendor's problem.

The first thing to do, and the one thing to do now if you read nothing else:
re-register the repository with verification skipped,
settings otherwise unchanged.

```text
PUT /_snapshot/<your_repository_name>?verify=false
{
  "type": "s3",
  "settings": {
    <your existing repository settings>
  }
}
```

That restores registration, snapshots, mounting, reads and restore. It does
**not** make deletes work, and it is not optional: without it you cannot
register the repository at all. Full breakdown of what it fixes and what it
does not is in
[the detail below](#keeping-the-repository-operational-in-detail).

**It applies to that one request and nothing afterwards, and nothing records
that you used it.** `verify` is a query parameter on the `PUT`, not a
repository setting. There is no field for it on `RepositoryMetadata`, so it is
not stored, and there is no status to read back. Measured on a repository
registered exactly this way, both `GET /_snapshot/<repo>` and the repository's
entry in cluster state return only `type`, `uuid` and your settings:

```json
{"my-repo": {"type": "s3", "uuid": "...",
  "settings": {"bucket": "...", "client": "...", "base_path": "..."}}}
```

Nothing there says verification was skipped. `POST /_snapshot/<repo>/_verify`
does not help either, because it is an action rather than a status: it runs
verification, which on an affected store fails and leaks the test blobs it
just wrote.

Three consequences an operator actually meets:

- **Re-register and you must pass it again.** `PUT` without `?verify=false`
  fails and rolls back, so the repository disappears. Anything that
  re-registers counts, including changing a non-dynamic setting such as
  `delete_objects_max_size`.
- **The flag is not a property of the repository, so nothing carries it across
  a cluster restart.** Automation that registers repositories at boot needs it
  written into that automation, not assumed.
- **A colleague reading the repository definition cannot tell.** The one place
  the decision is visible is wherever you wrote the `PUT`.

Treat `?verify=false` as something you re-apply, not something you set.

`<your existing repository settings>` is literal. `PUT` replaces the settings
block rather than merging into it, so `GET /_snapshot/<repo>` first and copy every
key across. Dropping `base_path` in particular repoints the repository at the
bucket root and makes every snapshot you have unreachable, with
`{"acknowledged":true}` returned either way. See
[base_path](#base_path-the-value-that-decides-what-your-repository-can-see).

`snapshot_sizes.py` reads Elasticsearch and needs no bucket credential. The
audit and the reclaim tool do read the bucket, so they need the Amazon S3
Compatibility API credential described above. All of it needs nothing but
Python 3.10 or newer.

```bash
git clone https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround
cd elasticsearch-oci-s3-workaround
python3 -m unittest discover -s tests     # no network needed
```

> **Searchable-snapshot warning that applies to everyone, not just this bug:**
> Elasticsearch does **not** block deleting a snapshot that backs a mounted
> searchable-snapshot index. On a repository with working deletes, that destroys
> the index. Here it leaves the index serving from leaked blobs. Mount clones,
> never policy snapshots. Generate the pinned set with `snapshot_sizes.py
> --emit-mounted` and treat it as the list nobody may delete from, by any
> means.

### Keeping the repository operational in detail

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

`?verify=false` is still a prerequisite for the cleanup in this repository, and
in particular for the log-driven sweeper. That sweeper's only input is the set
of keys Elasticsearch condemned, which it reads from the failed-delete WARN lines
in the Elasticsearch logs. Those lines exist only because Elasticsearch is still
*operating* the repository: running retention, attempting deletes, and failing.
A repository that cannot be registered is one Elasticsearch never attempts to
delete from. No attempts, no WARN lines, no input. So the order is:

1. `?verify=false` keeps the repository registered and in service.
2. Elasticsearch keeps attempting deletes on its normal schedule and keeps
   naming the keys it failed to delete.
3. Those keys used to be harvested and removed by a log-driven sweeper, and the
   residue it could not see was reclaimed by a periodic reachability sweep.
   Both tools are retired, so today the WARN lines are a measurement of the
   leak and nothing acts on them.

The reachability sweeper does not share that dependency. It reads the
repository's own metadata and needs no logs. It does need the repository to stay
reachable and readable, which is the same thing `?verify=false` preserves.

#### Do not lower `delete_objects_max_size` to get more keys logged

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

## Using it

> [!WARNING]
> **Beta.** Nobody has run this against a real Oracle Object Storage bucket,
> because no OCI endpoint is reachable from this project's lab. Everything here
> was measured against MinIO pinned to a release that rejects the same call, and
> against a real Elasticsearch 9.5.2.
>
> Treat your first run as an experiment. Use the dry run, read the manifest, and
> restore something afterwards to check the repository still works.
>
> The audit reads and cannot delete. The reclaim tool does delete, and an object
> store without versioning does not give it back.

### What you need

One directory:

```
generation_chain/     49 modules, 350 KB of Python, standard library only
```

Nothing to install, no packaging, no requirements file, no third-party import.
Checked by copying that directory alone into an empty folder and running both
the self-test and a real call against a store.

**If you cannot clone**, and in a locked-down environment you often cannot, pull
just that directory out of the release tarball:

```bash
mkdir gc && cd gc
curl -sSL https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/archive/refs/heads/main.tar.gz \
  | tar xz --strip-components=1 --wildcards '*/generation_chain'
python3 -m generation_chain --help
```

That is the whole install. It needs `curl` and `tar` and reaches GitHub once.
If even that is blocked, download the ZIP through a browser and copy
`generation_chain/` across; nothing in it cares how it arrived.

If you have a clone, copy the directory somewhere, `cd` to its parent, and run
`python3 -m generation_chain`.

You also need Python 3.10 or newer, and a credentials file you write yourself.
You do not need the tests, the evidence, the docs, `snapshot_sizes.py`, or
anything else in this repository.

### The credentials file

Write a JSON file and pass its path with `--credentials`. The tool takes a path
rather than a value because a secret in argv shows up in `ps` for every user on
the host, and it refuses a file other users can read, so `chmod 600` it.

```json
{
  "s3": {
    "access_key_id": "<<<Redacted>>>",
    "secret_access_key": "<<<Redacted>>>"
  },
  "elasticsearch": {
    "api_key": "<<<Redacted>>>"
  }
}
```

`s3.access_key_id` and `s3.secret_access_key`. On Oracle these are a
Customer Secret Key, not your console password and not an API signing key.
Create one under Identity, Users, your user, Customer Secret Keys. Oracle shows
the secret once, at creation. On AWS or MinIO they are an ordinary access key
pair.

`elasticsearch.api_key` is the `encoded` field returned by `POST
/_security/api_key`, used as `Authorization: ApiKey <value>`. Prefer this over a
password: an API key can be scoped to the privileges this tool needs, which are
read-only, and revoked on its own without touching a user account. The tool only
reads snapshot and index metadata; it never writes to the cluster.

`elasticsearch.username` and `elasticsearch.password` are the alternative, sent
as HTTP basic auth. Use one form or the other, not both.

```json
{
  "s3": {"access_key_id": "...", "secret_access_key": "..."},
  "elasticsearch": {"username": "elastic", "password": "..."}
}
```

The `elasticsearch` section is only needed when you pass `--elasticsearch`. The
`s3` section is needed for the `s3` transport, and for the `local` transport
neither is.

`oci` covers Oracle's native API rather than its S3 compatibility layer. It takes
`tenancy`, `user` and `fingerprint`, which are the OCIDs and fingerprint from
your `~/.oci/config`, plus `key_file` pointing at the private key PEM that
matches the fingerprint, and `pass_phrase` if that key has one.

```json
{
  "oci": {
    "tenancy": "<<<Redacted>>>",
    "user": "<<<Redacted>>>",
    "fingerprint": "<<<Redacted>>>",
    "key_file": "/home/you/.oci/oci_api_key.pem",
    "pass_phrase": null
  },
  "elasticsearch": {
    "api_key": "<<<Redacted>>>"
  }
}
```

Without `--credentials` the tool falls back to the standard locations,
`~/.aws/credentials` and `~/.oci/config`, and then to the environment.

### Step one: find out what is orphaned

```bash
python3 -m generation_chain \
  --transport s3 \
  --endpoint https://<namespace>.compat.objectstorage.<region>.oraclecloud.com \
  --region <region> \
  --bucket <bucket> \
  --prefix <the repository's base_path> \
  --credentials creds.json \
  --elasticsearch https://<cluster>:9200 --es-repository <repo-name> \
  --manifest orphans.tsv
```

Take `--prefix` from `GET _snapshot/<repo>` and use exactly the `base_path` it
reports. An empty `base_path` means the repository is the whole bucket.

Pass `--elasticsearch` and `--es-repository` every time. The CLI treats them as
optional, but which snapshots have searchable-snapshot indices mounted on them
lives only in cluster state, and nothing stops you deleting one an index depends
on. The cluster can only ever remove keys from the manifest, never add one.

To check the tool works before pointing it at anything, run it offline with no
store and no cluster:

```bash
python3 -m generation_chain --self-test
```

### What the output looks like

The report goes to stderr and the manifest to stdout, so they can be redirected
apart. This is a real run against a lab repository of 191,773 objects that was
being written to throughout, trimmed only where the output repeats itself.

```
transport: s3, S3 compatibility API at http://localhost:9000, bucket scalerig-snaps, prefix scalerig/, region us-east-1
repository uuid: 4O-dDdUiTHO3KtsKXLIhQw
  The uuid is a field whoever wrote the blob controls. It separates tenants sharing a bucket. It is not proof of authorship.

Coverage
  current root generation: 632
  generations read and believed: 0, 1, 2, 3, ... 630, 631, 632
  generations missing from the chain: (none)
  history this run can explain: 0%
    delete operations whose file lists it attributed in full: 0 of 392 found in the chain
    generation transitions it could read both ends of: 632 of 632
  shard directories read: 0 of 144
    indices/-jgUuDeaRBuSCjNkAWRTjQ/0 was dropped whole: snapshot 'scalerig-snap-20260826-030200-ief' declares 54 shard(s) in total and this run read 42
    indices/1IHYeDksR7SyRRh9ca_-LA/0 was dropped whole: the store holds the shard document of live snapshot(s) jatdwUP7Sq-Bd3M5dnrYqg here and the current file list does not name them
    ... 142 more, each naming the shard directory and why it was dropped
  shard directories of indices no live snapshot references: 4193
    Their blobs are reported as unexplained rather than condemned, because this run established no live set there.
  Lucene commit cross-check (issue #1): ran on 654 of 654 snapshot file lists

  Blobs orphaned by the operations above do NOT appear in the manifest. A key absent from it is not evidence that the key is live.

Elasticsearch corroboration: CHECKED against http://localhost:9200
  Everything it reported was removed from the manifest. What it did not report was not thereby condemned.

Dispositions
  orphaned: 30029
  protected: 0
  live: 948
  evidence: 39608
  unexplained: 121182
  outside-model: 6

Reclaimable
  51.97 MB across 30029 orphaned objects (51,972,892 bytes)
  Stored object size, as the store reported it in the listing. That is what a delete gives back.

Notes
  Elasticsearch at http://localhost:9200 reported 11 snapshot(s) and 5 mounted searchable-snapshot index(es); 0 key(s) left the manifest because it protects them
```

And the manifest on stdout, one row per condemned key:

```
key	reason	category	snapshot_uuid	snapshot_name	from_generation	to_generation
scalerig/indices/24P0ZEhjQC2SnrEb5K7bLw/0/snap-Kx8vQ.dat	left behind by deletion of snapshot ...	shard snapshot document	Kx8vQ...	scalerig-snap-20260826-024430-lcy	488	489
```

**That run condemned no segment blobs at all, and the reason is the point.** Every
shard directory was dropped, because the repository was being written to while it
was read: one snapshot declared 54 shards and the run had read 42. A partial view
of a shard directory is exactly the condition under which a set difference invents
orphans that are in fact live, so the segment path stopped rather than guess.
Everything in that manifest is metadata left by snapshots that were genuinely
deleted.

That is what the safety property looks like from the outside. A read that comes
back short produces a **smaller** manifest, never a wrong one. The price is
visible in the same report rather than hidden: `history this run can explain: 0%`.
A run against a quieter repository explains more and condemns more.

### What the six dispositions mean

Every key in the store gets exactly one, and only one of them is a list of
things to delete.

| Disposition | What it means | Delete it? |
|---|---|---|
| `orphaned` | A deleted snapshot's own file list named it, and this run could attribute it to a delete operation it actually observed. **This is the manifest.** | Yes, this is what the tool is for |
| `live` | A surviving snapshot references it, or it is the current root generation, the current shard generation document, or `index.latest`. | Never |
| `evidence` | A superseded root generation or shard generation document. Elasticsearch's own delete removes these. This tool never will, because its derivation reads them to learn what each delete removed. | No, and see the note below |
| `unexplained` | This run could not attribute it either way from what it managed to read. | No |
| `protected` | A guard held it back, usually the Elasticsearch veto reporting a mounted searchable snapshot. | No |
| `outside-model` | Not a shape this tool models at all, such as a co-tenant's object sharing the bucket. | No |

**A large `unexplained` count is not a backlog.** It is the honest answer when
the run could not see enough to decide, and it has several distinct causes that
the manifest's `reason` column tells apart: a shard directory the run dropped, a
snapshot document no readable generation names, index metadata with no live set
established, or a segment whose deleting operation this run never observed. In
the example above it is 63% of the store, almost all of it a consequence of
every shard directory being dropped. None of it is a deletion candidate.

**`evidence` grows and nothing shrinks it.** Elasticsearch reclaims superseded
generations as part of a snapshot delete, so against a store with this fault
they survive like everything else, and this tool will not name them because it
reads them. They accumulate for as long as the fault goes unfixed, and because
the audit reads one shard document per shard directory per generation, each pass
costs a little more than the last. That is
[issue #9](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/issues/9),
and it is the one number here that gets worse on its own.

The counts sum to every key the listing returned. In the example: 30,029 plus
948 plus 39,608 plus 121,182 plus 0 plus 6 is 191,773.

### Reading the result

Read the coverage report as well as the manifest. A short manifest means either
that there is little to clean up or that the run could not see most of the
repository, and those look the same without the coverage numbers.

A key missing from the manifest is not evidence that the key is live.

The exit codes matter if you schedule this. `0` wrote a manifest. `2` refused
for a settled reason, so retrying changes nothing. `3` means the invocation or a
credential is wrong. `4` means the store or cluster did not answer, and a retry
is reasonable. `5` means the repository is larger than the host can hold.

You can audit a repository while it is being written to. Every gate that notices
a document moving underneath it drops that shard, so a busy repository yields
fewer keys rather than different ones, and whatever it misses gets picked up on
the next run.

### Step two: delete, once you have read the manifest

Deleting is a separate tool from the audit, on purpose. Nothing you run in step
one can remove an object.

The dry run is the default. It reads the manifest, builds the requests it would
send, prints them along with the approval you would need, and sends nothing:

```bash
python3 -m generation_chain.reclaim \
  --manifest orphans.tsv \
  --endpoint https://... --region <region> --bucket <bucket> \
  --prefix <base_path> --credentials creds.json
```

It prints what it would send, and the approval that would authorise it. From the
same run as the report above:

```
manifest: orphans.tsv
  30029 key(s), sha256 fbd3088a640e300a34f153b015f57d99d63663076a1ae613213c9c2f3790199b
  31 batch(es) of up to 1000, checksum algorithm md5
  target: https://<endpoint>/<bucket>/<base_path>
DRY RUN. Nothing was sent. The first batch's request:
  POST ...?delete, 100540 byte body, 1000 key(s)
  Content-MD5: WRpAzVB46p2s8MmlS4mccg==
To execute against this exact manifest:
  --execute --approve-digest fbd3088a640e300a34f153b015f57d99d63663076a1ae613213c9c2f3790199b --approve-rows 30029
```

Two things to check against the audit before going further. The key count should
match what the report's `Reclaimable` line said, and the `Content-MD5` header is
the reason this works at all: it is the header the store demands and the one
Elasticsearch does not send.

The dry run does not yet report a size. The audit does, and that figure is the
one to read for how much this will free.

Read the manifest before going further. Every row carries the key, why it was
named, and which snapshot's deletion orphaned it, so you can check a row rather
than trust a count.

> [!CAUTION]
> **The command below deletes objects from your bucket. They do not come back.**
>
> Object storage has no undo. Unless the bucket has versioning switched on, and
> had it on when the object was written, a deleted object is gone. Restoring it
> means restoring the whole repository from somewhere else, and if this is your
> snapshot repository then there may be no somewhere else.
>
> Run the dry run above first and read what it prints. If the manifest is
> wrong, this is the step where that becomes permanent.

```bash
python3 -m generation_chain.reclaim \
  --manifest orphans.tsv \
  --endpoint https://... --region <region> --bucket <bucket> \
  --prefix <base_path> --credentials creds.json \
  --execute --approve-digest <the digest the dry run printed> \
  --approve-rows <the count the dry run printed> \
  --report deleted.jsonl
```

The approval covers that one file. An approval for one manifest will not execute
another, and editing a manifest invalidates its approval. It refuses a manifest missing its
completion marker rather than half executing it.

`--report` writes a line per batch recording which keys were requested and what
the store said about each. Keep it. It is the only record of what happened that
does not depend on the store.

`--checksum-algorithm` defaults to `md5`, which is what Oracle and MinIO
require. AWS S3 takes `crc32`. Your store decides this, not the tool.

### Making a repository that leaks, so there is something to audit

If you are evaluating this against your own cluster you need a repository that
churns: written to, snapshotted, and expired. `snapshot_churn_rig.py` builds one
and generates the load itself. It is a Python script using the standard library
only, so it needs no Kubernetes, no container and no extra install.

```bash
python3 snapshot_churn_rig.py run \
  --es https://your-cluster:9200 --user elastic --password-file espw \
  --prefix octest --repo-type s3 --bucket your-bucket --base-path octest \
  --docs-per-second 200 --snapshot-interval 30s --retention 5m --duration 2h
```

Everything it creates is namespaced under `--prefix`, and the SLM policy it
writes is scoped to its own data stream rather than `*`, so it cannot snapshot
an index it did not create. That is what makes it safe to run on a cluster
holding real data.

Full instructions, how to choose a rate, and the ILM poll-interval trap that
wastes an hour: [Generating load, without Kubernetes](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/docs/generating-load.md).

### Step three: check you did not break anything

```bash
python3 verify_restorable.py \
  --elasticsearch https://<cluster>:9200 --repository <repo> \
  --password-file /path/to/password
```

Run this after any delete. A repository can list clean, report `SUCCESS` on
every snapshot, and pass `_verify_integrity` while being unrestorable, which
this project has measured. Restoring a snapshot and counting the documents that
come back is the only check that catches it.

## The tools

Four, all Python 3.10+ and standard library only. Two are the working pair, an
audit that names what leaked and a separate tool that reclaims it. The other two
exist to measure and to check.

| Tool | Purpose |
|---|---|
| [`generation_chain/`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/generation_chain/README.md) | Reads a snapshot repository and names the objects a delete should have removed and did not. It cannot delete: its HTTP layer allows GET and HEAD and nothing else, behind an assert. Output is a manifest a person reads. See [its README](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/generation_chain/README.md) for the safety condition, the exit codes, and what it cannot see. |
| [`generation_chain/reclaim/`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/generation_chain/reclaim/) | Deletes the keys in an approved manifest, in batches, with `Content-MD5`. Dry run by default. `--execute` requires `--approve-digest` and `--approve-rows` from that dry run, so an edited manifest cannot be executed. It contains no reference to Elasticsearch: the veto is applied when the manifest is derived. |
| [`snapshot_churn_rig.py`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/snapshot_churn_rig.py) | Builds a snapshot repository that churns continuously and generates the load itself, so there is something to audit. One file, no Kubernetes. See [generating load](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/docs/generating-load.md). |
| [`verify_restorable.py`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/verify_restorable.py) | Restores an index from the repository and counts documents. The only check that survives the others passing. |
| `snapshot_sizes.py` | Per-day/week/month snapshot size report via `_snapshot/<repo>/<names>/_status` (incremental = real growth; total = restore size). Talks to ES directly, so no Kibana/LB timeouts. `--recommend` adds a sizing recommendation (baseline + retention-days x median daily growth + upgrade-day headroom, +20% margin) grounded in cited elastic.co docs; `--retention-days` accepts 5-10. `--split-frozen` separates SLM backups from frozen-tier mount snapshots; `--emit-classified` (with `--class` and `--out`) exports one row per snapshot, `--emit-mounted` exports the set of snapshots that mounted indices depend on. |

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
`RecoveryFailedException`, `_verify_integrity` clean. The check that sees that one
is `snapshot_sizes.py --emit-classified`, which reports
`0 mounted snapshot(s) MISSING-FROM-CATALOG` when the mounted set and the catalog
agree. Run both. Neither covers the other.

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
HTTP 200, `total=200`, `"failed": 0`. The rest of those measurements are in
[campaign-data.md](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/campaign-data.md).

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
a tool ends up looking at the wrong repository.** The retired sweepers took it
as `--prefix` and it defined everything they were allowed to consider. On a
bucket holding more than one repository, an empty prefix put every object in
scope. Whatever reads this bucket next inherits that problem, so it is written
down here rather than in a runbook.

## The failure in detail

The same failure is reported against NetApp StorageGRID, Hitachi Content
Platform (HCP) (reported fixed in later releases), Ceph RADOS Gateway, and MinIO
before its January 2025 fix. In testing, `RELEASE.2025-01-18T00-31-37Z` rejects
and `RELEASE.2025-01-20T14-49-07Z` accepts. AWS S3 itself is unaffected; it
treats `Content-MD5` as optional. What matters is the storage endpoint, not how
Elasticsearch is deployed: any self-managed, ECK or ECE cluster pointing a
repository at an affected store will hit this.

Since AWS SDK for Java v2.30.0 the SDK sends flexible checksums
(`x-amz-checksum-crc32`) instead of `Content-MD5`, including on checksum-required
operations like S3 Multi-Object Delete (`DeleteObjects`). Elasticsearch picked
this up when it removed a legacy signer override, which moved the SDK onto its
post-SRA path. Then the algorithms collide. The SDK defaults to CRC32. The
Amazon S3 Compatibility API accepts only `x-amz-checksum-sha256` and
`x-amz-checksum-crc32c` as alternatives to `Content-MD5`, so the out-of-the-box
default is the one algorithm such stores reject, and the whole batch comes back
HTTP 400. Measured against a real Oracle bucket: `crc32` fails, while `crc32c`,
`sha256` and `Content-MD5` all succeed against the same bucket with the same
tool. See [what Oracle's S3 Compatibility API actually does](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/oci-s3-compatibility/README.md). The same collision explains why `WHEN_REQUIRED` cannot help: that setting narrows *which operations* get a
checksum, not *which algorithm*.

The affected releases are **Elasticsearch 8.19.17+ and 9.5.0+**. Releases
8.19.0 through 8.19.16, 9.1 through 9.4, and everything predating the AWS SDK v2 migration
(including 9.0.x and 8.18.x) are unaffected, which is why an upgrade is usually
the moment the problem appears. The workaround Elastic support provides is
registering the repository with `?verify=false`. That restores registration and
snapshots. It does **not** make deletes work, so the leak continues.

No upstream fix has been offered to date. Elastic declined a proposal to expose
the S3 checksum algorithm as a repository setting, and declined a request to
document the change as breaking. Its published position is that the storage
vendor should fix this. See
[Root cause and upstream status](#root-cause-and-upstream-status) for the
detail and the sources. Check your vendor's accepted-checksum list. If it
excludes CRC32 and the vendor's published remedy is client-side (Oracle's
Amazon S3 Compatibility API documentation lists sha256 and crc32c as the
alternatives and points at `LegacyMd5Plugin` rather than announcing a
server-side change), then neither side has published a fix, and you should plan
as though the leak persists.

What this repository gives you:

1. Repository sizing that measures the damage, separating backup growth from
   frozen-tier footprint ([`skills/es-snapshot-audit`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/skills/es-snapshot-audit/SKILL.md)).
3. A validated runbook for getting off the broken path, moving backups to
   block/NFS storage while the frozen tier stays mounted where it is
   ([`skills/es-hybrid-migration`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/skills/es-hybrid-migration/SKILL.md)), minus
   its two cleanup steps, which drove a retired sweeper and are marked withdrawn
   in place.

Everything here was reproduced and validated end to end against a real
Elasticsearch 9.5.2 cluster and a fault-reproducing object store. See
[EVIDENCE.md](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/campaign-data.md) for the raw data and [METHODOLOGY.md](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/methodology.md)
for the replayable playbook.

## Root cause and upstream status

The AWS SDK v2 migration ([#126843](https://github.com/elastic/elasticsearch/pull/126843), in 8.19.0 and
9.1.0) carried a request-signer override that kept the SDK on its pre-SRA code
path, where checksum-required operations still receive `Content-MD5`. That is
why 8.19.0 through 8.19.16 and every 9.1 through 9.4 release interoperate with these stores
unchanged. [#150194](https://github.com/elastic/elasticsearch/pull/150194) removed the override. That was a reasonable change on its
own terms, since it restored the SDK's intended signing for
`PutObject`/`UploadPart`, but it also moved `DeleteObjects` onto the SRA path:
`x-amz-checksum-crc32`, no `Content-MD5`. The removal was backported to `8.19`
the same day as
[#150237](https://github.com/elastic/elasticsearch/pull/150237). By release
date users met it first in 8.19.17 (2026-06-23), a patch release, and only six
weeks later in 9.5.0 (2026-08-04).

A retroactive changelog PR seven weeks after the merge
([#153937](https://github.com/elastic/elasticsearch/pull/153937), "Add
changelog for #150194") added a one-line entry: "Update `repository-s3` to use
the default request signer from AWS SDK for Java." The candid version stayed in
that PR's own description, "Turns out that some S3-compatible storage behaves
differently with this change so it's worth mentioning in the changelog after
all", and never reached users. That entry was backported to `9.5` only, so
8.19.17 shipped the change with no release-note entry of any kind. The labels
are worth comparing too: the SDK v2 migration
([#126843](https://github.com/elastic/elasticsearch/pull/126843)) carried a `>breaking` label, while
[#150194](https://github.com/elastic/elasticsearch/pull/150194), the change that altered the wire format for these
stores, carried only `>bug`.

On an existing repository the failure is silent. Elasticsearch completes
the snapshot-delete API response *before* blob cleanup runs, and the cleanup
path catches and logs every deletion failure without rethrowing
([`BlobStoreRepository.java:1237-1243`](https://github.com/elastic/elasticsearch/blob/main/server/src/main/java/org/elasticsearch/repositories/blobstore/BlobStoreRepository.java#L1237),
[`:1581-1590`](https://github.com/elastic/elasticsearch/blob/main/server/src/main/java/org/elasticsearch/repositories/blobstore/BlobStoreRepository.java#L1581), [`:1611-1615`](https://github.com/elastic/elasticsearch/blob/main/server/src/main/java/org/elasticsearch/repositories/blobstore/BlobStoreRepository.java#L1611)). So snapshot delete,
SLM retention, and repository `_cleanup` all report success while leaving the
blobs behind. You get unbounded repository growth and WARN-level log noise. Only
repository `_analyze` and a fresh registration surface the 400 directly.
Elasticsearch has an open issue about tightening this leniency
([#100569](https://github.com/elastic/elasticsearch/issues/100569), "Less
lenience during snapshot deletion", open since 2023), referenced from a TODO in
the delete path itself.

No Elasticsearch configuration reaches it.

- `repository-s3` exposes no checksum-related setting at repository, client, or
  node level.
- The SDK's opt-out does not apply. `WHEN_REQUIRED` means checksum calculation
  happens "only when required by the API operation", and `DeleteObjects` *is*
  checksum-required (`requestChecksumRequired: true` in the SDK's S3 model;
  AWS: "The Content-MD5 request header is required for all Multi-Object Delete
  requests"). Setting `aws.requestChecksumCalculation=WHEN_REQUIRED` still
  leaves CRC32 on the delete.
- AWS's designated remedy, `LegacyMd5Plugin` (SDK ≥ 2.31.32), is a client
  plugin. Elasticsearch bundles the SDK version containing it (2.31.78) with no
  way to enable it.
- Peer projects ship this knob. aws-cli exposes `--checksum-algorithm` on
  `s3api delete-objects` itself, with `MD5` among its accepted values, the
  closest precedent there is. OpenSearch 3.3.0 added a legacy-MD5 *repository*
  setting ([opensearch-project/OpenSearch#19220](https://github.com/opensearch-project/OpenSearch/pull/19220));
  its client-settings form was unusable until 3.4.0. Hadoop S3A has
  `fs.s3a.create.checksum.algorithm`, though that governs the upload path only,
  not multi-object delete.

`Content-MD5` is optional on `DeleteObjects` per the S3 API, genuine AWS S3
accepts the SDK's current request, and a store that rejects it is the
non-conforming party. Elastic's reasoning follows from that and is largely
correct: the header is optional, AWS treats it as legacy, the SDK stopped
sending it, and a store requiring it is not fully S3-compatible. A vendor-side
fix is the clean resolution. So what follows is a request for a compatibility
accommodation with an unchanged default, an enhancement rather than a bug
report. Its argument is that Elastic's reasoning addresses only the server side
of the connection, while AWS itself ships a client-side mechanism for this case.

The proposal upstream was an optional per-repository setting. Leave it absent
and you get today's behavior, with not one byte on the wire changed:

```
checksum_algorithm: crc32c | sha256 | crc32 | md5
```

`crc32c` and `sha256` pass through the official S3 request member
(`DeleteObjectsRequest.Builder#checksumAlgorithm`), and they are what Oracle
documents as the Amazon S3 Compatibility API's accepted alternatives. `md5`
restores `Content-MD5` via `LegacyMd5Plugin`, AWS's own published compatibility
mechanism. Every value is a server-verified integrity header genuine AWS
accepts on this operation, so no
setting disables a protection, unlike
`unsafely_incompatible_with_s3_conditional_writes` ([#137185](https://github.com/elastic/elasticsearch/pull/137185)). The closest
precedent is `disable_chunked_encoding`
([#44052](https://github.com/elastic/elasticsearch/pull/44052)), a compatibility pass-through accepted once no alternative was
demonstrated.

Status: declined. The proposal went to Elastic through a support case. Elastic
declined it, and declined a separate request to document the change as
breaking. It reached users on both branches without that label: as
[#150194](https://github.com/elastic/elasticsearch/pull/150194) in 9.5.0, a
minor release, and as
[#150237](https://github.com/elastic/elasticsearch/pull/150237) in 8.19.17,
a patch release. Operators upgrade minors and patches expecting working behavior to hold. On
both branches, a cluster snapshotting to an unchanged bucket stops reclaiming
space with no change on the operator's side.

That outcome is not personal to one case. It follows Elastic's published
policy on this class of report, which the S3 repository documentation states
directly: operators should ensure their storage supplier offers a full
compatibility guarantee, and should not report Elasticsearch issues involving
storage that claims S3 compatibility unless the same issue can be demonstrated
against genuine AWS S3
([docs](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/s3-repository#repository-s3-compatible-services)).
A public report of the adjacent upload-path symptom from this same SDK change
([#156269](https://github.com/elastic/elasticsearch/issues/156269), a
`Content-SHA256` mismatch on upload rather than the delete failure described
here) was closed `not_planned` eight minutes and forty-three seconds after it
was filed, citing that policy.

Elastic later issued a support knowledge-base article on the delete
failure. Without reproducing its wording, it confirms the affected version
boundary, states that no workaround exists within Elasticsearch, confirms that
`WHEN_REQUIRED`, `disable_chunked_encoding` and `always_sign_requests` are all
ineffective against this error, notes that downgrading is not viable for most
deployments, and acknowledges that the `?verify=false` workaround leaves
unreachable objects behind and inflates storage usage. It also notes that
Elasticsearch cleans those leaked objects up automatically once the storage
service accepts the deletes again. That self-healing is real, and worth
knowing: the leak is recoverable, not permanent corruption. It also depends on
a vendor-side change, and it would hold just as well after a client-side fix in
Elasticsearch. The article gives one resolution, vendor-side: reconfigure or
upgrade the storage. It does not mention
`checksumAlgorithm`, `LegacyMd5Plugin`, aws-cli's `--checksum-algorithm`, or
OpenSearch's shipped setting, which is the client-side half of the solution space.

If your storage vendor ships a fix, take it. That is the clean resolution and it
makes this repository unnecessary. If your vendor treats its checksum list as a
design decision, and Oracle documents the Amazon S3 Compatibility API's that
way and points back at client-side `LegacyMd5Plugin` as the remedy, then no fix
is coming from either
side, and the leak is permanent until you change the architecture. That is what
this repository is for.

Sources:

- AWS SDK for Java 2.x, [S3 checksums](https://docs.aws.amazon.com/sdk-for-java/latest/developer-guide/s3-checksums.html).
  The 2.30.0 behavior change and `LegacyMd5Plugin`.
- AWS, [Data Integrity Protections for Amazon S3](https://docs.aws.amazon.com/sdkref/latest/guide/feature-dataintegrity.html).
  Defines `WHEN_REQUIRED`. The consequence that matters here is stated on the
  Java SDK checksums page above: "Some S3 operations, however, require a
  checksum calculation; you cannot disable checksum calculation for these
  operations."
- AWS S3 API Reference, [DeleteObjects](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteObjects.html).
  `Content-MD5` required for Multi-Object Delete.
- Oracle, [Amazon S3 Compatibility API Support](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/s3compatibleapi_topic-Amazon_S3_Compatibility_API_Support.htm).
  The Amazon S3 Compatibility API accepts `x-amz-checksum-sha256` and
  `x-amz-checksum-crc32c` as alternatives to `Content-MD5`, and recommends
  `LegacyMd5Plugin` client-side.
- NetApp, [multi-object delete fails on StorageGRID after AWS SDK v2.30.x](https://kb.netapp.com/hybrid/SGRID/Object_Mgmt/Object_Mgmt_KBs/After_upgrading_AWS_SDK_to_v2_30_x_multi_object_delete_fails_on_StorageGRID)
- Dell, [KB 000299507, ECS: AWS CLI fails with Missing Content-MD5](https://www.dell.com/support/kbdoc/en-us/000299507/awscli-fails-with-missing-content-md5).
  The same class of failure on Dell ECS, but from AWS CLI 2.23.0+ sending
  `CRC64NVME` on bucket-config operations. Different client and operation;
  cited as corroboration of the pattern, not of the `DeleteObjects` path.
- GitLab, [19.0 upgrade notes](https://docs.gitlab.com/update/versions/gitlab_19_changes/).
  The same `DeleteObjects` CRC32 collision hitting S3-compatible backends in
  another project, with no configuration workaround available there either.
- [`LegacyMd5Plugin` javadoc](https://docs.aws.amazon.com/java/api/latest/software/amazon/awssdk/services/s3/LegacyMd5Plugin.html)
- Elastic, [S3-compatible services](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/s3-repository#repository-s3-compatible-services).
  The published policy on reports involving non-AWS S3 endpoints.
- Elasticsearch PRs and issues: [#126843](https://github.com/elastic/elasticsearch/pull/126843) (SDK v2 migration), [#150194](https://github.com/elastic/elasticsearch/pull/150194) / [#150237](https://github.com/elastic/elasticsearch/pull/150237) (signer
  override removed, the trigger), [#153937](https://github.com/elastic/elasticsearch/pull/153937) (follow-up changelog note), [#100569](https://github.com/elastic/elasticsearch/issues/100569)
  (snapshot deletion tolerates blob-deletion failures), [#44052](https://github.com/elastic/elasticsearch/pull/44052)
  (`disable_chunked_encoding` precedent), [#137185](https://github.com/elastic/elasticsearch/pull/137185) (the disanalogy),
  [#156269](https://github.com/elastic/elasticsearch/issues/156269) (adjacent
  upload-path report, closed under the S3-compatibility policy).
- OpenSearch: [opensearch-project/OpenSearch#18240](https://github.com/opensearch-project/OpenSearch/issues/18240), [#19220](https://github.com/opensearch-project/OpenSearch/pull/19220) (the shipped
  client-side setting).
- AWS SDK: [aws/aws-sdk-java-v2#5805](https://github.com/aws/aws-sdk-java-v2/issues/5805), [#6055](https://github.com/aws/aws-sdk-java-v2/pull/6055).

## Authenticating to Elasticsearch with a read-only API key

[`snapshot_sizes.py`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/snapshot_sizes.py) talks to Elasticsearch and to nothing
else. A read-only key is all it needs, and a read-only key is all it should
ever be given.

`snapshot_sizes.py` takes `--user user:password` or `--api-key <encoded>`.

One privilege needs care, and it is worth granting even though no tool here
calls it any more. `_verify_integrity` is gated on
`cluster:admin/repository/verify_integrity`, and a key holding only
`monitor_snapshot` gets a 403. That is the check the section above tells you to
run after any deletion traffic, so a key without that action cannot run it.

The obvious fix is the wrong one. Among the named privileges only `manage` and
`all` carry that action, and `manage` is not "repositories and snapshots" as it
sounds. It resolves to essentially every non-security cluster admin action, so
handing it out to run a read-only audit is a bad trade.

You do not have to. Elasticsearch accepts a raw action name where it accepts a
privilege name, which buys exactly the one action and nothing else. That is why
the key body below reads:

```text
"cluster": ["monitor_snapshot", "cluster:admin/repository/verify_integrity"]
```

Registering a repository, deleting a snapshot and restoring one are still out of
reach, which is the point. If you need those, grant the specific actions the
same way (`cluster:admin/repository/put`, `cluster:admin/snapshot/delete`,
`cluster:admin/snapshot/restore`) on a separate operator credential with a short
expiry, rather than reaching for `manage`.

### 1. Generate the key

The toolkit only reads. It never writes, mounts, deletes or restores anything in
ES, so the key below is strictly read-only: `monitor_snapshot` is snapshot and
repository *listing and detail*, `view_index_metadata` is *read-only* index
metadata. Neither confers any write, delete, mount or restore ability anywhere in
the cluster. Hand this key to operators and auditors freely. It cannot modify
the cluster or its snapshots even by accident.

In Dev Tools or curl, the least-privilege body covering all modes is:

```text
POST /_security/api_key
{
  "name": "es-snapshot-readonly",
  "expiration": "90d",
  "role_descriptors": {
    "es-snapshot-readonly": {
      "cluster": [
        "monitor_snapshot",
        "cluster:admin/repository/verify_integrity"
      ],
      "indices": [
        { "names": ["*"], "privileges": ["view_index_metadata"] }
      ]
    }
  }
}
```

`monitor_snapshot` covers `GET _snapshot/<repo>/*` and
`GET _snapshot/<repo>/<names>/_status`. `view_index_metadata` on `*` is what lets
`--split-frozen` and `--emit-mounted` read `index.store.snapshot` settings off
mounted searchable-snapshot indices. Response:

```json
{
  "id": "<<<Redacted>>>",
  "name": "es-snapshot-readonly",
  "expiration": 1791590400000,
  "api_key": "<<<Redacted>>>",
  "encoded": "<<<Redacted>>>"
}
```

In the Kibana UI: Stack Management → Security → API keys → **Create API key**. Name
it `es-snapshot-readonly`, enable the privilege-restriction toggle (labelled
*Control security privileges*, *Restrict privileges* on older versions), and paste
the `role_descriptors` object above into the role descriptors box. Optionally set
an expiry. After creation, switch the credential format dropdown to **Base64**.
That string is the `encoded` value.

### 2. Retrieve and use it

| Fact | Detail |
|---|---|
| What `--api-key` wants | The `encoded` field, verbatim. The script sends it as `Authorization: ApiKey <value>` with no transformation. |
| If you only kept `id` and `api_key` | `encoded` is just `id:api_key` in base64, so `printf '%s' "$ID:$KEY" \| base64` reproduces it. Read both from a `0600` file rather than typing them. |
| Lost the secret | It is shown once, at creation, and cannot be retrieved afterwards. `GET /_security/api_key` returns metadata only (id, name, creation, expiration, role_descriptors), never the secret. |
| Recovery | Invalidate and reissue: `DELETE /_security/api_key` with `{"ids":["<the id>"]}`, then repeat step 1. |

Don't paste the key literally onto the command line. It lands in shell history
and in `ps` output. Read it from a `0600` file or an environment variable:

```bash
export ES_API_KEY="$(cat /path/to/es-snapshot-readonly.key)"
```

### 3. Usage

```bash
# per-day size report
./snapshot_sizes.py --es https://es.example.com:9200 --repo my-repo --group day \
    --api-key "$ES_API_KEY" --ca-cert /path/to/ca.crt

# + repository sizing recommendation
./snapshot_sizes.py --es https://es.example.com:9200 --repo my-repo --group day \
    --recommend --retention-days 7 --api-key "$ES_API_KEY" --ca-cert /path/to/ca.crt

# class-aware report (slm vs frozen-pinned vs other)
./snapshot_sizes.py --es https://es.example.com:9200 --repo my-repo --group week \
    --split-frozen --api-key "$(cat /path/to/es-snapshot-readonly.key)" \
    --ca-cert /path/to/ca.crt

# snapshots pinned by mounted indices: the set nobody may delete from
# Use --out, not a > redirect: a redirect creates the file even when the command
# failed, and an empty pre-flight file is what a failed export leaves behind.
./snapshot_sizes.py --es https://es.example.com:9200 --repo my-repo \
    --emit-mounted --api-key "$ES_API_KEY" --ca-cert /path/to/ca.crt \
    --out /path/to/mounted.txt
grep -cv '^#' /path/to/mounted.txt   # 0 is a STOP unless you confirmed no mounts
```

Pass `--api-key` alone. `--user` takes precedence if both are set, so a leftover
`--user` silently shadows the key and you get a confusing 401.

### 4. Verify

These are two different things. Kibana Dev Tools inspects the key, curl
exercises it.

To inspect it in Dev Tools: the console authenticates as *your* Kibana user, not as
the key, so nothing you type there is proof the key works. What it is good for is
looking the key up. That needs `manage_api_key` or `read_security` to see keys you
don't own, `manage_own_api_key` for your own:

```text
GET /_security/api_key?name=es-snapshot-readonly
```

Check four things: the key exists, `"invalidated": false`, `expiration` is a
future epoch-millis value (absent means it never expires), and `role_descriptors`
shows exactly `monitor_snapshot` plus `view_index_metadata` on `*` and nothing
else. `name` supports a trailing wildcard, so `?name=es-snapshot-*` sweeps a fleet
of them. The secret is never returned here. This is the cheap way to catch an
expired or invalidated key before cron jobs start reporting 401s.

To exercise it with curl, or anything that can set the header, run the same
calls the toolkit makes, one per privilege:

```bash
# identity + auth type: proves the key authenticates at all
curl -s --cacert /path/to/ca.crt -H "Authorization: ApiKey $ES_API_KEY" \
  'https://es.example.com:9200/_security/_authenticate'

# proves cluster monitor_snapshot
curl -s --cacert /path/to/ca.crt -H "Authorization: ApiKey $ES_API_KEY" \
  'https://es.example.com:9200/_snapshot/my-repo/*?filter_path=snapshots.snapshot'

# proves index view_index_metadata on *
curl -s --cacert /path/to/ca.crt -H "Authorization: ApiKey $ES_API_KEY" \
  'https://es.example.com:9200/*/_settings?filter_path=*.settings.index.store.snapshot'
```

`_authenticate` needs no privilege and returns `authentication_type` plus an
`api_key` object carrying the key's `id` and `name`. Use it to confirm you are
hitting the cluster as the key you think you are, and not on some ambient
credential. The second prints the repository's snapshot names. The third prints
the mounted searchable-snapshot indices, where an empty `{}` with HTTP 200 means
the privilege is present and nothing is mounted.

| Call | Failure | Means |
|---|---|---|
| `_security/_authenticate` | 401 | the key itself is bad. Stop here, the other two will 401 too |
| `_snapshot/my-repo/*` | 403 | missing cluster `monitor_snapshot` |
| `*/_settings` | 403 | missing index `view_index_metadata` |

Failure modes:

| Status | Cause | Fix |
|---|---|---|
| 401 | Key invalidated, expired, or malformed. Commonly a raw `id:api_key` passed instead of the base64 `encoded` value, or a stray `--user` overriding `--api-key`. | Reissue per step 1; pass `encoded`; drop `--user`. |
| 403 on `_snapshot/...` | Missing cluster `monitor_snapshot`. Breaks the default report, `--recommend` and `--split-frozen`. | Add `"cluster": ["monitor_snapshot"]`. |
| 403 on `_settings` | Missing index `view_index_metadata` on `*`. Breaks `--emit-mounted`; `--split-frozen` degrades to the unsplit report and prints why. | Add the `indices` block on `["*"]`. |

Operator runbooks for these tools, as installable skills: [skills/](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/tree/main/skills).

Validation: `test-results.md` (removed with the retired sweepers; in git history before `9a149a8`) (363 unit tests, two live-rig
campaigns, and adversarial review), [METHODOLOGY.md](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/methodology.md) (the
replayable playbook; see its Known limits section for what has *not* been
validated), [EVIDENCE.md](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/evidence/campaign-data.md) (raw data, including its own open
discrepancies), and [manifests/](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/tree/main/manifests) (the test rig).

## The three tools that were removed

| Script | What it did, and why it is gone |
|---|---|
| `oci_repo_sweeper.py` | Classified every object in a repository LIVE, ORPHAN or PROTECTED by reimplementing Elasticsearch's on-disk format, then deleted the orphans over Oracle's Object Storage API. |
| `s3_repo_sweeper.py` | The same classifier, reaching the bucket over the Amazon S3 Compatibility API with SigV4, for an operator whose only credential is a Customer Secret Key. |
| `es_log_driven_sweeper.py` | Parsed the keys out of Elasticsearch's own failed-delete WARN lines and deleted exactly those, with no reachability logic of its own. |

All three condemned a blob by its absence from a live set they built themselves,
which put every failed read and every unparseable document on the deleting side
of the decision. A reviewer drove one of them, along its documented path, to a
real delete of a live segment blob. The log-driven tool started from a stronger
premise, that Elasticsearch itself named the key, but it still had to answer
whether some surviving snapshot referenced the key now, and that is the same
absence test.

`generation_chain` reproduces what Elasticsearch does when it deletes a
snapshot: a set difference inside one shard directory, between the segment blobs
present there and the ones the new shard file list names. Nothing outside that
directory takes part, so a read failure elsewhere cannot condemn anything.

That is checked against Elasticsearch's source rather than inferred. Their rule
is in [`package-info.java`](https://github.com/elastic/elasticsearch/blob/main/server/src/main/java/org/elasticsearch/repositories/blobstore/package-info.java);
the same difference is [`derivation/shards.py`](generation_chain/derivation/shards.py),
`frozenset(present_blobs - live_blobs)`, and the narrowing to blobs a observed
delete actually named is [`derivation/garbage.py`](generation_chain/derivation/garbage.py),
`named & history.collectable`. Elasticsearch also deletes the superseded shard
generation documents; this tool never names them
([`derivation/classification.py`](generation_chain/derivation/classification.py)),
because its own derivation reads them.

The direction of the test is what makes it safe. Elasticsearch condemns a blob
on its ABSENCE from the current file list. This condemns on its PRESENCE in a
deleted snapshot's file list, so what it names is a subset of what Elasticsearch
itself would collect, and a read that fails makes the manifest shorter rather
than longer. That is the whole difference from the three tools above, and it is
why a failure here costs coverage instead of data.

It also gives Elasticsearch a veto. Two facts live in cluster state and appear
nowhere in the bucket: which snapshots have searchable-snapshot indices mounted
on them, and nothing stops you deleting one that is load bearing. Pass
`--elasticsearch` and `--es-repository` and the cluster can remove keys from the
manifest. It can never add one.

The audit itself has no delete path. Its HTTP layer allows GET and HEAD and
nothing else, behind an assert, so no change to it can quietly add one.

Reclaiming is a separate tool, `generation_chain.reclaim`, run against a manifest
the audit produced and a person has read. Dry run is the default and `--execute`
needs the digest and row count that dry run printed, so an edited manifest
invalidates its own approval.

Snapshots share segment blobs, so a `__<blobid>` object is usually reachable
from more than one snapshot and its key tells you nothing about how many.
[Blast radius](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/docs/blast-radius.md) works through what that sharing costs when
a delete is wrong, and it names the tools above throughout, because it was
written while they were the tools.

