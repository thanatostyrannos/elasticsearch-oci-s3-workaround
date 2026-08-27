---
name: es-snapshot-audit
description: Use when inventorying or sizing an Elasticsearch snapshot repository, answering how fast a repository is growing, auditing what a repository actually holds, separating SLM backup snapshots from frozen-tier searchable-snapshot mounts, planning storage capacity, or establishing a baseline before any snapshot-repository migration.
---

# Elasticsearch snapshot repository audit

Inventory and size a snapshot repository with [`snapshot_sizes.py`](../../snapshot_sizes.py),
keeping the two populations that live in one repository strictly apart:

- SLM backup snapshots are the repository's real *growth*. Retention deletes them
  on a schedule.
- Frozen-tier mount snapshots are pinned by mounted searchable-snapshot indices.
  They are a footprint *floor*, never growth, and never safe to delete.

Averaging those two together is the single most common sizing mistake on a
repository that backs a frozen tier. This skill's job is to stop that from
happening, and to surface the one state that must be fixed before anything
deletes from this repository.

The tool is read-only. It issues `GET` requests and writes nothing to the cluster.

---

## 1. Prerequisites

Generate a read-only API key per the *Authenticating to Elasticsearch with a
read-only API key* section of [README.md](../../README.md): cluster privilege
`monitor_snapshot`, plus `view_index_metadata` on `*`. That second privilege is
not optional. It is what lets `--split-frozen`, `--emit-mounted` and
`--emit-classified` read `index.store.snapshot` off mounted indices. Without it
those modes see no mounts and quietly report a frozen-free repository.

Never put the key on the command line. Read it from a `0600` file:

```bash
export ES_API_KEY="$(cat /path/to/es-snapshot-readonly.key)"
```

Pass `--api-key` alone. `--user` takes precedence when both are set, so a leftover
`--user` silently shadows the key and produces a confusing 401.

Confirm the repository name before you start:

```bash
curl -s --cacert /path/to/ca.crt -H "Authorization: ApiKey $ES_API_KEY" \
  'https://es.example.com:9200/_cat/repositories?v'
```

For TLS, use `--ca-cert /path/to/ca.crt`. `--insecure` disables hostname and
certificate verification entirely; use it only against a lab cluster.

Know what the run costs. `_status` on a completed snapshot reads shard-level
metadata out of the repository, one metadata read per shard per snapshot. Fine on
a daily cadence. Do not cron it every minute against a repository holding
thousands of snapshots. Tune request size with `--batch` (default 20) if the
cluster complains.

---

## 2. Run order

Run these in order. Each one answers a question the next one depends on.

### Step 1: plain report (what is the growth curve?)

```bash
./snapshot_sizes.py --es https://es.example.com:9200 --repo my-repo --group day \
    --api-key "$ES_API_KEY" --ca-cert /path/to/ca.crt
```

`--group` accepts `day`, `week` or `month`. Start at `day`; widen it when a daily
table is too long to read.

### Step 2: class-aware report (which bytes are growth, which are floor?)

```bash
./snapshot_sizes.py --es https://es.example.com:9200 --repo my-repo --group day \
    --split-frozen --api-key "$ES_API_KEY" --ca-cert /path/to/ca.crt
```

`--split-frozen` adds a class column to the per-period table and a class summary
below it. **Always run this on any repository that might back a frozen or cold
tier.** If you do not know whether it does, run it: an empty `frozen-pinned` class
is the answer.

If either discovery fetch fails, the script falls back to the unsplit report,
prints why, and keeps the original frozen-tier warning. A fallback is not a pass.

### Step 3: sizing recommendation

```bash
./snapshot_sizes.py --es https://es.example.com:9200 --repo my-repo \
    --split-frozen --recommend --retention-days 7 \
    --api-key "$ES_API_KEY" --ca-cert /path/to/ca.crt
```

Always pair `--recommend` with `--split-frozen`. Alone, `--recommend` can only
print a warning that the baseline undercounts by the entire frozen footprint.
With the split it prints the footprint as a measured term instead.

### Step 4: machine-readable exports

```bash
# every snapshot, classified: the inventory of record
./snapshot_sizes.py --es https://es.example.com:9200 --repo my-repo \
    --emit-classified --out classified.tsv \
    --api-key "$ES_API_KEY" --ca-cert /path/to/ca.crt

# just the backups, for a sizing spreadsheet
./snapshot_sizes.py --es https://es.example.com:9200 --repo my-repo \
    --emit-classified --class slm --out backups-only.tsv \
    --api-key "$ES_API_KEY" --ca-cert /path/to/ca.crt

# the snapshots a mounted index depends on: the set nobody may delete from
./snapshot_sizes.py --es https://es.example.com:9200 --repo my-repo \
    --emit-mounted --out mounted.txt \
    --api-key "$ES_API_KEY" --ca-cert /path/to/ca.crt
```

`--emit-mounted` and `--emit-classified` are mutually exclusive. `--out` requires
one of them; the human report tables are refused for file redirection on purpose.
`--class` applies only to `--emit-classified` and accepts a comma-separated subset
of `slm`, `frozen-pinned`, `other`.

Without `--out` both emit modes write to stdout and keep diagnostics on stderr, so
`--emit-mounted > mounted.txt` still works. Prefer `--out` when the file is going
to be read as a record of what is mounted. A `>` redirect creates the file whether
or not the command succeeded, and an empty file is the shape a failed export
leaves behind.

**The repository name is not validated.** `--emit-mounted` filters mounts on
`index.store.snapshot.repository_name` matching `--repo` exactly, and repository
names are case sensitive. A name that matches nothing, including a repository that
does not exist, prints `# 0 snapshot(s) ...` and exits 0. Read the name off the
cluster with `GET /_snapshot/_all` rather than typing it, and always check the row
count of the file before you use it:

```bash
grep -cv '^#' mounted.txt      # 0 is a STOP unless you have confirmed no mounts exist
```

---

## 3. How to read the output

### incremental vs total

| Column | Means | Rule |
|---|---|---|
| `added (incremental)` | bytes this snapshot actually uploaded | **This is repository growth.** Summing across snapshots is correct. |
| `largest snapshot (total)` | bytes a snapshot references | **Restore size.** Never sum totals across snapshots: that would count shared segments once per snapshot. |

The repository's true floor is the *union* of all retained snapshots' referenced
bytes. The largest single snapshot's total is a lower bound on that union, which
is why the recommendation uses it as the baseline and nothing stronger.

### The class summary

```
=== Snapshot classes (--split-frozen) ===
  slm               N snapshot(s), incrementals (real repo growth): ...
  frozen-pinned     N snapshot(s), frozen footprint ...
                        partial mounts (frozen tier, shared_cache): ...
                        full mounts (cold tier, full copy)       : ...
  other             N snapshot(s), incrementals: ... (manual / ILM-orphaned)
```

- `slm` snapshots come from an SLM policy. Their summed incrementals are the only
  meaningful growth figure in the report.
- `frozen-pinned` snapshots are pinned by a mounted searchable-snapshot index. The
  incremental is typically **0**, because a regular backup uploads nothing for a
  shard already mounted as a searchable snapshot. The *total* is the footprint.
  `partial: true` mounts are the frozen tier (shared_cache); absent or false is
  the cold tier (full local copy).
- `slm+mounted` is a label, not a bucket: a snapshot that SLM created *and*
  something mounted. It buckets as `frozen-pinned`, because treating it as a
  backup would count its total as growth and imply retention may reap it. It may
  not: deleting it destroys the mounted index.
- `other` means no policy and no mount. Manual snapshots and ILM-orphaned mounts.

The frozen footprint is a **floor**, not a figure. Mounts that share segment
lineage double-count it, and it excludes any blob the repository retains that no
snapshot references. For what the repository actually occupies in the bucket, ask
the store rather than the cluster: `python3 -m generation_chain` classifies every
key it lists under `--prefix`, and where the listing carries sizes it prints a
stored-object size per disposition. The sum of those lines is the repository's
real footprint, and the `orphaned` line is the part a delete would give back.

What that does not hand you is a clean "bytes still reachable" number. `live`
counts only what a run could tie to a surviving snapshot, and `unexplained`
counts what it could not decide either way, which holds live and dead blobs
together. So the audit answers "how big is this, and how much of it is
reclaimable"; it does not answer "how much of it is still needed". For that
question this floor is still what you have.

### Why the frozen warning appears when you do not split

Run `--recommend` without `--split-frozen` and you get:

```
WARNING (precondition): if this repository backs searchable
snapshots (frozen tier), regular snapshots upload ZERO files for
already-mounted indices, so the baseline below UNDERCOUNTS by the
entire frozen footprint ...
```

That is the tool refusing to pretend. Unsplit, it cannot see the mounts, so it
cannot tell whether the number it just printed is short by a few kilobytes or by
several terabytes. Add `--split-frozen` and it prints a NOTE and an arithmetic
term instead. **A recommendation carrying that warning is not a sizing answer; it
is a request to rerun with the split on.**

### The recommendation formula

```
  baseline (largest slm snapshot total)     : ...
  + retention growth (N x median daily)     : ...
  + upgrade-day headroom (1 x baseline)     : ...
  + frozen footprint (pinned mounts)        : ...
  = recommended repository capacity         : ...
  = with +20% operational margin            : ...
```

A p95-based conservative variant prints alongside the median-based one. Only
`SUCCESS` and `PARTIAL` snapshots feed the numbers; `PARTIAL` inclusion is flagged
because failed shards understate real growth. Read the notes the tool prints under
"growth samples". They tell you when the window includes the repository's first
snapshot day (growth overstated) and when an outlier day is present (reindex,
merge, or upgrade traffic).

Elastic publishes no official repository-capacity formula. The combination above
is a heuristic built on documented incremental behavior, and the tool says so in
its own assumptions block. Quote it as a heuristic.

---

## 4. The `--retention-days` policy gate

`--retention-days` accepts **5 to 10 only**, default 7. The argument parser
rejects anything outside that range with an explicit message: site snapshot policy
is 5 to 10 days maximum. If someone asks for a 30-day sizing, the answer is that
the gate exists precisely so that number never quietly becomes the plan.

The recommendation ends with a matching SLM retention block:

```json
"retention": { "expire_after": "7d", "min_count": 5 }
```

Avoid `max_count` here. With multiple snapshots per day (SLM dailies plus ILM
mount snapshots) a count bound can delete snapshots that are still inside the time
window.

---

## 5. The DANGER banner

`--split-frozen` and `--emit-classified` both print this to stderr when a mounted
index pins a snapshot the repository listing no longer contains:

```
!!! DANGER: mounted snapshot(s) MISSING from the repository !!!
```

Elasticsearch does not block deleting a snapshot that backs a mounted
searchable-snapshot index; only repository *unregistration* checks mounts. So the
delete went through, and the index is now serving reads from blobs that no live
snapshot references. On a repository whose deletes leak, those blobs are still
physically present, which is the only reason the index still works.

The blobs that index is reading were named by a snapshot that has been deleted,
which is the shape `python3 -m generation_chain` condemns. A run without the
Elasticsearch veto can put them in its manifest, and deleting them destroys the
index.

Passing `--elasticsearch` and `--es-repository` is what holds them back. The veto
reads `index.store.snapshot.*` off the mounted index itself, so it protects the
snapshot uuid and the index uuid that mount still records, whether or not the
repository still lists that snapshot.

Do not read that as permission to carry on. The veto protects by identity rather
than by understanding: a key the derivation attributed to the wrong snapshot, in
some other index's directory, wears a uuid the cluster is not protecting and goes
through. An index mounted on a snapshot nobody can find is a state to repair, not
one to work around. So:

1. **Reclaim nothing from this repository.** Not with
   `python3 -m generation_chain.reclaim`, not with a raw object-store client.
   Stop.
2. Identify the mounting index from the banner (it names them) or from the
   `mounted_by` column of `--emit-classified`.
3. Either restore or remount that index from a snapshot that still exists, or
   unmount and delete the index if the data is genuinely expendable.
4. Re-run `--emit-classified` and confirm the stderr summary reads
   `0 mounted snapshot(s) MISSING-FROM-CATALOG`.
5. Only then proceed.

`--emit-classified` still writes a row for every missing snapshot (class
`frozen-pinned`, state `MISSING-FROM-CATALOG`, all measured fields `-`), so the
export is complete. The banner prints regardless of any `--class` filter.

If a discovery fetch fails, `--emit-classified` **aborts with exit 1 and writes
nothing**, unlike `--split-frozen`, which falls back. An export missing the mount
linkage would misclassify every pinned snapshot as a plain backup, which is
exactly the mistake the file exists to prevent.

---

## 6. The two classification signals

There are no name heuristics anywhere in this tool. A snapshot called
`frozen-base-metrics` is not frozen because of its name, and a snapshot called
`daily-2026.08.24-xxxx` is not a backup because of its prefix. Two
authoritative signals decide it:

| Signal | Source | Means |
|---|---|---|
| `metadata.policy` | `GET _snapshot/<repo>/*` | SLM stamped its policy id at creation. This is a backup the retention machinery owns. |
| `index.store.snapshot.*` | `GET */_settings` | A searchable-snapshot index records the snapshot backing it in its own settings (`snapshot_name`, `repository_name`, `snapshot_uuid`, `partial`). If any index names a snapshot there, that snapshot is storage a live index reads at query time. |

The tool reads both signals from live cluster state, not from the repository. That
is why the key needs `view_index_metadata`, and why a mount is invisible to
repository metadata alone.

It is also why the mounted-set export exists. `python3 -m generation_chain` asks
the cluster for the same fact itself when you pass `--elasticsearch` and
`--es-repository`, and it refuses the run outright if it asked and got no answer,
so nothing here has to hand it a file. The export is the operator's copy: a
listing you can read, diff against last week's, and put in front of whoever signs
off a delete.

---

## 7. Decision table

| What you want to know | Invocation |
|---|---|
| How fast is this repository growing? | `--group day` (plain report); read `added (incremental)` |
| Same, on a repository with a frozen or cold tier | `--split-frozen --group day`; read the `slm` SUM row |
| How big should the repository be? | `--split-frozen --recommend --retention-days N` |
| How much do I need to buy for *backups only*? | `--split-frozen --recommend`; sum the first three terms, exclude the frozen footprint line |
| How big is the frozen tier? | `--split-frozen`; read the `frozen-pinned` footprint and its partial/full breakdown |
| What is actually in this repository, snapshot by snapshot? | `--emit-classified --out classified.tsv` |
| Which snapshots are backups I could age out? | `--emit-classified --class slm --out backups-only.tsv` |
| Which snapshots must never be deleted? | `--emit-classified --class frozen-pinned --out pinned.tsv` |
| What is unaccounted for (manual, ILM-orphaned)? | `--emit-classified --class other --out other.tsv` |
| Which snapshots are pinned by live mounts? | `--emit-mounted --out mounted.txt` |
| Is anything mounted on a deleted snapshot? | `--emit-classified`; check the stderr summary for `MISSING-FROM-CATALOG` |
| Is it safe to reclaim from this repository? | `--emit-classified`; a single `MISSING-FROM-CATALOG` row is a stop, and §5 is the repair |
| Which snapshots does a mounted index depend on? | `--emit-mounted --out mounted.txt`; the generation-chain audit reads that same fact off the cluster given `--elasticsearch` and `--es-repository` |

---

## 8. Export formats

`--emit-mounted` writes one tab-separated line per pinned snapshot, no header:

```
snapshot_name    snapshot_uuid|-    partial|full    mounting index/indices
```

A snapshot backing both a partial and a full mount reports `partial`: the
frozen-tier reading is the conservative one.

**No current tool consumes this file.** The sweeper that took it as a
`--mounted-snapshots` pre-flight is gone, and `python3 -m generation_chain` reads
the mount linkage from the cluster itself rather than from a file an operator
passes it. So the export is for people: it is the record of which snapshots a
mounted index depends on, and that is the set nobody may delete by any means.

An empty file while frozen indices exist is a **failure, not a pass**. It means
the export could not read index settings, and an empty listing of mounts proves
nothing while looking like it proved something.

`--emit-classified` writes a header row, then one row per snapshot:

```
snapshot  class  policy  tier  mounted_by  state  start_time_utc  incremental_bytes  total_bytes
```

Rows sort by start time then name; `MISSING-FROM-CATALOG` rows have no timestamp
and sort first. The stderr summary always reports every class count plus a
`filtered to:` line, so a `--class` subset file is never mistaken for the whole
repository.

---

## 9. Full flag reference

| Flag | Purpose |
|---|---|
| `--es URL` | required; `http(s)://host:9200` |
| `--repo NAME` | required; repository name |
| `--group day\|week\|month` | report grouping, default `day` |
| `--user user:password` | HTTP basic auth; takes precedence over `--api-key` |
| `--api-key ENCODED` | the `encoded` field from the API key, verbatim |
| `--ca-cert FILE` | CA bundle for https |
| `--insecure` | disable TLS verification (lab only) |
| `--batch N` | snapshots per `_status` request, default 20 |
| `--recommend` | append the repository sizing recommendation |
| `--retention-days N` | retention window for `--recommend`; **5 to 10**, default 7 |
| `--split-frozen` | separate `slm` / `frozen-pinned` / `other` in the report and in `--recommend` |
| `--emit-mounted` | export the pinned-snapshot set as TSV and exit |
| `--emit-classified` | export one classified row per snapshot as TSV and exit |
| `--class NAMES` | restrict `--emit-classified` rows to a subset of `slm,frozen-pinned,other` |
| `--out FILE` | send an emit mode's output to FILE instead of stdout |

---

## 10. Related

- [es-hybrid-migration](../es-hybrid-migration/SKILL.md) is the split-repo
  migration this audit is the first step of.
- [`generation_chain`](../../generation_chain/README.md) is what reads the
  bucket rather than the cluster: it names the objects a delete should have
  removed and did not, and `python3 -m generation_chain.reclaim` is the separate
  command that removes them. This audit is what you run first, because a
  `MISSING-FROM-CATALOG` row is a stop for both.
- The two sweep runbooks that used to be listed here, es-orphan-sweep and
  es-log-cleanup, are removed with the tools they drove, which are also gone.
  Nothing in this repository reads Elasticsearch's failed-delete WARN lines any
  more. See the retirement note in [the main README](../../README.md).

Proof. Procedure: [methodology.md](../../evidence/methodology.md) §3.9 and §4
(steps 0, 1 and 7). Measured outcomes: [campaign-data.md](../../evidence/campaign-data.md) §7 (the
classified inventory, the split-frozen recommendation, and what the split means for
sizing) and Part II's opening, where a `MISSING-FROM-CATALOG` DANGER state
stopped a campaign before it touched anything. That campaign was driven by a tool
since retired; the state it caught, and the reason it is a stop, are unchanged.
