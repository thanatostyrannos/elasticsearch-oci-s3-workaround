# Testing this in your own OCI environment

Nothing in this repository should be taken on faith. The results published here
came from one tenancy, one bucket and one cluster. Yours will differ in ways
neither of us can predict, and the only way to find out whether this tool is
safe against your data is to run it against your data.

This page is the procedure for doing that. It uses a **separate bucket** that
holds nothing you care about, so that a mistake anywhere in it costs you
nothing.

Read [the read-only quickstart](quickstart-read-only.md) first if you only want
a report of what is orphaned. This page is for the fuller exercise: build a
repository that leaks on purpose, then confirm the tool finds and removes the
leaked objects without touching anything live.

## Words used here

**Snapshot repository.** The bucket and path where Elasticsearch stores
snapshots. Registered with a name, and referred to by that name afterwards.

**base_path.** The prefix inside the bucket that a repository occupies. Two
repositories can share a bucket if their base paths differ.

**ILM, index lifecycle management.** The Elasticsearch feature that ages an
index through phases: hot, then frozen, then deleted. It is what manufactures
new indices and removes old ones without anyone asking.

**SLM, snapshot lifecycle management.** The feature that takes snapshots on a
schedule and expires them after a retention period.

**Orphan.** An object still in the bucket that no live snapshot references. On
a store that deletes correctly these do not accumulate. On OCI Object Storage
through the S3 Compatibility API they accumulate forever, because Elasticsearch
sends a checksum header the store rejects, and reports success anyway.

## Before you start

You need:

- An OCI tenancy where you can create a bucket you do not mind destroying.
- A customer secret key for Object Storage, which is what the S3 Compatibility
  API authenticates with. This is not your API signing key.
- An Elasticsearch cluster at 8.19.17 or later, or 9.5.0 or later. Earlier
  versions do not send the header that causes the fault, so they will not
  reproduce it.
- Python 3.9 or later on the machine running the tests. No other dependency.
- Roughly an hour and a half of cluster time, and about 300 MB of bucket space
  at the settings below.

Do not point any of this at a bucket holding a repository you rely on. Not the
load generator, and not the delete path. The read-only path is safe anywhere,
by construction, but there is no reason to take the risk while you are learning
what the tool does.

## Step 1: make a bucket that holds nothing

```
oci os bucket create --name es-leak-test --compartment-id <compartment-ocid>
```

A standard bucket in the same region as your cluster. Do not enable versioning:
the S3 Compatibility API cannot list object versions, so versioning would give
you no recovery path here anyway, and it changes what a delete means. That
limitation is documented in
[Oracle's supported operations list](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/s3compatibleapi_topic-Amazon_S3_Compatibility_API_Support.htm)
and discussed further in [blast radius](blast-radius.md#there-is-no-recovery-path-through-the-amazon-s3-compatibility-api).

Your S3 compatibility endpoint is:

```
https://<namespace>.compat.objectstorage.<region>.oraclecloud.com
```

`<namespace>` comes from `oci os ns get`. It is not a secret, but it identifies
your tenancy, so keep it out of anything you publish.

## Step 2: write the credentials file

One JSON file, mode 600. Credentials never go on a command line here, because
an argument is visible in `ps` to every other user on the host.

```json
{
  "s3": {
    "access_key": "<customer secret key: access key id>",
    "secret_key": "<customer secret key: secret>"
  },
  "elasticsearch": {
    "username": "elastic",
    "password": "<cluster password>"
  }
}
```

```
chmod 600 creds.json
```

The `elasticsearch` section is what the audit itself uses when it asks the
cluster which objects to protect. It is separate from the password file the
test harness uses for its own calls, and deliberately so: the harness never
hands its own credential to the audit.

## Step 3: settings on the cluster

Two settings matter, and one of them will stop the run dead if it is missing.

```yaml
xpack.searchable.snapshot.shared_cache.size: 2gb
```

Frozen indices are mounted as partial searchable snapshots, and without a
shared cache configured the frozen phase fails and ILM stalls. 2gb was enough
for everything below.

The node we measured on ran an 8g heap in a 16Gi container. Smaller will work
for a lower ingest rate. The audit itself runs outside the cluster and its
memory use is discussed under storage below.

Register the repository against the test bucket:

```
PUT _snapshot/leaktest-repo
{
  "type": "s3",
  "settings": {
    "bucket": "es-leak-test",
    "base_path": "leaktest",
    "client": "oci"
  }
}
```

**Register it with verification disabled if verification fails.** On a store
with this fault, repository verification itself tries a batch delete and gets
rejected. That rejection is the first evidence the fault is present, and it is
not a reason to stop:

```
PUT _snapshot/leaktest-repo?verify=false
```

The `client` setting names an `s3.client.*` block in your Elasticsearch
keystore and config holding the endpoint and region. That is standard S3
repository configuration and is not specific to this tool.

## Step 4: the settings we used, and what they cost

The load generator writes documents, lets ILM roll and expire indices, and lets
SLM take and expire snapshots. Every expiry strands objects, because the store
will not delete them. That is the leak, manufactured on purpose.

These are the exact settings behind the published results:

| Setting | Value | Why this value |
|---|---|---|
| `--docs-per-second` | 60 | Fills a shard in seconds, so a snapshot almost never catches an empty one |
| `--doc-bytes` | 1024 | Default. Drives segment and snapshot sizes |
| `--shards` | 2 | Two shard directories per backing index, so the completeness check has something to be incomplete about |
| `--rollover-max-age` | 24h | Holds the run to one backing index |
| `--rollover-max-docs` | 100000000 | Same reason, from the other direction |
| `--frozen-min-age` | 2m | How soon after rollover an index becomes a partial searchable snapshot |
| `--delete-min-age` | 10m | How soon after that ILM deletes it. Must leave the frozen phase time to finish |
| `--snapshot-interval` | 60s | SLM cadence. One leak per interval once retention bites |
| `--retention` | 5m | How long a snapshot lives. Five snapshots alive at once at this cadence |
| `--retention-check-interval` | 5m | How often SLM looks for expired snapshots. The cluster default checks once a day |
| `--ilm-poll-interval` | 10s | The cluster default of 10m would stall every short phase above |

**Why rollover is pinned so high.** Rollover creates a backing index whose
shards are briefly empty, and Elasticsearch writes a shard document listing no
files when it snapshots an empty shard. The parser refuses such a document,
correctly, because a document naming nothing satisfies the subset test against
every directory. One refused document drops its whole shard directory, and that
shortens its snapshot's declared extent, which drops that snapshot's other
directories too. Holding rollover back stops the poison being produced. This is
a change to the test rig and never to what the audit will condemn.

### What it costs in storage

Measured on the published run, which ingested for 89 minutes:

| What | Measured | Note |
|---|---|---|
| Documents written | 320,640 | 60 per second, sustained |
| Bucket space leaked | 270.2 MB | Objects no live snapshot referenced |
| Leak rate | 181.9 MB per hour | At these settings. Scales with ingest rate and snapshot cadence |
| Per snapshot expiry | about 3 MB | One expiry per minute at this cadence |
| Elasticsearch data volume | 1 GiB was sufficient | One backing index, rollover held back |
| Frozen shared cache | 2 GiB | Configured, not measured at capacity |

Budget bucket space for the leak rate times how long you intend to run, and
remember that nothing reclaims it until you run the delete path. A run left
going overnight at these settings would leave about 1.5 GB behind.

The audit holds the repository listing in memory while it works, so its own
memory use scales with object count rather than object size. `--memory-mb` makes
it refuse before reading rather than fail partway through. This is a real
limit and it is tracked as
[issue #7](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/issues/7).

## Step 5: start the load generator

```
python3 snapshot_churn_rig.py run \
  --es https://localhost:9200 \
  --user elastic \
  --password-file ./espw \
  --prefix leaktest \
  --repo-type s3 \
  --bucket es-leak-test \
  --base-path leaktest \
  --s3-client oci \
  --shards 2 \
  --docs-per-second 60 \
  --rollover-max-age 24h \
  --rollover-max-docs 100000000 \
  --frozen-min-age 2m \
  --delete-min-age 10m \
  --snapshot-interval 60s \
  --retention 5m \
  --retention-check-interval 5m \
  --ilm-poll-interval 10s \
  --duration 2h \
  --state-file ./rig-state.json
```

Every argument:

| Argument | What it does |
|---|---|
| `--es` | Cluster URL |
| `--user` | Cluster user for the generator's own calls |
| `--password-file` | Path to that user's password. A path, not the password, because argv is public on the host |
| `--ca-cert` | Certificate authority file, if the cluster uses a private one |
| `--insecure` | Skip certificate verification. For a lab cluster with a self-signed certificate |
| `--prefix` | Name prefix for everything the generator creates, so teardown knows what is its own |
| `--data-stream` | Name of the data stream. Defaults to `<prefix>-stream` |
| `--state-file` | Where the generator records what it created, so teardown can remove exactly that |
| `--repo-type` | `s3` for object storage, `fs` for a shared filesystem |
| `--bucket` | The bucket |
| `--s3-client` | The `s3.client.*` block name in the Elasticsearch keystore |
| `--base-path` | Prefix inside the bucket |
| `--location` | Path, for `--repo-type fs` |
| `--s3-endpoint` | Endpoint, when the generator itself needs to reach the bucket |
| `--s3-region` | Region for that |
| `--s3-access-key` | Access key for that. Prefer leaving it unset |
| `--s3-secret-key-file` | Path to the secret for that |
| `--snapshot-interval` | SLM cadence |
| `--retention` | How long a snapshot lives before SLM expires it |
| `--retention-check-interval` | How often SLM looks for expired snapshots |
| `--slm-cron` | An explicit cron schedule, overriding `--snapshot-interval` |
| `--ilm-poll-interval` | How often ILM evaluates phases |
| `--docs-per-second` | Ingest rate |
| `--doc-bytes` | Approximate size of each document |
| `--shards` | Primary shards per backing index |
| `--rollover-max-age` | Age at which a backing index rolls over |
| `--rollover-max-docs` | Document count that forces a rollover sooner |
| `--frozen-min-age` | How long after rollover an index becomes a partial searchable snapshot |
| `--delete-min-age` | How long after rollover ILM deletes it |
| `--duration` | How long the generator ingests. ILM and SLM keep churning after it exits |
| `--report-interval` | How often it prints a report line |
| `--report-file` | JSONL file the reports append to |

Watch for the line confirming the fault reproduced:

```
store rejected the batch delete inside repository verification, the first
evidence this store leaks deletes
```

If you do not see that, and repository verification succeeded, your store does
not have this fault and there is nothing here for you to reclaim.

Wait for `first_snapshot_expired` to appear in the report file. Until a
snapshot has expired, nothing has leaked and the audit will correctly find
nothing.

## Step 6: look before you touch anything

```
python3 -m generation_chain \
  --transport s3 \
  --endpoint https://<namespace>.compat.objectstorage.<region>.oraclecloud.com \
  --region <region> \
  --bucket es-leak-test \
  --prefix leaktest/ \
  --credentials ./creds.json \
  --elasticsearch https://localhost:9200 \
  --es-repository leaktest-repo \
  --manifest orphans.tsv
```

`generation_chain` is the audit. `generation_chain.reclaim` is the separate
delete tool, it takes a manifest this run produces, and it is not what you want
here.

This reads and reports. It cannot delete: the read path allows `GET`, `HEAD`
and the `POST` that lists a bucket, and refuses anything else at the transport
rather than trusting a caller to behave. The count and reclaimable size print
to your screen as it works, and `--manifest` writes every orphaned object to a
tab separated file.

Confirm the number is not zero and that it grows between runs. That is the leak
accumulating.

## Step 7: run the loop

Copy the configuration template, fill it in, lock it down:

```
cp scripts/test-cycle.conf.example my.conf
chmod 600 my.conf
```

```sh
ENDPOINT="https://<namespace>.compat.objectstorage.<region>.oraclecloud.com"
REGION="<region>"
BUCKET="es-leak-test"
PREFIX="leaktest/"
CREDENTIALS="/path/to/creds.json"
ELASTICSEARCH="https://localhost:9200"
ES_PASSWORD_FILE="/path/to/espw"
REPOSITORY="leaktest-repo"
DATA_STREAM="leaktest-stream"
OUT="./test-cycle-output"
CYCLES=100
MODE="mixed"
SLEEP_BETWEEN=5
SETTLE_TIMEOUT=300
DRY_RUN_ONLY="yes"
```

Then:

```
./scripts/run-test-cycle.sh my.conf
```

**Leave `DRY_RUN_ONLY="yes"` for the first run.** It audits and dry runs and
deletes nothing. Read the output, satisfy yourself that the objects it names
are ones you expect to be dead, and only then set it to `no`.

Every setting in that file:

| Setting | What it does |
|---|---|
| `ENDPOINT` | The S3 compatibility endpoint |
| `REGION` | Region, used for request signing |
| `BUCKET` | The test bucket |
| `PREFIX` | The repository's base path inside it. Keep the trailing slash |
| `CREDENTIALS` | Path to the JSON credentials file |
| `ELASTICSEARCH` | Cluster URL. Optional, but without it the audit cannot see which objects back a mounted searchable snapshot, and deletes are not re-checked against the cluster before they run |
| `ES_PASSWORD_FILE` | Password path for the harness's own calls only. It never reaches the audit |
| `REPOSITORY` | The repository name as Elasticsearch knows it |
| `DATA_STREAM` | The data stream whose shards the segment-mode wait watches |
| `OUT` | Directory for per-cycle output |
| `CYCLES` | How many cycles to run |
| `MODE` | `mixed`, `metadata` or `segment`. See below |
| `SLEEP_BETWEEN` | Seconds between cycles |
| `SETTLE_TIMEOUT` | How long segment mode waits before auditing anyway and recording that it did not settle |
| `DRY_RUN_ONLY` | `yes` audits and dry runs only. `no` really deletes |

The three modes:

- **metadata** audits without waiting. It condemns snapshot documents, index
  metadata and global metadata. It needs only the generation chain, so it works
  against a repository being actively written to.
- **segment** waits until no snapshot is in flight and every primary shard holds
  documents, then audits. It condemns data blobs, which is the path with real
  consequences if it is ever wrong.
- **mixed** alternates, and is the only setting that exercises both.

The harness takes the same arguments directly if you would rather not use the
script. `python3 reclaim_test_protocol.py --help` lists them, and the ones the
script does not expose are `--start` to resume an interrupted run,
`--min-docs-per-shard` and `--fresh-snapshots` to tune the segment-mode wait,
and `--concurrency` to change how many shard documents are read at once.

## Step 8: read the results

```
cd test-cycle-output
awk '/^deleted:/{d+=$2} /^failed:/{f+=$2} /^unconfirmed:/{u+=$2}
     END {printf "deleted=%d failed=%d unconfirmed=%d\n", d, f, u}' exec-*.txt
```

Read totals from the per-cycle execute files rather than from the summary line.
The summary has been wrong before, and the execute files are what actually
happened.

`cycles.tsv` has one row per cycle. The columns that tell you whether the run
was healthy:

- **shards_read** should read `2/2`, or however many shard directories your
  configuration has. Anything less means a shard directory was dropped and the
  segment path condemned less than it could have.
- **failed** and **unconfirmed** should be zero. An unconfirmed delete is one
  the store accepted without the object actually going away, which is the fault
  itself showing up in the delete path.
- **exit** should be zero on every row.

For comparison, the published run: 58 cycles, 888 objects deleted, no failures,
no unconfirmed deletes, every cycle reading 2 of 2, and no non-zero exits.
Metadata cycles took 26 seconds on average and segment cycles 124 seconds.

## Step 9: tear it down

```
python3 snapshot_churn_rig.py teardown \
  --es https://localhost:9200 --user elastic --password-file ./espw \
  --prefix leaktest --state-file ./rig-state.json \
  --repo-type s3 --bucket es-leak-test --base-path leaktest
```

That removes the data stream, index template, ILM policy, snapshots and
repository, and restores the cluster settings it changed. It does not empty the
bucket, because deleting objects is what the store refuses to do. Empty it with
the OCI tooling instead, which uses the native API and works:

```
oci os object bulk-delete --namespace <namespace> --bucket-name es-leak-test --prefix leaktest/
```

Then delete the bucket.

## Running this from GitLab CI

`.gitlab-ci.yml` in this repository does both halves of the above, and keeps
them apart on purpose.

**Which runner.** The Kubernetes executor is the best fit if you have one. Each
job runs as a pod in the cluster, so it reaches Elasticsearch by service DNS
with no ingress to expose, and a Secret mounts at mode `0600`, which is what
these tools ask for anyway. A shell or Docker runner works too, as long as it
can reach both the cluster and the object storage endpoint. A runner outside
the cluster cannot reach a ClusterIP service, and that is the usual reason this
does not work first time.

**Job length.** The qualification job asks for a six hour timeout, because
fifty cycles takes about two and a half hours at the settings above. Check your
instance's maximum job timeout under **Settings, CI/CD, General pipelines**,
and your runner's own timeout, which can be lower and silently wins.

**Variables to create**, under **Settings, CI/CD, Variables**:

| Variable | Type | What it holds |
|---|---|---|
| `CREDS_JSON` | File | The JSON credentials file from step 2 |
| `ES_PASSWORD` | File | The cluster password, on its own |
| `OCI_ENDPOINT` | Variable | Your S3 compatibility endpoint |
| `OCI_REGION` | Variable | Region |
| `OCI_BUCKET` | Variable | The test bucket |
| `OCI_PREFIX` | Variable | Base path, with a trailing slash |
| `ES_URL` | Variable | Cluster URL |
| `ES_REPOSITORY` | Variable | Repository name |
| `ES_DATA_STREAM` | Variable | Data stream name |

Mark both File variables **Masked** and **Protected**. File type matters: it
gives the job a path rather than a value, which is how these tools want a
secret. The pipeline copies each one to mode `600` before use, because GitLab
does not guarantee the mode and the loop script refuses anything looser.

**Schedule the audit, never the rig.** Under **Build, Pipeline schedules**, add
a weekly schedule. `audit:orphans` runs on a schedule and nothing else does:
`qualify:oci` explicitly refuses to run from one. That boundary is the reason a
scheduled audit is safe, and it holds because the scheduled job cannot delete
anything rather than because it has been asked not to.

The audit publishes `orphans.txt` as an artifact with a ninety day expiry, so
you can watch the count between runs. A number that climbs is the leak. A
number that drops after a reclaim is the reclaim working.

**Teardown is not automatic and cannot be.** The rig's state lives inside
Elasticsearch and in your bucket, not in GitLab, so no job cleanup reaches it.
`qualify:oci` tears down in `after_script`, which runs on success, on failure
and on timeout, and `RUNNER_AFTER_SCRIPT_TIMEOUT` is raised because teardown
talks to a cluster and a store. A runner that dies outright skips even that,
which is why `teardown:oci` exists as a manual job using the state file saved
from the run. If a qualification job ends strangely, run it.

A generator that outlives its job keeps ingesting and keeps leaking. That is
the failure worth guarding against here, not a wasted pipeline minute.

**Deletes are off by default.** `DRY_RUN_ONLY` is `yes` in the pipeline
variables. Run it that way first, read what it names, and only then run the
pipeline again with `DRY_RUN_ONLY` set to `no` for that single run.

## What a good result looks like

The tool is behaving if, across a run of fifty cycles or more:

- Every cycle exits zero.
- `failed` and `unconfirmed` are both zero throughout.
- Every segment-mode cycle reads all of its shard directories.
- The read-only report finds objects, and the count drops after a delete run
  and then climbs again as the generator keeps leaking.
- Nothing in your cluster breaks. Snapshots keep succeeding, restores work, and
  frozen indices stay searchable.

The last one is the one that matters. Run a restore afterwards and confirm it
succeeds. A clean sweep that has quietly removed something live would show up
there and nowhere else.

If any of those do not hold, please
[open an issue](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/issues)
with the `cycles.tsv` and the execute file from the cycle that went wrong.
