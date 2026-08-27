---
name: es-hybrid-migration
description: Use when migrating Elasticsearch snapshot storage off a broken S3-compatible endpoint, when snapshot deletes are acknowledged but blobs never disappear because the endpoint rejects DeleteObjects over a missing Content-Md5 against the x-amz-checksum-crc32 header, when SLM retention no longer reclaims space, or when deciding where backups versus the frozen tier should live.
---

# The split-repo hybrid migration (Strategy D)

## The fault

Elasticsearch sends `x-amz-checksum-crc32` on `DeleteObjects`. Some S3-compatible
endpoints reject the batch for a missing `Content-Md5`. The snapshot delete
**reports success** and the blobs stay. Everything downstream follows from that:
leaked bytes, retention that never reclaims, a repository that only grows.

## The architecture

Split the problem by *which repository actually needs to delete*.

| Leg | Where | Why |
|---|---|---|
| Backups | a new `fs` repository on block or NFS storage | Deletes are filesystem unlinks. No checksum header is involved, so SLM retention reclaims space on its own with no tooling in the loop. This is where the fault hurt most, and the hybrid removes it rather than mitigating it. |
| Frozen tier | stays on the S3-compatible repository, re-registered with `?verify=false` | Searchable snapshots are mounted against a specific repository. Relocating them means re-uploading and re-mounting the entire tier, which is exactly the cost the frozen tier exists to avoid. Reads never issue a `DeleteObjects`, so the fault is not on the serving path. |
| The residue | audited on the S3-compatible repository, then reclaimed from a manifest a person has read | That repository still leaks on delete. Step 9 derives what a delete should have removed and did not, and removes exactly that. The scheduled log-driven drain that used to be Step 8 ran a retired tool and has no replacement. |

**State this plainly to whoever signs off: the leak is not eliminated, it is
confined and bounded, and clearing what it leaves behind is a standing operation
rather than a one-off.** The hybrid applies only to a repository whose deletion
traffic is occasional frozen-tier churn instead of a daily retention cycle, so
the residue is small and measurable. Step 9 reclaims it, from a manifest a person
reads, with the delete in a separate command from the derivation. Left alone the
residue is a storage bill rather than a data-loss risk, so if the sign-off is not
comfortable with the delete step, stopping after Step 7 is a coherent position.

---

## Before you start

- A read-only API key for the audit steps, per the *Authenticating to
  Elasticsearch with a read-only API key* section of [README.md](../../README.md).
  Export it, do not put it on the command line:
  `export ES_API_KEY="$(cat /path/to/es-snapshot-readonly.key)"`.
- A separate credential with write access, for the registration, SLM, delete and
  restore calls below. `snapshot_sizes.py` never needs it; you do.
- For Step 9, a JSON credentials file for the object store, and an
  Elasticsearch credential in the same file. Both tools take a PATH with
  `--credentials` and never a value, because a secret in argv is visible in `ps`
  to every user on the host, and a file other users can read is refused rather
  than used. `chmod 600` it. The shape is in
  [README.md](../../README.md#the-credentials-file).
- Shorthand used below:
  `ES='curl -s --cacert /path/to/ca.crt -u "$ES_ADMIN" https://es.example.com:9200'`
- Placeholders: `my-repo` is the existing S3-compatible repository, `backups-fs` is
  the new filesystem repository.
- Write every intermediate file to a scratch directory. Nothing here belongs in a
  repository.
- Run Step 0 through Step 10 **in order**. Each step exists because it is the only
  thing that can falsify a specific claim in the plan. Do not reorder them.

---

## Prerequisite: a wrong delete in this bucket has no undo

Step 9 deletes objects out of a production bucket. Read this before it, not
after the first mistake.

Earlier versions of this runbook told you to turn on bucket versioning and keep
a dated manifest, and called the pair a recovery path. On the endpoint this
project exists for, it is not one. Oracle's Amazon S3 Compatibility API does not
carry `ListObjectVersions`, so the version id of a deleted object can never be
discovered, and an id nobody can discover is an id nobody can ask for.
`GetBucketVersioning` and `PutBucketVersioning` are absent from that surface as
well, so an operator holding only a Customer Secret Key can neither turn
versioning on nor confirm that it is on. A manifest on its own tells you what
you lost. It does not get it back.

Object versions do exist on an Object Storage bucket with versioning enabled,
and they are restorable through Oracle's own Object Storage API and through the
Console. That needs a credential for that API. If the compatibility surface is
all you have, what gets the data back is a copy that was never in this bucket,
and replication is not that copy, because Oracle's policies carry the delete
across to the destination.

The operation lists, the misreadable sentence in Oracle's documentation that
this corrects, and the reason retention rules do not help either are in
[blast radius](../../docs/blast-radius.md#there-is-no-recovery-path-through-the-amazon-s3-compatibility-api).

---

## Step 0: heal broken mounts first

Nothing else in this runbook is meaningful while an index is mounted on a snapshot
the repository catalog does not contain. Those blobs were named by a snapshot that
has been deleted, which is the shape Step 9's audit condemns, and the only thing
standing between them and a delete is a veto that protects by identity rather than
by understanding. Repair the state instead of relying on the veto.

```bash
./snapshot_sizes.py --es https://es.example.com:9200 --repo my-repo \
    --api-key "$ES_API_KEY" --ca-cert /path/to/ca.crt \
    --emit-classified --out classified.tsv

# For every index whose mount snapshot reports MISSING-FROM-CATALOG:
$ES -XDELETE '/<mounted-index>'
```

**Acceptance.** Re-running `--emit-classified` prints
`0 mounted snapshot(s) MISSING-FROM-CATALOG` on stderr.

**If it fails.** Stop. Do not continue and reclaim nothing. Elasticsearch does not
block deleting a snapshot that backs a mounted searchable-snapshot index, so this
state is reachable, and the index is currently serving reads from blobs no live
snapshot references. Restore or remount the index from a snapshot that still
exists, or unmount and delete it if the data is expendable. Then re-run. See
[es-snapshot-audit](../es-snapshot-audit/SKILL.md) §5 for the full DANGER-banner
procedure.

---

## Step 1: size the fs target from the `slm`-only terms

```bash
./snapshot_sizes.py --es https://es.example.com:9200 --repo my-repo \
    --api-key "$ES_API_KEY" --ca-cert /path/to/ca.crt \
    --split-frozen --recommend --retention-days 7

./snapshot_sizes.py --es https://es.example.com:9200 --repo my-repo \
    --api-key "$ES_API_KEY" --ca-cert /path/to/ca.crt \
    --emit-classified --out classified.tsv
```

The recommendation prints four terms:

```
  baseline (largest slm snapshot total)     : ...
  + retention growth (N x median daily)     : ...
  + upgrade-day headroom (1 x baseline)     : ...
  + frozen footprint (pinned mounts)        : ...   <- NOT yours to buy
  = recommended repository capacity         : ...
```

**The sizing rule for the hybrid: use the `slm`-only terms and exclude the frozen
footprint line.** The first three size the new block or NFS volume, because that
is all the `fs` repository will hold. The fourth stays on the S3-compatible
repository, where those bytes already live and where they will remain. Buying NFS
capacity for the frozen footprint is buying the same terabytes twice. **Add the
operational margin to the three-term sum, not to the printed total.**

**Acceptance.** The classified inventory accounts for every snapshot with a class,
and every `frozen-pinned` row carries a `tier` and a `mounted_by` index. In the
reference run: 3 snapshots, `slm=1`, `frozen-pinned=1`, `other=1`, zero missing
from catalog. The three `slm` terms summed to ~1.14 MiB against a printed total of
~1.4 MiB, and the ~281 KiB difference is exactly the frozen footprint that does
not move.

**If it fails.** A row with no class, or a `frozen-pinned` row with no
`mounted_by`, means discovery did not complete. Check that the key carries
`view_index_metadata` on `*`. If `--recommend` prints the frozen-tier WARNING
instead of the `--split-frozen` NOTE, the split did not take and the baseline
undercounts by the whole frozen footprint. Fix that before you buy anything.

**Bound the number honestly.** The frozen footprint is a *floor*: mounts sharing
segment lineage double-count it, and it excludes any retained blob no snapshot
references. It is the right input for sizing the new `fs` volume, which is what
this step is for, and it is not the size of the S3-compatible repository. For
that, ask the store: Step 9's audit classifies every key under the prefix and,
where the listing carries sizes, prints a stored-object size per disposition, so
the sum is the real footprint and the `orphaned` line is the part a delete gives
back. It still will not tell you how much of the rest is needed, because
`unexplained` holds live and dead blobs together.

---

## Step 2: provision and register the `fs` repository

An `fs` repository requires the parent directory to be listed in `path.repo` on
**every master and data node**, and `path.repo` is a **static setting**. This is
the campaign's one rolling restart. Plan it as such: it is the only disruptive
step in the whole migration, and it happens before any data moves.

```bash
# 1. Attach the block/NFS volume to every node and add its parent to path.repo.
#    Under an operator-managed deployment this is an edit to the Elasticsearch
#    resource's node sets (volume + config), applied as a rolling restart.

# 2. Register, WITH verification. No ?verify=false here:
$ES -XPUT '/_snapshot/backups-fs' -H 'Content-Type: application/json' -d '{
  "type":"fs","settings":{"location":"backups","compress":true}}'
```

`location` resolves relative to a `path.repo` entry; an absolute path is accepted
when it sits under one.

**Acceptance.** Registration returns `{"acknowledged":true}` **with verification
left on**. That is the entire point of the step. Verification writes and then
deletes a probe blob; on the S3-compatible repository that delete leg is what fails.
On an `fs` repository the delete is an unlink, so verification passes cleanly. A
successful verified registration is the first direct evidence that the fault does
not exist on this storage.

**If it fails.** A `repository_verification_exception` naming a node means that
node does not have the volume mounted or does not have the parent in `path.repo`.
Check every node, not just the one named. **Do not reach for `?verify=false` here
to make the error go away.** On an `fs` repository, verification passing is the
result you came for. Suppressing it discards the proof.

---

## Step 3: repoint SLM and prove retention works again

One policy update. **Zero ILM edits.** Nothing about index lifecycle, rollover, or
the frozen phase changes; only the repository the backup policy writes to.

```bash
# Repoint every SLM policy's "repository" to the fs repo. Nothing else changes.
$ES -XPUT '/_slm/policy/<policy-id>' -H 'Content-Type: application/json' -d '{
  "schedule":"0 30 3 * * ?","name":"<daily-{now/d}>","repository":"backups-fs",
  "config":{"indices":["logs-*"]},"retention":{"expire_after":"7d","min_count":5}}'

$ES -XPOST '/_slm/policy/<policy-id>/_execute'
```

Then prove retention actually reclaims bytes. **An acknowledged delete proves
nothing**: that is exactly what the S3-compatible repository already returns while
leaking. Count files on disk instead:

```bash
# run inside any node that has the volume, before:
find <fs-repo-location> -type f | wc -l
$ES -XDELETE '/_snapshot/backups-fs/<one-backup-snapshot>'
# and again after:
find <fs-repo-location> -type f | wc -l
```

**Acceptance.** The file count **drops**. Blobs genuinely unlink. In the reference
run it went from 26 files to 2: the repository emptied down to `index.latest` and
its current root generation blob, the irreducible floor of a repository with no
snapshots in it, because the deleted snapshot was the only one holding those
segments.

**If it fails.** A count that stays flat means you are still writing to the faulty
transport and the repoint did not take. Re-read the policy
(`GET /_slm/policy?filter_path=*.policy`) and confirm `"repository"` really says
`backups-fs`. Check that you deleted a snapshot on `backups-fs` and not on
`my-repo`.

**Leave one standing backup snapshot in place.** Step 10 restores from it.

---

## Step 4: formalize the frozen repository's registration

The frozen tier stays where it is. This step adds `?verify=false` to its
registration and changes nothing else about it.

> **A `PUT` on an existing repository REPLACES its settings. It does not merge
> them.** Any key you leave out of the body is not preserved, it is dropped. The
> one that matters most is `base_path`: drop it and the repository silently
> repoints at the bucket root, where it lists whatever other repository lives
> there and none of your own snapshots. The blobs are untouched and completely
> unreachable, no error is raised, and `{"acknowledged":true}` comes back either
> way. Read the existing settings first and carry every one of them across.

### 4a. Read what is there

```bash
$ES '/_snapshot/my-repo?filter_path=*.settings' 
$ES '/_cat/snapshots/my-repo?h=id,status' > snapshots-before.txt
wc -l < snapshots-before.txt
```

Keep both. The first is the settings you must reproduce; the second is what you
will compare against afterwards.

### 4b. Re-register, carrying every existing key

```bash
$ES -XPUT '/_snapshot/my-repo?verify=false' -H 'Content-Type: application/json' -d '{
  "type":"s3","settings":{
    "bucket":"<bucket>",
    "base_path":"<copy from 4a; omit ONLY if the existing settings have no base_path>",
    "endpoint":"<s3-compat-endpoint>",
    "path_style_access":true,
    "client":"default"}}'
```

The five keys above are the common shape, not an exhaustive list. If 4a showed
`compress`, `chunk_size`, `server_side_encryption`, `storage_class`, a different
`client`, or anything else, carry those too. The rule is that the body you send
should differ from what 4a printed only where you intend it to.

`base_path` is the prefix inside the bucket that the repository lives under. It is
the single most consequential value in this toolkit: it decides which blobs the
repository can see, and it is the same value Step 9 passes to both the audit and
the reclaim command as `--prefix`. Copy it from 4a and use it verbatim there. See
[base_path](../../README.md#base_path-the-value-that-decides-what-your-repository-can-see).

### 4c. Acceptance

`{"acknowledged":true}` proves only that the PUT parsed. It comes back for a
registration that now points somewhere else entirely. Check the snapshots instead:

```bash
$ES '/_cat/snapshots/my-repo?h=id,status' > snapshots-after.txt
diff snapshots-before.txt snapshots-after.txt && echo "SAME SNAPSHOTS, registration preserved"
```

**Acceptance.** `{"acknowledged":true}` **and** `diff` reports no difference: the
same snapshots, the same count, the same names as before the PUT.

**If the listing changed**, you have repointed the repository. Nothing has been
deleted. Put the settings from 4a back with another `PUT ?verify=false` and
re-check the listing. Reclaim nothing, delete nothing and take no snapshot
against the repository while it is repointed: two repositories registered against the same
bucket path share one `RepositoryData`, and writing through either can corrupt the
other.

**What `?verify=false` does and does not do.** It is the documented workaround for
this fault. It skips the write-then-delete probe whose delete leg fails, and
nothing else. It does **not** suppress any error on the read path, and it does
**not** make deletes work. Step 6 measures precisely what it does not fix.

Re-registering does not touch the objects in the bucket, and with the settings
carried across correctly the repository keeps seeing the same snapshots and the
mounts keep working. That is a statement about a correct re-registration, not a
property of the `PUT` itself: get the settings wrong and the contents survive
while becoming unreachable, which is the failure this step is built to prevent.

**If it fails.** A 500 with `RepositoryVerificationException` means `verify=false`
did not reach the request; it is a query parameter on the `PUT`, not a body
setting. Anything else and the repository settings themselves are wrong; compare
against 4a before you overwrite it.

---

## Step 5: prove the frozen tier still serves from the repository

Do not use a search to prove this. Ask the repository.

```bash
$ES -XPOST '/_snapshot/my-repo/_verify_integrity'
```

**Acceptance.** `"total_anomalies": 0` and `"result": "pass"`.

`_verify_integrity` verifies whatever the registration currently points at. A
repository repointed at the wrong bucket path passes cleanly, because the
repository it lands on is healthy. Step 4c is what catches that; do not read a
pass here as confirmation that the registration is right.

### Why the cache-clear-then-search check was removed

The gate this runbook used to carry was
`POST /_searchable_snapshots/cache/clear` followed by a
`track_total_hits` search, accepted on a full doc count with `"failed": 0`. It
passes on a mount whose blobs are gone, so it never told you what it claimed to.

Measured on the rig, a local test lab reproducing this fault against a pinned
MinIO, with a mount whose 8 data blobs were all deleted: HTTP
200, `total=200`, `"failed": 0`. A clean pass on a destroyed mount. Three
repairs were tried and none of them work:

- `&request_cache=false` changes nothing. The pass survives it, survives an
  explicit request-cache clear, and appears on the first query against a mount
  created after the blobs were already gone, so no cache entry could have
  existed. The count comes from the open Lucene reader.
- `size=1` catches total loss and misses partial loss. With one segment's blob
  deleted, `size=1`, sorts and aggregations all passed, and the aggregations
  returned correct values covering the destroyed segment. A `max` aggregation
  returned 1393.0, a document living in the segment whose only blob no longer
  existed.
- Closing and reopening the index detects nothing on either mount type. Both
  return to green with no error in the cluster log.

Part of the reason is the `.snapshot-blob-cache` system index, which caches blob
byte ranges per repository and per snapshot. It survives
`_searchable_snapshots/cache/clear` and it survives a remount under a new name,
so any recipe built on "clear the cache, then query" has to account for it.

A `full_copy` mount is worse. One that finished prewarming keeps serving from
its local copy with the repository destroyed, and no search-shaped check works
there at all. `full_copy` in 9.5.2 fetches lazily and depends on prewarm: a
`full_copy` mount created after the blobs were deleted reported green and
46.7kb in `_cat/indices`, then returned 404 against S3 on the first document
fetch.

`_verify_integrity` caught every case the search missed: total loss (8
anomalies), partial loss (1 anomaly, named with its Lucene filename), and
corruption by length mismatch.

**Two limits carry with it, and both belong in the sign-off.**
`_verify_integrity` is repository-scoped and slow on a large repository, so it
is a gate you schedule rather than a probe you run per index. And by default it
compares blob names and lengths without downloading contents, so it catches a
missing or wrong-sized blob and not a blob whose bytes are wrong at the right
length.

A cold search is still worth running as a smoke test, and the reference run
returned 3,500 hits with 0 failed shards after a cache clear. Record that. Do
not accept on it.

**If it fails.** Anomalies mean the repository no longer holds everything the
mount needs. Stop the migration. Reclaim nothing. Go back to Step 0 and check
whether the mount's snapshot is still in the catalog.

---

## Step 6: measure the residual leak honestly

The hybrid does not fix deletes on the S3-compatible repository. Prove that on the
record rather than letting it be an assumption.

```bash
$ES -XDELETE '/_snapshot/my-repo/<scrap-snapshot>'
$ES -XDELETE '/_snapshot/my-repo/<old-snapshot>'

# capture the ES node log covering those deletes
grep -c "Failed to delete" es-node.log
```

**Acceptance.** Both deletes return `{"acknowledged":true}` **and** the log carries
failed-delete WARN lines. Reference run: **14** `Failed to delete` WARN lines from
two snapshot deletions, alongside the `no longer part of any snapshot ... but
failed to remove them` lines that name the condemned keys.

That residue is Step 9's entire workload under the hybrid, and it is bounded: the
S3-compatible repository now sees occasional frozen-tier churn instead of a daily
retention cycle. The WARN count is the measure of the leak. It is no longer an
input to any tool here, because nothing in this repository reads those lines any
more.

**If it fails.** Zero WARN lines with acknowledged deletes could mean the endpoint
was fixed, or could mean you captured the wrong node's log or a window that does
not cover the deletes. Widen the capture and confirm the timestamps bracket the
delete calls before you conclude anything. Do not report "the leak is gone" off a
log you have not bracketed.

---

## Step 7: export the mounted set

Repository metadata cannot see which snapshots are pinned by searchable-snapshot
mounts; that fact lives in index settings on the cluster. Step 9's audit reads it
off the cluster itself, given `--elasticsearch` and `--es-repository`, and refuses
the run if it asks and gets no answer. Export it anyway: this file is the copy a
person reads, diffs against last week's, and puts in front of whoever signs off
the delete.

Get the repository name off the cluster first. The export filters on it, names
are case sensitive, and a name that matches nothing exits 0 with an empty file:

```bash
$ES '/_snapshot/_all?filter_path=*.type'

./snapshot_sizes.py --es https://es.example.com:9200 --repo my-repo \
    --api-key "$ES_API_KEY" --ca-cert /path/to/ca.crt \
    --emit-mounted --out mounted.txt

# The count is the check. Compare it against the mounts you know you have.
grep -cv '^#' mounted.txt
```

**Acceptance.** One line per mounted snapshot: snapshot name first, then uuid,
tier and mounting index. The row count matches the number of mounted snapshots
you expect.

**If it fails.** An empty file while frozen indices exist is a **failure, not a
pass**. It means the export did not see them, and an empty listing of mounts
proves nothing while looking like it proved something. It also means the key you
are holding cannot read mount settings, which is the same key Step 9's audit will
use for its veto. The usual causes, in order of how often they bite: the repository name is wrong or
wrongly cased, the key is missing `view_index_metadata` on `*`, or you are pointed
at the wrong cluster. Confirm which with
`GET /*/_settings?filter_path=*.settings.index.store.snapshot`: an empty `{}` at
HTTP 200 means the cluster really has no mounted searchable snapshots.

---

## Step 8: withdrawn, and nothing replaces it

Step 8 used to run the log-driven sweeper over the keys Elasticsearch itself
condemned in its failed-delete WARN lines. That tool is retired and is no longer
in this repository.

**The reason is the one that matters here.** Starting from a key Elasticsearch
named looks like a stronger premise than reachability, and it is not enough: the
tool still had to answer whether some surviving snapshot references that key now,
and it answered by absence from a live set it computed itself. Any read that
failed, or any document that would not parse, turned into a deletion. A reviewer
drove one of these tools, along its documented path, to a real delete of a live
segment blob.

Nothing here reads those WARN lines any more, and no current tool does what this
step did. Keep capturing them anyway: Step 6 uses the count as the measure of the
leak, and the series is what tells you whether the topology is holding.

What the step was *for*, getting the leaked keys out of the bucket, is Step 9's
job now. Step 9 derives them from the repository's own metadata rather than from
a log line, and a failed read there shortens its manifest instead of extending
it.

---

## Step 9: audit the residue, read the manifest, then reclaim it

Two commands, and the split is the design.
[`python3 -m generation_chain`](../../generation_chain/README.md) reads and
cannot delete: its HTTP layer allows GET and HEAD refused at the transport.
`python3 -m generation_chain.reclaim` deletes and derives nothing: it removes
exactly the keys a manifest names, in the order given. Between the two sits a
person reading that manifest.

The audit condemns a blob on its PRESENCE in a deleted snapshot's file list, not
on its absence from a live set, so what it names is a subset of what
Elasticsearch's own delete would have collected. A read that fails costs coverage
rather than data. That is the property Step 8's tool did not have.

### 9a. Audit, and read the coverage report

```bash
python3 -m generation_chain \
    --transport s3 \
    --endpoint <s3-compat-endpoint> --region <region> \
    --bucket <bucket> --prefix <the base_path from Step 4a> \
    --credentials creds.json \
    --elasticsearch https://es.example.com:9200 --es-repository my-repo \
    --manifest orphans.tsv --classification classified-keys.tsv \
    --coverage-json coverage.json
```

`--prefix` is the repository's `base_path`, copied from Step 4a exactly. An empty
`base_path` means the repository is the whole bucket.

**Pass `--elasticsearch` and `--es-repository` every time.** The CLI treats them
as optional and they should not be. Which snapshots have searchable-snapshot
indices mounted on them lives only in cluster state, nothing stops you deleting
one an index depends on, and the cluster's answer can only ever remove keys from
the manifest. A run that asked and could not be answered refuses rather than
proceeding with an empty veto.

**Write the manifest with `--manifest FILE`, not a `>` redirect.** The completion
marker that proves the derivation finished is appended to a named file, and the
reclaim command refuses a manifest that does not carry one.

**Acceptance.** Exit 0, a manifest, and a coverage report you have actually read.
The report is not a formality: a short manifest means either that there is little
to clean up or that the run could not see most of the repository, and those look
identical without the coverage numbers. **A key absent from the manifest is not
evidence that the key is live.** `orphaned` is the only disposition that is a list
of things to delete; `unexplained` is what the run could not decide either way,
and some of it is live.

**If it fails.** The exit code says whether to retry. `2` is a settled refusal, so
retrying changes nothing: an unsupported repository format, or a catalog the run
could not anchor. `3` is the invocation or a credential. `4` means the store or
the cluster did not answer, and a retry is reasonable. `5` means a single shard
directory is larger than this host can hold, so run it somewhere bigger, narrow
it with `--prefix`, or raise `--max-ram`.

**A repository under churn is safe to audit and yields less.** Every gate that
notices a document moving underneath it drops that shard, so the manifest gets
shorter rather than different, and what this pass misses the next one picks up.

### 9b. Dry run, which is the default

```bash
python3 -m generation_chain.reclaim \
    --manifest orphans.tsv \
    --endpoint <s3-compat-endpoint> --region <region> --bucket <bucket> \
    --prefix <the same base_path> --credentials creds.json
```

Nothing is sent. It prints the batches it would send, the manifest's row count
and sha256, and the exact `--approve-digest` and `--approve-rows` that would
authorise that file.

`--prefix` must be the one the manifest was derived under. The manifest holds
keys relative to the prefix and this command puts the prefix back on, so a
mismatch aims the deletes at keys nobody audited.

`--checksum-algorithm` defaults to `md5`, which is what this endpoint requires,
and that header is why this works where Elasticsearch does not: the batch
`DeleteObjects` goes out carrying `Content-MD5`. AWS S3 takes `crc32`. The store
decides it, not the tool.

This command speaks the S3 compatibility path only. There is no OCI-native
delete transport, so the credential it needs is the Customer Secret Key pair, the
same one the audit used for `--transport s3`.

**Acceptance.** The key count matches the audit's `Reclaimable` line, and you have
read rows rather than only counted them. Every row carries the key, why it was
named, and which snapshot's deletion orphaned it, so a row is checkable on its
own.

> [!CAUTION]
> **The next command deletes objects from a production bucket, and they do not
> come back.** Through the Amazon S3 Compatibility API there is no recovery path
> at all; see the prerequisite at the top of this runbook. If the dry run's
> manifest is wrong, 9c is where that becomes permanent.

### 9c. Execute

```bash
python3 -m generation_chain.reclaim \
    --manifest orphans.tsv \
    --endpoint <s3-compat-endpoint> --region <region> --bucket <bucket> \
    --prefix <the same base_path> --credentials creds.json \
    --execute --approve-digest <the digest 9b printed> \
    --approve-rows <the count 9b printed> \
    --elasticsearch <cluster> --es-repository <repository> \
    --report deleted.jsonl
```

`--execute` refuses to run without one of `--elasticsearch` with
`--es-repository`, or `--without-elasticsearch`. The manifest's protection was
decided when it was derived, and a searchable snapshot mounted since then is not
in it, so the tool makes you say whether to re-check. Step 9a passes the cluster
every time and this step does the same. Use `--without-elasticsearch` only when
the repository is orphaned and there is no cluster left to ask.

The approval covers one file. It will not execute a different manifest, and
editing a manifest invalidates its approval, which is the point: the digest is
tied to the exact bytes somebody read.

Keep `--report`. One JSON line per batch, recording which keys were requested and
what the store said about each. It is the only account of what happened that does
not depend on the store.

**Acceptance.** Exit 0, and every key the run executed against was deleted or
already absent. Then run Step 10 before taking another snapshot.

**If it fails.** Exit `3` means the approval does not match this manifest. That is
the guard working; re-run 9b against the file you actually intend to delete
rather than editing the numbers to fit. Exit `4` means the run executed and at
least one key failed or went unconfirmed, so read `deleted.jsonl` to see which,
and treat the repository as having had partial deletion traffic: Step 10 applies.
Exit `2` is the invocation, the manifest or the checksum algorithm.

**Measured, and on what.** Twelve audit-and-reclaim cycles were run back to back
against the rig, a local lab reproducing this fault against a pinned MinIO, with
the load generator writing and SLM snapshotting every fifteen seconds throughout.
146,800 objects were removed across the twelve. Every cycle that preserved its
output reported zero failed and zero unconfirmed deletes, `unconfirmed` being
keys the store's own answer accounted for neither way. The restore checks at
cycles 3, 6 and 9 each restored a real index with zero integrity anomalies. The raw output is in
[evidence/delete-campaign](../../evidence/delete-campaign/README.md).

**What that does not cover.** The cleanup leg has since been run against a real
Oracle Object Storage bucket, 58 cycles with 888 objects deleted and no failed
or unconfirmed deletes, but against one tenancy and one repository shape.
Treat the first production run as an experiment anyway: dry run, read the
manifest, reclaim, then restore something.

**Do not fill the gap with a raw object-store client.** Deleting a key out of this
bucket by hand carries every risk the retired tools carried and none of the
guards these two commands put in front of it, and there is no undo.

---

## Step 10: final proofs

Two independent proofs, one per leg of the hybrid. **Both must pass.**

```bash
# 1. The S3-compatible repository is internally consistent after the reclaim, and
#    the frozen tier's blobs are all still there. One call covers both, because a
#    search against the mount does not.
$ES -XPOST '/_snapshot/my-repo/_verify_integrity'

# 2. The fs repository can actually restore a backup
$ES -XPOST '/_snapshot/backups-fs/<standing-backup>/_restore?wait_for_completion=true' \
  -H 'Content-Type: application/json' -d '{
    "indices":"<index>","rename_pattern":"(.+)","rename_replacement":"restored-$1"}'
$ES '/_cat/count/<index>?h=count'; $ES '/_cat/count/restored-<index>?h=count'
```

[`verify_restorable.py`](../../verify_restorable.py) does the second one
for you, against either repository. It restores under a fresh name, counts the
documents that come back, and deletes nothing:

```bash
python3 verify_restorable.py \
    --elasticsearch https://es.example.com:9200 --repository backups-fs \
    --password-file /path/to/password
```

It picks the most recent `SUCCESS` snapshot holding an index that is not a
searchable-snapshot mount, restores it as `probe<timestamp>`, counts the
documents and deletes the probe. Zero documents is a failure. Snapshots still
being written are excluded from the candidates rather than treated as a defect,
because on a cluster with SLM running there is nearly always one. When there is
nothing completed to restore from yet it says so and exits 0, which is a check
that did not run rather than a check that passed. Read the output, not just the
exit code.

**Acceptance.**

- `_verify_integrity` verifies every snapshot with no errors and reports
  `"total_anomalies": 0, "result": "pass"`. Reference run: 2/2 snapshots, 3/3
  indices, 4/4 index-snapshots, 28 blobs, final repository generation 9.
- The restore completes with `failed: 0` shards and **exact** doc-count equality
  against the original index (reference: 3,500 / 3,500).

**Exact equality, not approximate. Anything less is a failed campaign.**

On Elasticsearch 9.5.2 the `results` object of `_verify_integrity` contains
exactly `status`, `final_repository_generation`, `total_anomalies` and `result`.
It carries no `snapshot_restorability` and no `restorable_snapshot_count`, so an
acceptance criterion written against either of those names never matches
anything. Read `results.total_anomalies` and `results.result`.

**Run this after any deletion traffic, not only at the end of the migration.** A snapshot
taken after a bad delete reports `SUCCESS` and cannot be restored. Elasticsearch
deduplicates shard files on physical name, length and checksum, and never checks
that the blob is still in the store, so a snapshot happily reuses a reference to
a blob that is gone. Nothing surfaces until someone tries to restore, which can
be weeks of backups later. `_verify_integrity` is what stands between a bad
delete and that.

It does not stand between you and every bad delete. It walks the snapshots the
repository currently lists, so a snapshot deleted while an index was still
mounted on it is not inspected at all, and the check reports
`total_anomalies: 0, result: pass` with that index already destroyed. Measured on
the rig. Pair it with `--emit-classified` and require `0 mounted snapshot(s)
MISSING-FROM-CATALOG`, which is the Step 0 check run again. Neither covers the
other.

**A cold frozen search is a smoke test, not a proof.** Reference run: 3,500 hits,
0 failed shards. It also returns exactly that against a mount whose blobs have
been deleted, which is why it is no longer an acceptance criterion. Step 5 has
the measurements.

**If it fails.** `_verify_integrity` anomalies mean something the repository still
references has been removed. Stop, and reclaim nothing further. Do not take
another snapshot either, because the next one will inherit the damage and report
`SUCCESS`. A restore with mismatched counts means the fs repository is not a
working backup target, which invalidates the whole migration; recheck Step 2 and
Step 3 before you decommission anything on the old repository.

---

## Acceptance criteria, condensed

| Step | Must be true |
|---|---|
| Step 0 | `--emit-classified` reports `0 mounted snapshot(s) MISSING-FROM-CATALOG` |
| Step 1 | every snapshot classified; new-volume sizing uses baseline + retention growth + upgrade headroom, **frozen footprint excluded** |
| Step 2 | `fs` repository registers `acknowledged:true` **with verification on** |
| Step 3 | on-disk file count **drops** after a backup delete (reference: 26 → 2) |
| Step 4 | S3-compatible repository re-registers `acknowledged:true` with `?verify=false`; mounts undisturbed |
| Step 5 | `_verify_integrity` on the S3-compatible repository: `total_anomalies: 0`, `result: pass`. A cache-clear-then-search check does **not** substitute; it passes on a destroyed mount |
| Step 6 | deletes still `acknowledged:true` **and** failed-delete WARNs appear (reference: 14) |
| Step 7 | one line per mounted snapshot; never empty while frozen indices exist |
| Step 8 | withdrawn with the log-driven sweeper; no current tool replaces it |
| Step 9 | audit exits 0 with a manifest and a coverage report you read; the dry run's key count matches the audit's `Reclaimable` line; `--execute` runs only against that manifest's own digest and row count, and exits 0 |
| Step 10 | `_verify_integrity` clean (`total_anomalies: 0`, `result: pass`); restore doc counts equal exactly |

---

## Standing guardrail: MOUNT ONLY CLONES, never policy snapshots

Elasticsearch **does not block deleting a snapshot that backs a mounted
searchable-snapshot index.** Only repository *unregistration* checks mounts. So a
policy snapshot that someone mounted can be reaped by ordinary SLM retention, and
the mounted index will keep serving from blobs no live snapshot references until
something deletes them, at which point the index is destroyed.

**The rule: mount searchable snapshots from dedicated clones, never from snapshots
an SLM policy owns.** The audit tool labels a snapshot that is both SLM-created and mounted
`slm+mounted` and buckets it as `frozen-pinned` for exactly this reason: treating
it as a backup would count its total as growth *and* imply retention may delete
it.

Audit for it continuously with
[es-snapshot-audit](../es-snapshot-audit/SKILL.md):
`--emit-classified` and look for `slm+mounted` in the `class` column, and for any
`MISSING-FROM-CATALOG` row.

---

## Ongoing maintenance loop

The hybrid is not a one-time migration. It leaves one repository that still leaks
on delete, and that repository needs a standing loop.

**After any deletion traffic on the S3-compatible repository** (frozen tier churn, an
index unmounted, a manual snapshot removed):

1. Capture the ES node log covering the deletes, and count the failed-delete
   WARN lines. That count is the leak, and keeping the series is what tells you
   whether the topology is holding. Nothing consumes those lines any more; the
   number is for you.
2. Re-run `--emit-classified` and require `0 mounted snapshot(s)
   MISSING-FROM-CATALOG`, and refresh the mounted set with `--emit-mounted --out
   mounted.txt`. That export names the snapshots a mounted index depends on,
   which is the set nobody may delete by any means.
3. Run Step 9 on a cadence that suits the churn: audit, read the manifest,
   reclaim. The audit gets slower as the repository accumulates root generations
   that nothing removes, which is a known cost of the unfixed fault, so a
   repository under heavy churn wants shorter, more frequent passes rather than
   one long one.
4. Run `POST /_snapshot/my-repo/_verify_integrity` and require
   `total_anomalies: 0`, `result: pass`. This is not optional hygiene. A snapshot
   taken after a bad delete reports `SUCCESS` and cannot be restored, so an
   unverified reclaim can seed weeks of broken backups before anyone notices.
   `_verify_integrity` cannot see a mounted index whose snapshot has left the
   catalog, which is why item 2 is separate and neither covers the other.
5. Restore something with `verify_restorable.py`. A repository can list clean,
   report `SUCCESS` on every snapshot and pass `_verify_integrity` while being
   unrestorable, and this project has measured that.

Track the WARN count and the repository's total size over time. Under the hybrid
both should stay small and bounded. A growing residue means deletion traffic is
still hitting the S3-compatible repository more than the topology intends, and
the fix is upstream. It was never more reclaiming.

---

## Honest caveats

The test rig did **not** validate four things about this architecture. Carry them
into the production plan rather than letting the runbook above read as broader
coverage than it is.

**1. The storage substrate under the `fs` repository is not validated.** The rig's
`fs` repository lived on an ephemeral in-pod volume, not on real NFS or a real
block device. What that proves is the *Elasticsearch* half: registration passes
with verification on, SLM writes there, and retention deletes genuinely unlink
blobs. What it does not touch is the storage half: mount options (`hard`, `intr`,
`sync`, `noac`), server-side failover, stale file handles under a failover,
permission and ownership behavior across nodes, or capacity exhaustion.
**Validate the volume itself separately, before the migration, with the storage
team's own acceptance tests.**

**2. The cleanup leg has run against one real endpoint, not many.** Step 9 was
measured against MinIO pinned to a release that rejects the same call, and then
against a real Oracle Object Storage bucket over the Amazon S3 Compatibility
API. Claims about other S3-compatible stores that reject the same call, Dell
ECS and Hitachi HCP among them, still rest on their published operation lists
rather than on a run. This is a limit on the whole architecture and not on any
one step of it.

**3. The two halves of Step 9 are validated differently.** The audit is covered
by the unit suite and by twelve live cycles against a churning repository. The
reclaim command's own guards, the completion marker, the approval digest and the
checksum header, are covered by unit tests plus those same cycles against MinIO.
What no test can supply is the one thing an operator supplies: reading the
manifest before approving it. The design puts a person there deliberately, and a
person who approves without reading has removed the last gate.

**4. The pre-existing limits carry over.** The hybrid does not narrow the WARN
truncation gap, does not exercise the TRACE drain procedure, does not touch a real
production object-storage endpoint, and does not test scale: the rig is tens of
objects, so pagination, worker concurrency under real latency, and memory behavior
on a repository with millions of blobs are covered by code review only. The
campaign exercised the frozen tier against a real mount, but it is **one** partial
mount. Frozen
tiers with many mounts sharing segment lineage, where the measured footprint
double-counts, remain sized by a floor rather than a figure.

---

## Related

- [es-snapshot-audit](../es-snapshot-audit/SKILL.md) is the sizing and inventory
  tool behind Step 0, Step 1 and Step 7.
- [`generation_chain`](../../generation_chain/README.md) is the audit and
  the reclaim command behind Step 9, with the safety condition, the exit codes
  and what it cannot see.
- The two runbooks that used to run Step 8 and Step 9, es-log-cleanup and
  es-orphan-sweep, are removed along with the sweepers they drove. See the
  retirement note in [the main README](../../README.md).

Proof, and which parts of it are history. Procedure and acceptance criteria:
[methodology.md](../../evidence/methodology.md) §1.3 (the architecture), §4 (Step 0
through Step 10), §6.2 (what the campaign did not prove). Measured outcomes:
[campaign-data.md](../../evidence/campaign-data.md) Part II: §7 sizing inputs, §8 registration and repoint, §9 the retention unlink proof, §10
`verify=false` and frozen serving, §11 the residual leak, §14 the restore.

Both documents record a campaign whose cleanup steps were driven by the retired
sweepers, and they carry a banner saying so. Read §12 and §13 there, the cleanup
and the verification after it, as an account of what a wrong delete costs and
what `_verify_integrity` does and does not notice, which are properties of
Elasticsearch and the object store rather than of any tool. The commands in them
no longer exist. The evidence for Step 9 as it is written today is
[evidence/delete-campaign](../../evidence/delete-campaign/README.md).
