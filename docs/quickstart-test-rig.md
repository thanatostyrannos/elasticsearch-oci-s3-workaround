# Running the test rig against your own cluster

Prove the tool behaves on a repository you can afford to lose, before pointing
it at one you cannot.

The rig is two pieces. A load generator that manufactures a leaking repository
on purpose, and a loop that audits it and reclaims what it finds, over and
over. You need the first only if you have no leaking repository to hand, which
most people testing this do not.

**Read [the read-only quickstart](quickstart-read-only.md) first** if all you
want is a number out of an existing bucket. This page is for the fuller thing.

## What you need

- An Elasticsearch cluster you can throw away. Do not point the load generator
  at production: it writes continuously, rolls indices, and snapshots on a
  cycle measured in seconds.
- A bucket, and a prefix inside it that nothing else uses.
- Python 3.12 or later. Nothing to install.
- An object store that actually reproduces the fault. If yours accepts the
  batch delete, nothing leaks and the rig measures nothing. Oracle Object
  Storage does reproduce it. So does MinIO, but only up to and including
  `RELEASE.2025-01-18T00-31-37Z`; the next release accepts the request and the
  fault disappears.

## Step 1: the load generator

`snapshot_churn_rig.py` builds everything it needs and names all of it with
one prefix, so teardown can find it again. Give it a prefix nothing else uses.

```bash
python3 snapshot_churn_rig.py run \
  --es http://your-cluster:9200 \
  --user elastic --password-file /path/to/espw \
  --prefix rig1 --data-stream rig1-stream \
  --repo-type s3 --bucket your-bucket --base-path rig1 --s3-client default \
  --shards 2 --docs-per-second 60 --doc-bytes 512 \
  --snapshot-interval 60s --retention 5m --delete-min-age 10m \
  --ilm-poll-interval 10s --duration 8h \
  --report-file ./rig1-reports.jsonl --state-file ./rig1-state.json
```

The password is a **path**, never a value. A secret in an argument is visible
in `ps` to every user on the host.

It creates `rig1-repo`, `rig1-ilm`, `rig1-stream`, `rig1-template` and
`rig1-slm`, and refuses to start if that prefix already matches anything.

### The two flags people get wrong

`--ilm-poll-interval 10s` matters more than it looks. Elasticsearch checks
lifecycle policies every ten minutes by default, so a policy with a one minute
`min_age` does nothing for ten. Without this the rig is fast only on paper.

`--snapshot-interval` and `--retention` together decide whether anything leaks
at all. Retention has to expire snapshots inside your observation window: a
five minute retention with a one minute snapshot cycle starts orphaning blobs
within about six minutes. A one hour retention on a twenty minute test
produces nothing, and a clean-looking result that measured nothing.

### Check it is actually working

Two things, by count, not by assumption.

```bash
tail -1 rig1-reports.jsonl | python3 -m json.tool | grep -E 'expired|taken|object_count'
```

`expired_total` must climb above zero. Until it does, no snapshot has been
deleted, so nothing has leaked and there is nothing to find.

And confirm the fault reproduces, which the rig logs at registration:

```
store rejected the batch delete inside repository verification, the first
evidence this store leaks deletes; registering rig1-repo with verify=false
and continuing
```

If you do not see that, your store accepts the batch delete and does not have
the bug this tool exists for.

### Pointing it at a specific index instead

If you already have a data stream or index you want snapshotted, skip the load
generator and register the repository yourself, then run the loop against it.
The rig is only a way to manufacture the condition. Nothing downstream depends
on the data having come from it.

## Step 2: the loop

```bash
cp scripts/test-cycle.conf.example my.conf
chmod 600 my.conf
$EDITOR my.conf
./scripts/run-test-cycle.sh my.conf
```

It starts in `DRY_RUN_ONLY="yes"`. Leave it there for the first run. The loop
audits, dry runs, and stops short of deleting, which exercises everything
except the irreversible part.

The script checks before it starts: that the config and credentials files are
not readable by other users, that the credentials file has the sections the
run needs, that the cluster answers, and that the endpoint is not plain http
to somewhere off your machine. Each of those otherwise fails later with less
to go on.

### Reading the output

`cycles.tsv` has a row per cycle:

```
cycle  utc  mode  settle  shards_read  segments_condemned  deleted  failed
unconfirmed  reclaimable  exit
```

- **exit** other than 0 means the [audit](../README.md#what-audit-means-here) did not run. The loop stops on it.
  The reason is in `derive-<n>.txt`.
- **shards_read** as `16 of 52` is normal. A shard directory whose current
  document cannot be read is dropped whole, and so are the rest of that
  snapshot's directories. Fewer directories read means a shorter manifest,
  never a wrong one.
- **failed** or **unconfirmed** above zero stops the run. That is the safety
  stop, and it means the repository had a problem worth looking at.

Read totals from the per-cycle files, not from `cycles.tsv`:

```bash
awk '/^deleted:/{d+=$2} /^failed:/{f+=$2} /^unconfirmed:/{u+=$2} \
     END{printf "deleted=%d failed=%d unconfirmed=%d\n",d,f,u}' out/exec-*.txt
```

The summary file has disagreed with the execute files before, and every time
the execute files were right.

### Turning deletes on

Only after a dry run has completed cleanly, and only on a repository you can
afford to lose. Set `DRY_RUN_ONLY="no"`. The script says what it is about to
do and waits ten seconds.

Each cycle then audits, dry runs, and executes against the digest that dry run
printed. If the manifest changed in between, the approval no longer matches
and the cycle fails rather than deleting under a stale approval.

## Step 3: expect it to get slower

The audit reads one shard document per shard directory per generation, and
nothing ever removes a generation, so a repository that has been leaking for a
while costs more to read than a fresh one. A run that starts at ten seconds a
cycle can reach ten minutes a cycle by cycle fifty.

That is issue #9 and it is a property of the fault, not of the tool. It is
also why a hundred-cycle run should start from a repository you just created
rather than one carried over from yesterday: starting fresh keeps the
generation count, and so the per-cycle cost, comparable across runs. A rig
inherited from a previous session already carries whatever generations it
built up, so its timings are not a clean baseline for the next comparison.

## Step 4: tear it down

```bash
python3 snapshot_churn_rig.py teardown \
  --es http://your-cluster:9200 --user elastic --password-file /path/to/espw \
  --prefix rig1 --state-file ./rig1-state.json \
  --bucket your-bucket --purge-bucket
```

Teardown is the end of a test, not something to do at the start of the next
one. A cycle that cleans up after itself leaves the next run nothing to wait
for.

If the bucket is Terraform-managed, `terraform destroy` empties it as part of
the destroy rather than one object at a time.
