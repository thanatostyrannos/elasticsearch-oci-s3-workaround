# Generating load, without Kubernetes

To measure anything about a leaking snapshot repository you need a repository
that leaks, which means one that is being written to, snapshotted and expired
continuously. This describes how to make one with a Python script and nothing
else.

`snapshot_churn_rig.py` does the whole job: it creates the index template, data
stream, ILM policy, snapshot repository and SLM policy, then ingests documents
into its own data stream at a rate you choose. **Standard library only, no pod,
no cluster admin beyond the cluster you point it at.** Checked with an AST walk
rather than by reading the imports: it has no third-party dependency at all.

## Getting it: one file

`snapshot_churn_rig.py` is a single file, 52 KB, standard library only. There is
no package to install and no second file to fetch. Checked by copying it alone
into an empty directory and running it, and by walking its imports with an AST
rather than reading them.

If you cannot clone:

```bash
curl -sSLO https://raw.githubusercontent.com/thanatostyrannos/elasticsearch-oci-s3-workaround/main/snapshot_churn_rig.py
python3 snapshot_churn_rig.py run --help
```

Python 3.12 or newer. Nothing else.

That is a deliberate contrast with the audit tool, which is a package of 49
modules under `generation_chain/`. The rig is one file because it has to be
runnable on a box that is not yours, by someone who is reproducing a bug and
does not want to install anything to do it.

## The shortest useful invocation

```bash
python3 snapshot_churn_rig.py run \
  --es https://your-cluster:9200 \
  --user elastic --password-file espw \
  --prefix octest \
  --repo-type s3 --bucket your-bucket --base-path octest \
  --duration 2h
```

The password comes from a file rather than a flag, because a secret in argv is
visible in `ps` to every user on the host.

## What it creates, and why the names matter

Everything is namespaced under `--prefix`:

| Created | Name |
|---|---|
| index template | `<prefix>-template` |
| data stream | `<prefix>-stream` |
| ILM policy | `<prefix>-ilm` |
| snapshot repository | `<prefix>-repo` |
| SLM policy | `<prefix>-slm` |

Nothing else is touched. Teardown removes exactly what it created and restores
the cluster settings it changed.

## Writing into a name you chose

By default the data stream is `<prefix>-stream`. On a cluster where index names
follow a convention this harness does not get to pick, override it:

```bash
python3 snapshot_churn_rig.py run \
  --es https://your-cluster:9200 --user elastic --password-file espw \
  --prefix octest \
  --data-stream team-metrics-loadtest \
  --repo-type s3 --bucket your-bucket --base-path octest \
  --duration 2h
```

**Only the stream name moves.** The repository, ILM policy, SLM policy and index
template still come from `--prefix`, so two rigs writing the same stream name
into different buckets do not collide with each other's policies.

Pass the same `--data-stream` to `status` and `teardown`. They resolve names the
same way, and a teardown that does not know the name will not find the indices
to remove.

The name is validated as an index name before anything is created: 3 to 200
characters, starting with a lowercase letter, holding only lowercase letters,
digits, dot, underscore or hyphen. Elasticsearch refuses anything else, and
refusing early is better than failing halfway through setup.

**The override narrows the teardown scope rather than widening it.** Teardown
takes the backing indices of the stream you named and leaves anything that
merely resembles it. `team-metrics-loadtest-prod-000001` is not touched by a
teardown of `team-metrics-loadtest`, because the rule requires the name to
appear as a whole segment rather than as a leading string.

## Keeping it away from real data

This is the part to check before running it on a cluster that holds anything.

The SLM policy it writes is scoped to its own data stream:

```json
"config": {"indices": ["octest-stream"]}
```

Never `*`. It cannot snapshot an index it did not create, so it will not pull
production data into a test bucket even when sharing a cluster with it.

`--base-path` keeps several rigs apart inside one bucket, so a probe experiment
and a repository under test do not collide.

Teardown refuses to delete an index that merely starts with the prefix, because
an index whose name happens to begin with those characters belongs to someone
else.

## Choosing a rate

Two settings decide how fast garbage appears, and they do different jobs.

**`--docs-per-second`** decides how much data exists. More documents means more
segments, so bigger objects and a bigger repository.

**`--snapshot-interval`** decides how many *operations* happen, and operations
are what leak. Every snapshot delete against a store with this fault leaves its
blobs behind, and leaves a superseded root generation behind as well. This is
the setting that makes a repository accumulate.

```bash
  --docs-per-second 200 \
  --snapshot-interval 30s \
  --retention 5m \
  --delete-min-age 15m
```

`--retention` is what causes deletes: a snapshot older than that is expired by
SLM, and the expiry is the operation that leaks. Without retention you get a
repository that grows and never orphans anything.

Measured on a lab repository, the leak that no tool reclaims comes to roughly
39 KB per operation for the root generation, plus about 2.9 KB for every shard
directory the snapshot touched.

**A production cadence is hourly.** A 30 second interval is 120 times that, and
15 seconds is 240 times. That is deliberate for a lab, and it means wall-clock
numbers taken from a rig are a property of the rig rather than a prediction.

## The trap that wastes an hour

`indices.lifecycle.poll_interval` defaults to ten minutes, so a one minute
`--delete-min-age` does nothing for ten. The harness sets `--ilm-poll-interval`
for you. If you build the policies by hand instead, set it yourself or the fast
lifecycle is fast only on paper.

## Watching it

```bash
python3 snapshot_churn_rig.py status --es ... --prefix octest
```

Safe at any time. `--report-file` appends a JSONL line per interval, which is
the artifact a later measurement cites.

## Stopping

```bash
python3 snapshot_churn_rig.py teardown --es ... --prefix octest
```

Removes what it created, restores the settings it changed, and verifies. Run it
before you try to delete the bucket, because a bucket with objects in it will
refuse to go.

## Every flag

```bash
python3 snapshot_churn_rig.py run --help
```

The methodology behind the design, including why the load runs *during* the
tests rather than only before them, is in
[the churn rig methodology](churn-rig-methodology.md).
