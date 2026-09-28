# Running it: find what leaked, reclaim it, check nothing broke

Three steps, and only the second can delete anything. Step one is the audit,
`python3 -m generation_chain`, which reads and cannot delete. Step two is
`python3 -m generation_chain.reclaim`, which deletes only what a manifest you
read names, behind an approval bound to that exact file. Step three restores
something and counts it.

If all you want is a number out of an existing bucket, step one is the whole
job and nothing on the way can remove an object.

> [!IMPORTANT]
> **Run this in your own environment before you trust it with anything.**
>
> The delete path has been exercised against a live Oracle Object Storage
> bucket: 58 cycles, 888 objects deleted, no failures, no unconfirmed deletes,
> and every cycle reading every shard directory it depended on. That was one
> tenancy, one bucket and one cluster. Yours is a different one, and no result
> of ours tells you what will happen in it.
>
> [Testing in your own OCI environment](testing-guide.md)
> is the procedure for finding out. It builds a repository that leaks on
> purpose, in a bucket holding nothing you care about, and runs the same loop
> against it. It takes about ninety minutes and it tells you whether this tool
> is safe against your data, which is a question we cannot answer from here.
>
> Do that before you point anything at a repository you rely on. The audit
> reads and cannot delete. The reclaim tool does delete, and an object store
> with no version history does not give it back.
>
> Validation: the unit suite (`python3 -m unittest discover -s tests`), two
> live-rig campaigns against a real Oracle bucket, and adversarial review; see
> [Campaign results](../FACTS.md#campaign-results-2026-08-27-against-a-real-oracle-bucket)
> in FACTS.md for the raw numbers. Reproduce a campaign of your own with
> [Testing in your own OCI environment](testing-guide.md).

## What you need

One directory:

```
generation_chain/     50 modules, 375 KB of Python, standard library only
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

You also need Python 3.12 or newer, and a credentials file you write yourself.
You do not need the tests, the evidence, the docs, or anything else in this
repository.

And your repository's `base_path`, the prefix inside the bucket it lives
under. Read it with `GET _snapshot/<repo>`. See
[base_path](operating-the-repository.md#base_path-the-value-that-decides-what-your-repository-can-see)
for why getting it wrong points every tool here at the wrong repository.

## The credentials file

Write a JSON file and pass its path with `--credentials`. The tool takes a path
rather than a value because a secret in argv shows up in `ps` for every user on
the host, and it refuses a file other users can read. `chmod 600` it; `0400`
is accepted too.

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

## Step one: find out what is orphaned

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

`--prefix` takes the `base_path` with or without a trailing slash. Both tools
normalise it.

While it runs it tells you where it is:

```
[00:07:05] listed 12,742 objects
[00:07:19] read the generation chain: 195 generation(s) believed, current 194
[00:07:19] reading 200 shard directories in 1 group(s). This is the slow part
```

**Expect that last phase to be slow and quiet.** It reads one shard document
per directory per generation, and nothing ever removes a generation, so a
repository that has been leaking for a while costs more to read than a fresh
one. Ten minutes is normal. It has not hung. `--quiet` turns the progress off.

Nothing in this step can delete. The audit's HTTP transport permits `GET` and
`HEAD` and raises on anything else, and that refusal is a raise rather than an
assertion, so `python3 -O` cannot strip it. The audit does not import the
package that deletes, and a test fails if a future change makes it.

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

### Keep the manifest

`--manifest orphans.tsv` is the file. It is written whether or not you ever
delete, and it is tab separated with a header:

```
key  reason  category  snapshot_uuid  snapshot_name  from_generation  to_generation
```

Just the object names:

```bash
tail -n +2 orphans.tsv | grep -v '^#' | cut -f1 > orphan-keys.txt
wc -l orphan-keys.txt
```

Only segment blobs, which are the data:

```bash
awk -F'\t' '$3 == "segment blob" {print $1}' orphans.tsv | wc -l
```

The key count, to check against the report:

```bash
awk -F'\t' 'NR > 1 && $1 !~ /^#/ {n++} END {print n, "keys"}' orphans.tsv
```

To keep the report as well as the file:

```bash
python3 -m generation_chain ... --manifest orphans.tsv 2> report.txt
```

The last line of the manifest is `# derivation complete`. If it is missing,
the run refused partway and the file is not a manifest. Nothing will act on
one without it.

### Reading the result

Read the coverage report as well as the manifest. A short manifest means either
that there is little to clean up or that the run could not see most of the
repository, and those look the same without the coverage numbers.

A key missing from the manifest is not evidence that the key is live.

You can audit a repository while it is being written to. Every gate that notices
a document moving underneath it drops that shard, so a busy repository yields
fewer keys rather than different ones, and whatever it misses gets picked up on
the next run.

### When it refuses

It refuses rather than guessing, and the message says what to do.

The exit codes matter if you schedule this. `0` wrote a manifest. `2` refused
for a settled reason, so retrying changes nothing. `3` means the invocation or a
credential is wrong. `4` means the store or cluster did not answer, and a retry
is reasonable. `5` means the repository is larger than the host can hold.

The three refusals you are most likely to meet:

- **The credentials file is group or world readable.** `chmod 600` it.
- **`--region` is wrong.** A wrong region and a wrong endpoint both answer a
  bare 403, so it is never defaulted.
- **No `elasticsearch` section, but you passed `--elasticsearch`.** The audit
  reads its cluster credential from the credentials file. There is no flag
  that takes a password.

## Step two: delete, once you have read the manifest

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
  --elasticsearch https://<cluster>:9200 --es-repository <repository> \
  --report deleted.jsonl
```

**`--execute` will not run without you saying which of those last two lines you
mean.** The manifest's protection was decided when it was derived, and a
searchable snapshot mounted since then is not in it. So the tool makes you
choose: pass `--elasticsearch` with `--es-repository` to re-check the veto
against the cluster as it is now, as above, or pass `--without-elasticsearch`
to state that there is no cluster to ask, which is the case when the repository
is orphaned and its cluster is gone.

Neither is the default. Defaulting to checking would break anyone reclaiming an
orphaned repository, and defaulting to not checking would make the dangerous
path the quiet one.

The approval covers that one file. An approval for one manifest will not execute
another, and editing a manifest invalidates its approval. It refuses a manifest missing its
completion marker rather than half executing it.

`--report` writes a line per batch recording which keys were requested and what
the store said about each. Keep it. It is the only record of what happened that
does not depend on the store.

`--checksum-algorithm` defaults to `md5`, which is what Oracle and MinIO
require. AWS S3 takes `crc32`. Your store decides this, not the tool.

## Step three: check you did not break anything

```bash
python3 verify_restorable.py \
  --elasticsearch https://<cluster>:9200 --repository <repo> \
  --password-file /path/to/password
```

Run this after any delete. A repository can list clean, report `SUCCESS` on
every snapshot, and pass `_verify_integrity` while being unrestorable, which
this project has measured. Restoring a snapshot and counting the documents that
come back is the only check that catches it.

## Authenticating to Elasticsearch with a read-only API key

Two things here talk to Elasticsearch: the audit's `--elasticsearch` flag,
described in [The credentials file](#the-credentials-file) above, and the
manual `_verify_integrity` check in [Verify the repository](operating-the-repository.md#verify-the-repository-after-any-deletion-traffic-not-just-after-the-migration).
Neither writes to the cluster. A read-only key is all either needs, and a
read-only key is all it should ever be given.

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

Both only read. Neither writes, mounts, deletes or restores anything in ES, so
the key below is strictly read-only: `monitor_snapshot` is snapshot and
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

`monitor_snapshot` covers the calls `--elasticsearch` makes for corroboration,
`GET _snapshot/<repo>/_all` and `GET _snapshot/_status`, plus
`GET _snapshot/<repo>/<names>/_status` for the manual checks above.
`view_index_metadata` on `*` is what lets corroboration read
`index.store.snapshot` settings off mounted searchable-snapshot indices, which
is how it knows which snapshots to remove from the manifest. Response:

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
| What `elasticsearch.api_key` wants | The `encoded` field, verbatim, in the credentials file described above. It is sent as `Authorization: ApiKey <value>` with no transformation. |
| If you only kept `id` and `api_key` | `encoded` is just `id:api_key` in base64, so `printf '%s' "$ID:$KEY" \| base64` reproduces it. Read both from a `0600` file rather than typing them. |
| Lost the secret | It is shown once, at creation, and cannot be retrieved afterwards. `GET /_security/api_key` returns metadata only (id, name, creation, expiration, role_descriptors), never the secret. |
| Recovery | Invalidate and reissue: `DELETE /_security/api_key` with `{"ids":["<the id>"]}`, then repeat step 1. |

Don't paste the key literally into a shell command. It lands in history and in
`ps` output. Write it straight into the credentials file's
`elasticsearch.api_key` field, keep that file `0600`, and if you need it as a
shell variable for the manual checks below, read it from a file rather than
typing it:

```bash
export ES_API_KEY="$(cat /path/to/es-snapshot-readonly.key)"
```

### 3. Usage

```bash
python3 -m generation_chain \
  --transport s3 \
  --endpoint https://<namespace>.compat.objectstorage.<region>.oraclecloud.com \
  --region <region> \
  --bucket <bucket> \
  --prefix <the repository's base_path> \
  --credentials creds.json \
  --elasticsearch https://es.example.com:9200 --es-repository my-repo \
  --es-ca-cert /path/to/ca.crt \
  --manifest orphans.tsv
```

`creds.json` carries the `elasticsearch.api_key` field from step 1, in the
shape [The credentials file](#the-credentials-file) shows above. The manual
check `_verify_integrity` above is a separate call, made directly:

```bash
curl -s --cacert /path/to/ca.crt -H "Authorization: ApiKey $ES_API_KEY" \
  -XPOST 'https://es.example.com:9200/_snapshot/my-repo/_verify_integrity'
```

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
calls corroboration makes, one per privilege:

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
| 401 | Key invalidated, expired, or malformed. Commonly a raw `id:api_key` passed instead of the base64 `encoded` value. | Reissue per step 1; put `encoded` in the credentials file. |
| 403 on `_snapshot/...` | Missing cluster `monitor_snapshot`. Corroboration cannot proceed, and the audit refuses rather than run uncorroborated. | Add `"cluster": ["monitor_snapshot"]`. |
| 403 on `_settings` | Missing index `view_index_metadata` on `*`. Same refusal: corroboration cannot confirm which snapshots are mounted. | Add the `indices` block on `["*"]`. |
