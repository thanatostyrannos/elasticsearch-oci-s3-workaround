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
[The failure in detail](docs/problem-record.md#the-failure-in-detail) below.

## What "audit" means here

This project uses **audit** for one specific thing: the read-only pass that
reconstructs, from a snapshot repository's own generation chain, which objects
no live snapshot references. It is `python3 -m generation_chain`. It reads,
writes a manifest, and cannot delete: its transport permits `GET` and `HEAD`
and raises on anything else.

It does not mean a security audit, and it does not mean audit logging. Where
those are meant, the documents say "audit records" or name the assessment.

Two programs, and the difference matters more than the word:

| | Reads | Deletes |
|---|---|---|
| the audit, `generation_chain` | yes | no, and cannot be made to |
| the delete tool, `generation_chain.reclaim` | yes | yes, past an approval matching the exact manifest |

## Start here

**[Reading a leaking repository, without deleting anything](docs/running-it.md)**
is the shortest path to a real answer: how many objects a failed delete has
stranded in your bucket, how much space they occupy, and a file naming every
one. It deletes nothing and cannot.

**[Running the test rig against your own cluster](docs/quickstart-test-rig.md)**
is next, if you want to watch the whole thing work on a repository you can
afford to lose before pointing it at one you cannot. It covers standing up a
load generator that manufactures a leaking repository on purpose, and
`scripts/run-test-cycle.sh`, which drives the audit-and-reclaim loop from a
config file and checks the things that otherwise fail confusingly later.

**[Testing in your own OCI environment](docs/testing-guide.md)**
is the full procedure against Oracle Object Storage specifically, using a
separate bucket that holds nothing you care about. It carries the exact ILM and
snapshot settings behind the published results, what they cost in bucket space
and cluster storage, an explanation of every argument the tools take, and what
a good result looks like so you can tell whether yours is one. Do this before
you point the delete path at anything you would miss.

## The fix

Three things work. One of them was always the better answer, and one of them
only came back recently.

Keep the repository in service with `?verify=false`, which is the first step
below and takes a minute. Then move the backups off the broken delete path onto
a filesystem repository, where retention is an ordinary unlink and no tooling
sits in the loop at all. That is the split-repo migration, and it is the fix
rather than the mitigation.

The third answer is reclaiming what already leaked. This repository does that
in two halves: an audit that reads and cannot delete, and a separate tool that
deletes only what a person approved. See [Using it](docs/running-it.md#using-it).

If you find an older runbook for this bug, from this project or anywhere else,
check which direction it condemns in before you run it. Deciding what to delete
by absence from a list the tool built itself puts every failed read on the
deleting side. This one decides by presence in a deleted snapshot's file list,
which is the direction Elasticsearch uses.

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
| [Find what leaked](docs/running-it.md#using-it), with `generation_chain` | Amazon S3 Compatibility API | Unchanged. It reads and writes a manifest; it cannot delete. |
| [Reclaim what leaked](docs/running-it.md#step-two-delete-once-you-have-read-the-manifest), with `generation_chain.reclaim` | Amazon S3 Compatibility API | Unchanged, and the bucket stops growing. Deletes only what you approved. |
| [Move backups to shared storage](https://gist.github.com/thanatostyrannos/cb7ccafece8d74be125edc9b7fa77f14), the split-repo migration, minus its two cleanup steps | Object Storage API for the audit calls | **A filesystem repository.** Elasticsearch reclaims space on its own again. |

**There is no undo in this bucket, which is why deleting is gated behind a
manifest a human reads and an approval bound to that exact file.** Oracle's supported-operations list for the
Amazon S3 Compatibility API carries no `ListObjectVersions`, no
`GetBucketVersioning` and no `PutBucketVersioning`, so a reader who can only
reach that API cannot turn versioning on, cannot confirm it is on, and cannot
discover a version id to ask for. Bucket versioning does protect objects on an
Object Storage bucket, and Oracle's own API and the Console can see those
versions. The S3 surface cannot, and a version id nobody can discover is not a
recovery path. See
[Blast radius](docs/blast-radius.md#there-is-no-recovery-path-through-the-amazon-s3-compatibility-api).

The sweepers aim at orphans, meaning blobs no live snapshot references, and a
backup that classifies LIVE is never touched. Be precise about what that
guarantee rests on, because it is not absolute. Both reachability sweepers
decide what is LIVE by reimplementing Elasticsearch's on disk format. A decode
that *fails* degrades the affected scope to PROTECTED, and nothing is deleted. A
decode that *succeeds and returns a wrong file list* is the expensive case: one
renamed field in an Elasticsearch upgrade deleted 96.4% of a repository in the
test lab (henceforth **the rig**: Elasticsearch 9.5.2 under ECK in Rancher
Desktop against a MinIO pinned to the last release that reproduces this fault) by
bytes. No amount of guarding the decode closes that case, because a wrong file
list that keeps the entry count right passes every check you can write against
the decode itself. The decision underneath has to change: compute the difference
the way Elasticsearch computes it, inside one shard directory, and give the
half that reads nothing to delete with.

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
- **Reclaiming is a manual loop, not a fix.** `generation_chain` finds what
  leaked and `generation_chain.reclaim` removes it, but somebody reads the
  manifest and approves it every time. The leak resumes the moment the loop
  stops. The migration below is what ends it, by moving deletion traffic
  somewhere deletes work.

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
[the detail below](docs/operating-the-repository.md#keeping-the-repository-operational-in-detail).

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
[base_path](docs/operating-the-repository.md#base_path-the-value-that-decides-what-your-repository-can-see).

The audit and the reclaim tool read the bucket, so they need the Amazon S3
Compatibility API credential described above. Passing `--elasticsearch` to
the audit needs a read-only Elasticsearch credential instead, and no bucket
credential at all. All of it needs nothing but Python 3.12 or newer.

```bash
git clone https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround
cd elasticsearch-oci-s3-workaround
python3 -m unittest discover -s tests     # no network needed
```

> **Searchable-snapshot warning that applies to everyone, not just this bug:**
> Elasticsearch does **not** block deleting a snapshot that backs a mounted
> searchable-snapshot index. On a repository with working deletes, that destroys
> the index. Here it leaves the index serving from leaked blobs. Mount clones,
> never policy snapshots. Always pass `--elasticsearch` and `--es-repository`
> to the audit: it asks the cluster which snapshots have a mounted index and
> removes every one of them from the manifest before the reclaim tool ever
> sees it. Treat that set as the list nobody may delete from, by any means.

## The tools

Four, all Python 3.12+ and standard library only. Two are the working pair, an
audit that names what leaked and a separate tool that reclaims it. The other two
exist to measure and to check.

| Tool | Purpose |
|---|---|
| [`generation_chain/`](generation_chain/) | Reads a snapshot repository and names the objects a delete should have removed and did not. It cannot delete: its HTTP layer allows GET and HEAD and nothing else, refused at the transport with a raised exception rather than an assert, because `python3 -O` strips asserts and once let a DELETE through. Output is a manifest a person reads. See [the safety condition](FACTS.md#the-safety-condition-stated-correctly) in FACTS.md and [the exit codes](docs/running-it.md#when-it-refuses) in the read-only quickstart. |
| [`generation_chain/reclaim/`](generation_chain/reclaim/) | Deletes the keys in an approved manifest, in batches, with `Content-MD5`. Dry run by default. `--execute` requires `--approve-digest` and `--approve-rows` from that dry run, so an edited manifest cannot be executed. It contains no reference to Elasticsearch: the veto is applied when the manifest is derived. |
| [`snapshot_churn_rig.py`](snapshot_churn_rig.py) | Builds a snapshot repository that churns continuously and generates the load itself, so there is something to audit. One file, no Kubernetes. See [generating load](docs/generating-load.md). |
| [`verify_restorable.py`](verify_restorable.py) | Restores an index from the repository and counts documents. The only check that survives the others passing. |
