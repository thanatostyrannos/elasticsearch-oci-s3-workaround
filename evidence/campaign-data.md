# EVIDENCE: raw data appendix for the E2E validation


> [!NOTE]
> **The sweep runs described here were driven by tools that are now retired.**
> `s3_repo_sweeper.py`, `oci_repo_sweeper.py` and `es_log_driven_sweeper.py`
> have been removed; see
> [the main README](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/README.md#the-three-tools-that-were-removed).
>
> The measurements are kept because they are not measurements of those tools.
> What a wrong delete costs, what a mounted searchable snapshot is linked to,
> what a restore returns and what `_verify_integrity` fails to notice are
> properties of Elasticsearch and the object store. The cost is the same
> whichever tool made the delete.
>
> Read the classification decisions and command invocations as history of
> retired tooling. Read the consequences as current.
This is the receipts file. Every number below is copied from a file on disk or
from a command run against the live rig, the local test lab that reproduces the
fault (defined in [FACTS.md](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/FACTS.md#the-test-lab-henceforth-the-rig)); nothing here is summarized, rounded for
effect, or reconstructed from memory. Where a source is missing or a value is not
what it appears to be, that is stated inline rather than smoothed over.

Companion documents: `test-results.md` (what passed), `methodology.md` (how the
rig was built and what the test layers cover). This file is only the data.

## Two campaigns

The evidence here comes from two separate end-to-end runs against the same class
of rig. They are kept apart on purpose: the second one changes the storage
topology, so mixing their numbers would be meaningless.

| | **Campaign 1: single-repo leak reproduction** | **Campaign 2: split-repo hybrid (Strategy D)** |
|---|---|---|
| Sections | §1 to §6 | §7 to §15 |
| Topology | one S3 repository (`oci-repro`) doing backups *and* frozen mounts | backups on a filesystem repository (`backups-fs`), frozen mounts left on S3 (`oci-repro`) |
| Question asked | when `DeleteObjects` is rejected, what leaks, and can the pod log reconstruct it? | does moving backup retention off the leaking store stop the leak, and can the residue still be swept safely? |
| Repository generation at audit | 3 | 9 |
| Orphans found | 28 (378,142 B) | 57 total; 53 swept, 4 residual (3,611 B) |
| Log-driven recall (keys) | 20 of 28, 71.4% | 53 of 57, 93.0% |
| Rig endpoint | `http://localhost:9200` | `http://localhost:9202` |
| Raw artifacts | session scratch directory, root level | session scratch directory, `hybrid/` subdirectory |

Section numbering is continuous across both parts so that every cross-reference
in this file (`§1a`, `§3c`, `§13b`, …) resolves without ambiguity.

Values marked **re-verified at write time** were re-read from the still-running
rig, or recomputed by re-running a read-only tool against a captured mirror, at
the moment this document was written, not copied from the capture.

---

# Part I. Campaign 1: single-repo leak reproduction

**Source artifacts** captured during the campaign-1 E2E run (session scratch files, not committed; the repo files they validate were `oci_repo_sweeper.py` and `es_log_driven_sweeper.py`, both since retired and removed, plus [`tests/fixtures/`](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/tree/main/tests/fixtures), which is still here):

| Artifact | Size | mtime (local, UTC-7) | What it is |
|---|---:|---|---|
| `repo-mirror/` | 67 files, 1,120,400 B | 2026-08-24 12:50:50 | pre-sweep mirror of the rig bucket |
| `repo-mirror2/` | 39 files, 742,258 B | 2026-08-24 12:54:36 | post-sweep mirror (live + protected blobs only) |
| `orphans.tsv` | 28 data rows | 2026-08-24 12:50:58 | reachability sweeper dry-run manifest |
| `log-manifest.tsv` | 20 data rows | 2026-08-24 12:53:52 | log-driven sweeper manifest (post-dedup) |
| `es-pod.log` | 465 lines, 268,989 B | 2026-08-24 12:50:33 | raw ES pod log (ECS JSON, one object per line) |
| `sweeper-stderr.txt` | - | 2026-08-24 12:50 | reachability sweeper console summary |
| `lg.err` | - | 2026-08-24 12:53 | log sweeper console summary, **post-dedup run** (matches the shipped manifest) |
| `log-sweeper-stderr.txt` | - | 2026-08-24 12:51 | log sweeper console summary, earlier pre-dedup run (see caveat in §6) |
| `log-keys.txt` | 20 lines | 2026-08-24 12:53 | bare key list, one per line, from the post-dedup run |

The MinIO credentials and the ES `elastic` password live in files with mode `0600`
in that directory and are deliberately not reproduced anywhere in this document.

---

## 1. Repository anatomy at audit time

"Audit time" is the moment the reachability sweeper listed the bucket: repository
generation 3, two live snapshots, five live indices, 67 objects totalling
1,120,400 bytes (1.07 MiB).

The sweeper's own classification of those 67 objects, verbatim from
`sweeper-stderr.txt`:

```
INFO listing repository objects...
INFO 67 objects, 1.1 MiB
INFO loading root state (index.latest + index-N)...
INFO generation 3: 2 live snapshots, 5 live indices
class         objects          bytes
LIVE               36      721.9 KiB
ORPHAN             28      369.3 KiB
PROTECTED           3        2.9 KiB
```

`LIVE + PROTECTED = 39`, which is exactly the file count of `repo-mirror2/`. The
28 ORPHAN keys are exactly the set difference `repo-mirror/` minus `repo-mirror2/`,
verified by set comparison, zero keys in either direction outside that
difference, and byte totals reconcile: `742,258 + 378,142 = 1,120,400`.

The three PROTECTED objects are `index-2` (previous root generation, held back by
the sweeper's N-1 guard) and the two repository-verification scratch blobs under
`tests-pL1PTmvUTFm-aV8dyaqcaw/`.

### 1a. Index UUID → index name

Derived from `repo-mirror2/index-3`, field `indices.<name>.id`. Cross-checked
against the `index` blocks returned by `POST /_snapshot/oci-repro/_verify_integrity`
in §5, which report the same five UUID/name pairs.

| Index UUID | Index name | Shard generation (live, gen 3) |
|---|---|---|
| `FKf_36IxTWyQnF1j3fme5A` | `logs-app` | `XFd_ujjMTaGqEhHi5SZPrg` |
| `BtIUzj_XSz-Ygq0AfcOMIg` | `metrics-sys` | `gW2D_OyTQUOtpbRGh-FDqA` |
| `uWYEV8XgTRq9NPAtuedM9Q` | `.security-7` | `SaAVz3TUQxmdIFF0FUuMvA` |
| `-6gHKxGPT_-EZY3_ShXJEg` | `.ds-ilm-history-7-2026.08.24-000001` | `5-eGzXmZSEymyZgowVVf7w` |
| `GVsRrzdESB-K1azSkyk4fA` | `.ds-.logs-elasticsearch.deprecation-default-2026.08.24-000001` | `6m36_dxXREWZnQ3gFbyPOA` |

Only two of the five are user indices. The other three are system/data-stream
indices that got swept into the snapshot because `include_global_state: true`.
They matter here because three of the eight log-invisible orphans (§3) live in
two of them.

### 1b. Live vs orphan, per shard directory

Live counts and bytes are a walk of `repo-mirror2/`; orphan counts and bytes are
an aggregation of `orphans.tsv`. `(meta)` rows are the per-index
`indices/<uuid>/meta-*.dat` metadata blobs that sit above the shard directory.

| Path group | Index name | Live objs | Live bytes | Orphan objs | Orphan bytes | Orphan % of bytes |
|---|---|---:|---:|---:|---:|---:|
| `(repo root)` | - | 7 | 83,861 | 4 | 43,300 | 34.0% |
| `indices/FKf_36IxTWyQnF1j3fme5A/0` | `logs-app` | 5 | 289,672 | 6 | 161,851 | 35.9% |
| `indices/FKf_36IxTWyQnF1j3fme5A/(meta)` | `logs-app` | 1 | 533 | 0 | 0 | 0% |
| `indices/BtIUzj_XSz-Ygq0AfcOMIg/0` | `metrics-sys` | 5 | 290,263 | 6 | 162,451 | 35.9% |
| `indices/BtIUzj_XSz-Ygq0AfcOMIg/(meta)` | `metrics-sys` | 1 | 538 | 0 | 0 | 0% |
| `indices/uWYEV8XgTRq9NPAtuedM9Q/0` | `.security-7` | 5 | 44,882 | 4 | 3,509 | 7.3% |
| `indices/uWYEV8XgTRq9NPAtuedM9Q/(meta)` | `.security-7` | 1 | 2,814 | 0 | 0 | 0% |
| `indices/-6gHKxGPT_-EZY3_ShXJEg/0` | `.ds-ilm-history-7-…` | 5 | 13,454 | 4 | 3,490 | 20.6% |
| `indices/-6gHKxGPT_-EZY3_ShXJEg/(meta)` | `.ds-ilm-history-7-…` | 1 | 729 | 0 | 0 | 0% |
| `indices/GVsRrzdESB-K1azSkyk4fA/0` | `.ds-.logs-…deprecation-…` | 5 | 14,399 | 4 | 3,541 | 19.7% |
| `indices/GVsRrzdESB-K1azSkyk4fA/(meta)` | `.ds-.logs-…deprecation-…` | 1 | 1,069 | 0 | 0 | 0% |
| `tests-pL1PTmvUTFm-aV8dyaqcaw/` (protected) | - | 2 | 44 | 0 | 0 | 0% |
| **TOTAL** | | **39** | **742,258** | **28** | **378,142** | **33.8%** |

Every one of the five index directories carries orphans, and no `meta-*.dat`
index-metadata blob is orphaned. The leak is confined to shard-level and
root-level state, which is what you would expect when the failing operation is
"delete the blobs the previous generation no longer references."

### 1c. Orphans by reason code

| Reason (sweeper's classification) | Objects | Bytes | % of orphan bytes |
|---|---:|---:|---:|
| `segment vs current shard file set` | 4 | 315,897 | 83.5% |
| `root meta-<uuid> vs live snapshots` | 1 | 38,805 | 10.3% |
| `shard index-<gen> vs current gen` | 15 | 14,563 | 3.9% |
| `shard snap-<uuid> vs live snapshots` | 5 | 4,382 | 1.2% |
| `stale root generation` | 2 | 4,027 | 1.1% |
| `root snap-<uuid> vs live snapshots` | 1 | 468 | 0.1% |
| **TOTAL** | **28** | **378,142** | **100%** |

Four segment blobs are 83.5% of the wasted bytes. Fifteen shard `index-<gen>`
blobs are 3.9%. Object count and byte cost point in opposite directions here, and
that gap is the single most load-bearing fact in this document: it is why the
partial recovery in §3 loses far more space than its 20/28 hit rate suggests.

---

## 2. The full orphan manifest (28 rows)

All 28 rows of `orphans.tsv`, in file order, with the index UUID resolved to a
name using the mapping in §1a. Byte values are exact.

**Timestamp caveat, stated up front:** the reachability sweeper was pointed at the
local mirror of the rig bucket, so `created` / `last_modified` / `last_accessed`
are the mirror copy's filesystem times (all inside a 38 ms window at
`19:50:50.93` to `19:50:50.97` UTC), not the original object timestamps in MinIO.
The real writes happened between `19:50:11` and `19:50:25` UTC per `es-pod.log`.
These columns are reproduced because they are what the manifest actually contains;
they are not usable evidence for any age-based retention policy, and no claim
in this document depends on them.

| # | Index | Shard | Blob | Size (B) | created (mirror fs) | last_modified (mirror fs) | Reason |
|---:|---|---|---|---:|---|---|---|
| 1 | `(repo root)` | - | `index-0` | 1,693 | 2026-08-24T19:50:50.939931Z | 2026-08-24T19:50:50.939931Z | stale root generation |
| 2 | `(repo root)` | - | `meta-Hrv33udFQNSFJK1cLTdQVA.dat` | 38,805 | 2026-08-24T19:50:50.973520Z | 2026-08-24T19:50:50.973520Z | root meta-\<uuid\> vs live snapshots |
| 3 | `(repo root)` | - | `snap-Hrv33udFQNSFJK1cLTdQVA.dat` | 468 | 2026-08-24T19:50:50.973520Z | 2026-08-24T19:50:50.973520Z | root snap-\<uuid\> vs live snapshots |
| 4 | `(repo root)` | - | `index-1` | 2,334 | 2026-08-24T19:50:50.935732Z | 2026-08-24T19:50:50.935732Z | stale root generation |
| 5 | `metrics-sys` | 0 | `index-gw8nXlCFQ0eFJcIT1StZsw` | 1,236 | 2026-08-24T19:50:50.948328Z | 2026-08-24T19:50:50.948328Z | shard index-\<gen\> vs current gen |
| 6 | `metrics-sys` | 0 | `index-rBUHMFb9T9WUFmqasJEx2Q` | 1,230 | 2026-08-24T19:50:50.948328Z | 2026-08-24T19:50:50.948328Z | shard index-\<gen\> vs current gen |
| 7 | `metrics-sys` | 0 | `snap-Hrv33udFQNSFJK1cLTdQVA.dat` | 871 | 2026-08-24T19:50:50.952527Z | 2026-08-24T19:50:50.948328Z | shard snap-\<uuid\> vs live snapshots |
| 8 | `metrics-sys` | 0 | `__hbLf4fSvQoCGDy6kH-Q0LA` | 742 | 2026-08-24T19:50:50.948328Z | 2026-08-24T19:50:50.948328Z | segment vs current shard file set |
| 9 | `metrics-sys` | 0 | `index-aY9-HJY-TcK9bw81coxYpQ` | 861 | 2026-08-24T19:50:50.952527Z | 2026-08-24T19:50:50.952527Z | shard index-\<gen\> vs current gen |
| 10 | `metrics-sys` | 0 | `__BmG7ZoTQT6WkC5r4tx1zeg` | 157,511 | 2026-08-24T19:50:50.952527Z | 2026-08-24T19:50:50.952527Z | segment vs current shard file set |
| 11 | `.security-7` | 0 | `index-zpLksOM6RD2ThXq8X7Gl7A` | 868 | 2026-08-24T19:50:50.969322Z | 2026-08-24T19:50:50.969322Z | shard index-\<gen\> vs current gen |
| 12 | `.security-7` | 0 | `index-Azf2dsfRSGuxfHb5W-VJxg` | 884 | 2026-08-24T19:50:50.969322Z | 2026-08-24T19:50:50.969322Z | shard index-\<gen\> vs current gen |
| 13 | `.security-7` | 0 | `snap-Hrv33udFQNSFJK1cLTdQVA.dat` | 878 | 2026-08-24T19:50:50.973520Z | 2026-08-24T19:50:50.973520Z | shard snap-\<uuid\> vs live snapshots |
| 14 | `.security-7` | 0 | `index-xyv9sr06TqKFFsDWJkA1JA` | 879 | 2026-08-24T19:50:50.969322Z | 2026-08-24T19:50:50.969322Z | shard index-\<gen\> vs current gen |
| 15 | `.ds-.logs-…deprecation-…` | 0 | `index-VgbkC_G7QCO5_Y1QxLMTYA` | 875 | 2026-08-24T19:50:50.965123Z | 2026-08-24T19:50:50.965123Z | shard index-\<gen\> vs current gen |
| 16 | `.ds-.logs-…deprecation-…` | 0 | `index--Ro_2MZMR8WL2kvnKeoQ1Q` | 886 | 2026-08-24T19:50:50.960924Z | 2026-08-24T19:50:50.960924Z | shard index-\<gen\> vs current gen |
| 17 | `.ds-.logs-…deprecation-…` | 0 | `index-7fkmGhZlTyqWlV47o5uuZg` | 891 | 2026-08-24T19:50:50.960924Z | 2026-08-24T19:50:50.960924Z | shard index-\<gen\> vs current gen |
| 18 | `.ds-.logs-…deprecation-…` | 0 | `snap-Hrv33udFQNSFJK1cLTdQVA.dat` | 889 | 2026-08-24T19:50:50.965123Z | 2026-08-24T19:50:50.965123Z | shard snap-\<uuid\> vs live snapshots |
| 19 | `logs-app` | 0 | `index-N_uNxlVyQMOLAbNX0bCPjw` | 1,235 | 2026-08-24T19:50:50.956726Z | 2026-08-24T19:50:50.956726Z | shard index-\<gen\> vs current gen |
| 20 | `logs-app` | 0 | `index-0NOPErF9Ts6CjVx4gtKF_A` | 1,240 | 2026-08-24T19:50:50.956726Z | 2026-08-24T19:50:50.956726Z | shard index-\<gen\> vs current gen |
| 21 | `logs-app` | 0 | `index-LNACpd7OQHmhoabvrldQBQ` | 861 | 2026-08-24T19:50:50.956726Z | 2026-08-24T19:50:50.956726Z | shard index-\<gen\> vs current gen |
| 22 | `logs-app` | 0 | `snap-Hrv33udFQNSFJK1cLTdQVA.dat` | 871 | 2026-08-24T19:50:50.960924Z | 2026-08-24T19:50:50.956726Z | shard snap-\<uuid\> vs live snapshots |
| 23 | `logs-app` | 0 | `__1ugMrX0TQ5-j4F7fz3bAXQ` | 156,902 | 2026-08-24T19:50:50.956726Z | 2026-08-24T19:50:50.952527Z | segment vs current shard file set |
| 24 | `logs-app` | 0 | `__sBR1r9ldTP2rSNzYGwefrg` | 742 | 2026-08-24T19:50:50.952527Z | 2026-08-24T19:50:50.952527Z | segment vs current shard file set |
| 25 | `.ds-ilm-history-7-…` | 0 | `index-CMpZ1xhhSZOASrfxdJCDWA` | 880 | 2026-08-24T19:50:50.944130Z | 2026-08-24T19:50:50.944130Z | shard index-\<gen\> vs current gen |
| 26 | `.ds-ilm-history-7-…` | 0 | `index-q077nLXoQmGQbKinKyFWAg` | 863 | 2026-08-24T19:50:50.944130Z | 2026-08-24T19:50:50.944130Z | shard index-\<gen\> vs current gen |
| 27 | `.ds-ilm-history-7-…` | 0 | `snap-Hrv33udFQNSFJK1cLTdQVA.dat` | 873 | 2026-08-24T19:50:50.948328Z | 2026-08-24T19:50:50.948328Z | shard snap-\<uuid\> vs live snapshots |
| 28 | `.ds-ilm-history-7-…` | 0 | `index-2D_bWIlxRQKzyn4ySZ-5AA` | 874 | 2026-08-24T19:50:50.944130Z | 2026-08-24T19:50:50.944130Z | shard index-\<gen\> vs current gen |

Two names truncated for table width: `.ds-.logs-…deprecation-…` is
`.ds-.logs-elasticsearch.deprecation-default-2026.08.24-000001`, and
`.ds-ilm-history-7-…` is `.ds-ilm-history-7-2026.08.24-000001`. Full UUID paths
for all 28 keys appear in §3's tables and in `orphans.tsv` itself.

Every `snap-*.dat` and `meta-*.dat` orphan carries the same snapshot UUID,
`Hrv33udFQNSFJK1cLTdQVA`, which is `snap-1`, the snapshot deleted at
`19:50:25.098Z`. It appears once at the repo root and once per index shard, six
times in total, which is the shape you get when a snapshot delete cleans the
index but fails to clean the blobs.

---

## 3. Log-condemned keys and the cross-check

### 3a. The 20 keys the log-driven sweeper condemned

From `log-manifest.tsv`. The `size_bytes` / `created` / `last_modified` /
`last_accessed` columns in that file are all literal `-`: the log-driven path
never lists the bucket, it only reads the pod log, so it genuinely does not know
how big any of these blobs are. Those columns are omitted here rather than
padded with values borrowed from the other manifest.

| # | Key | In orphan set? | first_seen_in_logs | last_seen_in_logs | Source |
|---:|---|---|---|---|---|
| 1 | `index-0` | yes | 2026-08-24T19:50:24.635Z | 2026-08-24T19:50:25.098Z | sdk-exception,stale-root |
| 2 | `index-1` | yes | 2026-08-24T19:50:24.871Z | 2026-08-24T19:50:25.098Z | sdk-exception,stale-root |
| 3 | `indices/-6gHKxGPT_-EZY3_ShXJEg/0/index-2D_bWIlxRQKzyn4ySZ-5AA` | yes | 2026-08-24T19:50:24.871Z | 2026-08-24T19:50:24.871Z | sdk-exception |
| 4 | `indices/-6gHKxGPT_-EZY3_ShXJEg/0/index-q077nLXoQmGQbKinKyFWAg` | yes | 2026-08-24T19:50:24.635Z | 2026-08-24T19:50:24.635Z | sdk-exception |
| 5 | `indices/BtIUzj_XSz-Ygq0AfcOMIg/0/index-aY9-HJY-TcK9bw81coxYpQ` | yes | 2026-08-24T19:50:24.635Z | 2026-08-24T19:50:24.635Z | sdk-exception |
| 6 | `indices/BtIUzj_XSz-Ygq0AfcOMIg/0/index-rBUHMFb9T9WUFmqasJEx2Q` | yes | 2026-08-24T19:50:24.871Z | 2026-08-24T19:50:24.871Z | sdk-exception |
| 7 | `indices/FKf_36IxTWyQnF1j3fme5A/0/__1ugMrX0TQ5-j4F7fz3bAXQ` | yes | 2026-08-24T19:50:25.098Z | 2026-08-24T19:50:25.098Z | sdk-exception |
| 8 | `indices/FKf_36IxTWyQnF1j3fme5A/0/__sBR1r9ldTP2rSNzYGwefrg` | yes | 2026-08-24T19:50:25.098Z | 2026-08-24T19:50:25.098Z | sdk-exception |
| 9 | `indices/FKf_36IxTWyQnF1j3fme5A/0/index-0NOPErF9Ts6CjVx4gtKF_A` | yes | 2026-08-24T19:50:25.098Z | 2026-08-24T19:50:25.098Z | sdk-exception |
| 10 | `indices/FKf_36IxTWyQnF1j3fme5A/0/index-LNACpd7OQHmhoabvrldQBQ` | yes | 2026-08-24T19:50:24.635Z | 2026-08-24T19:50:25.098Z | sdk-exception |
| 11 | `indices/FKf_36IxTWyQnF1j3fme5A/0/index-N_uNxlVyQMOLAbNX0bCPjw` | yes | 2026-08-24T19:50:24.871Z | 2026-08-24T19:50:25.098Z | sdk-exception |
| 12 | `indices/FKf_36IxTWyQnF1j3fme5A/0/snap-Hrv33udFQNSFJK1cLTdQVA.dat` | yes | 2026-08-24T19:50:25.098Z | 2026-08-24T19:50:25.098Z | sdk-exception |
| 13 | `indices/GVsRrzdESB-K1azSkyk4fA/0/index--Ro_2MZMR8WL2kvnKeoQ1Q` | yes | 2026-08-24T19:50:24.871Z | 2026-08-24T19:50:25.098Z | sdk-exception |
| 14 | `indices/GVsRrzdESB-K1azSkyk4fA/0/index-7fkmGhZlTyqWlV47o5uuZg` | yes | 2026-08-24T19:50:25.098Z | 2026-08-24T19:50:25.098Z | sdk-exception |
| 15 | `indices/GVsRrzdESB-K1azSkyk4fA/0/index-VgbkC_G7QCO5_Y1QxLMTYA` | yes | 2026-08-24T19:50:24.635Z | 2026-08-24T19:50:25.098Z | sdk-exception |
| 16 | `indices/GVsRrzdESB-K1azSkyk4fA/0/snap-Hrv33udFQNSFJK1cLTdQVA.dat` | yes | 2026-08-24T19:50:25.098Z | 2026-08-24T19:50:25.098Z | sdk-exception |
| 17 | `indices/uWYEV8XgTRq9NPAtuedM9Q/0/index-xyv9sr06TqKFFsDWJkA1JA` | yes | 2026-08-24T19:50:24.871Z | 2026-08-24T19:50:24.871Z | sdk-exception |
| 18 | `indices/uWYEV8XgTRq9NPAtuedM9Q/0/index-zpLksOM6RD2ThXq8X7Gl7A` | yes | 2026-08-24T19:50:24.635Z | 2026-08-24T19:50:24.635Z | sdk-exception |
| 19 | `meta-Hrv33udFQNSFJK1cLTdQVA.dat` | yes | 2026-08-24T19:50:25.098Z | 2026-08-24T19:50:25.098Z | sdk-exception |
| 20 | `snap-Hrv33udFQNSFJK1cLTdQVA.dat` | yes | 2026-08-24T19:50:25.098Z | 2026-08-24T19:50:25.098Z | sdk-exception |

Two keys carry a second source, `stale-root`. That is the INFO line at
`19:50:25.093Z` quoted in §4c, which names `index-0` and `index-1` explicitly.
Every other key comes only from the SdkException stack traces.

### 3b. The cross-check

Set arithmetic over the two manifests:

| Quantity | Keys | Bytes |
|---|---:|---:|
| Reachability sweeper (`orphans.tsv`) | 28 | 378,142 |
| Log-driven sweeper (`log-manifest.tsv`) | 20 | (unknown to that tool; 214,267 per `orphans.tsv`) |
| In log manifest but **not** an orphan (false positives) | **0** | 0 |
| In orphan set but **missed** by logs | **8** | **163,875** |

**Zero false positives.** The 20 log-condemned keys are a strict subset of the 28.
Nothing the logs named was actually live. That is the safety property that
matters: a log-driven sweep run in `--execute` mode against this repository would
not have deleted a reachable blob.

Recall is the weak half. The logs recovered 20 of 28 keys (71.4%) but only
214,267 of 378,142 bytes (56.7%), because one of the eight misses is the single
largest orphan in the repository.

### 3c. The 8 orphans the logs missed

| # | Index | Key | Size (B) | Reason |
|---:|---|---|---:|---|
| 1 | `.ds-ilm-history-7-2026.08.24-000001` | `indices/-6gHKxGPT_-EZY3_ShXJEg/0/index-CMpZ1xhhSZOASrfxdJCDWA` | 880 | shard index-\<gen\> vs current gen |
| 2 | `.ds-ilm-history-7-2026.08.24-000001` | `indices/-6gHKxGPT_-EZY3_ShXJEg/0/snap-Hrv33udFQNSFJK1cLTdQVA.dat` | 873 | shard snap-\<uuid\> vs live snapshots |
| 3 | `metrics-sys` | `indices/BtIUzj_XSz-Ygq0AfcOMIg/0/__BmG7ZoTQT6WkC5r4tx1zeg` | 157,511 | segment vs current shard file set |
| 4 | `metrics-sys` | `indices/BtIUzj_XSz-Ygq0AfcOMIg/0/__hbLf4fSvQoCGDy6kH-Q0LA` | 742 | segment vs current shard file set |
| 5 | `metrics-sys` | `indices/BtIUzj_XSz-Ygq0AfcOMIg/0/index-gw8nXlCFQ0eFJcIT1StZsw` | 1,236 | shard index-\<gen\> vs current gen |
| 6 | `metrics-sys` | `indices/BtIUzj_XSz-Ygq0AfcOMIg/0/snap-Hrv33udFQNSFJK1cLTdQVA.dat` | 871 | shard snap-\<uuid\> vs live snapshots |
| 7 | `.security-7` | `indices/uWYEV8XgTRq9NPAtuedM9Q/0/index-Azf2dsfRSGuxfHb5W-VJxg` | 884 | shard index-\<gen\> vs current gen |
| 8 | `.security-7` | `indices/uWYEV8XgTRq9NPAtuedM9Q/0/snap-Hrv33udFQNSFJK1cLTdQVA.dat` | 878 | shard snap-\<uuid\> vs live snapshots |
| | | **TOTAL** | **163,875** | |

### 3d. Why they were missed: `limit(10)`

The eight missed keys are not a parser bug. Elasticsearch never wrote them to
the log. `S3BlobStore.deleteBlobs` builds the exception message like this:

```java
new IOException("Failed to delete blobs " + partition.stream().limit(10).toList(), e)
```

`limit(10)` keeps at most 10 keys from the failing batch and drops the rest with
no ellipsis, no count, and no marker of any kind. The rendered list simply closes
with `]` as if it were complete.

The `snap-1` delete at `19:50:25.098Z` (log line 463) is exactly that case. Its
stack trace names exactly 10 keys, all from two indices: six from `logs-app`
(`FKf_36IxTWyQnF1j3fme5A`) and four from the deprecation data stream
(`GVsRrzdESB-K1azSkyk4fA`). It closes with `)]`. The batch actually spanned all
five index shards, 18 blobs. The 8 blobs belonging to `metrics-sys`,
`.security-7`, and `ilm-history` fell off the end of the list. Those 8 keys are
precisely the set in §3c.

Key counts per WARN line, counted from the raw stack traces, confirm 10 is a
ceiling and not a coincidence of batch size:

| Log line | Timestamp (UTC) | Keys named | Batch |
|---:|---|---:|---|
| 442 | 19:49:58.013Z | 0 (3 in nested `Caused by`) | repository verification scratch |
| 455 | 19:50:24.635Z | 6 | metadata cleanup |
| 458 | 19:50:24.871Z | 7 | metadata cleanup |
| 462 | 19:50:25.098Z | 5 | stale root blobs |
| 463 | 19:50:25.098Z | **10** | `snap-1` delete, **truncated** |

Union of all keys named across every WARN line: **21**, counted independently
from the stack-trace lists. The sweeper reports `26 unique keys` parsed, and the
two reconcile exactly: 21 + 3 repository-verification scratch keys from line
442's nested `Caused by` + 2 cross-form duplicate spellings (collapsed by the
dedup pass, see §6) = 26.

The log sweeper condemned
20 of those. The one it withheld is `index-2`, named in line 462 as "no longer
part of any snapshot," and correctly held back by the sweeper's
numerically-highest-index-N guard. `index-2` is still present in `repo-mirror2/`
and is classified PROTECTED, not ORPHAN, by the reachability sweeper. Both tools
independently declined to delete it. That is the one place the log's own account
of reachability is more aggressive than either sweeper, and both sweepers were
right to be conservative.

The log sweeper reports this exposure itself, verbatim from `lg.err`:

```
NOTE: 9 SdkException-path line(s) parsed. ES caps these at 10 keys from the
LAST delete batch only (S3BlobStore.java:382). For full coverage enable TRACE on
'logger.org.elasticsearch.repositories.blobstore' for one retention cycle and
re-feed the logs; re-runs are idempotent via the state file.
```

---

## 4. The WARN lines themselves

Extracted from `es-pod.log` by line number. Each pod log line is a single ECS
JSON object; the fields below are pulled out and the long ones trimmed to roughly
200 characters. Trimming is marked with `…`.

### 4a. Line 442: repository verification failure (the fault's first appearance)

The rig's MinIO is configured to reject `DeleteObjects` without a `Content-Md5`
header. The very first thing that trips over it is `PUT _snapshot`'s verification
teardown, which returns a 500 to the caller.

```
@timestamp : 2026-08-24T19:49:58.013Z
log.level  : WARN
log.logger : rest.suppressed
message    : path: /_snapshot/oci-repro, params: {repository=oci-repro}, status: 500
error.type : org.elasticsearch.repositories.RepositoryVerificationException
error.message : [oci-repro] cannot delete test data at

  Caused by: java.io.IOException: Failed to delete blobs
  [ObjectIdentifier(Key=tests-pL1PTmvUTFm-aV8dyaqcaw/data-Z4nd2wQQR9ut7jRbysNHZQ.dat),
   ObjectIdentifier(Key=tests-pL1PTmvUTFm-aV8dyaqcaw/master.dat), ObjectIdentifier(…

  Caused by: software.amazon.awssdk.services.s3.model.S3Exception: Missing
  required header for this request: Content-Md5. (Service: S3, Status Code: 400,
  Request ID: 18CED5A2F60DF771, Extended Request ID: dd9025bab4ad464b0…
```

Those two `tests-pL1PTmvUTFm-aV8dyaqcaw/*` blobs are still in the bucket at audit
time; they are the two PROTECTED objects in §1, 22 bytes each.

### 4b. Line 463: the truncated `snap-1` delete (SdkException stack-trace path)

This is the line that loses 8 keys. `error.message` and `error.stack_trace`
carry the same truncated list; the stack trace's first line is 892 characters
and names exactly 10 keys.

```
@timestamp : 2026-08-24T19:50:25.098Z
log.level  : WARN
log.logger : org.elasticsearch.repositories.blobstore.BlobStoreRepository
message    : [snap-1/Hrv33udFQNSFJK1cLTdQVA] Failed to delete some blobs during snapshot delete

stack[0]   : java.io.IOException: Failed to delete blobs
  [ObjectIdentifier(Key=indices/FKf_36IxTWyQnF1j3fme5A/0/index-N_uNxlVyQMOLAbNX0bCPjw),
   ObjectIdentifier(Key=indices/FKf_36IxTWyQnF1j3fme5A/0/index-0NOPErF9Ts6CjVx4gtKF_A),
   ObjectIdentifier(Key=indices/FKf_36IxTWyQnF1j3fme5A/0/__1ugMrX0TQ5-j4F7fz3bAXQ), …

stack tail : …, ObjectIdentifier(Key=indices/GVsRrzdESB-K1azSkyk4fA/0/index--Ro_2MZMR8WL2kvnKeoQ1Q),
   ObjectIdentifier(Key=indices/GVsRrzdESB-K1azSkyk4fA/0/index-7fkmGhZlTyqWlV47o5uuZg)]

  Caused by: software.amazon.awssdk.services.s3.model.S3Exception: Missing
  required header for this request: Content-Md5. (Service: S3, Status Code: 400,
  Request ID: 18CED5A9450AFDBF, …
```

Note the tail: the list closes with `)]`. Nothing in this line indicates that 8
more keys existed. An operator reading the log has no way to know the list is
incomplete.

### 4c. Lines 461 and 462: the stale-root pair (INFO announce, WARN failure)

Five milliseconds apart. The INFO line is the only place in the entire log where
a blob list is complete and untruncated, which is why `index-0` and `index-1`
carry the extra `stale-root` source in §3a.

```
@timestamp : 2026-08-24T19:50:25.093Z
log.level  : INFO
log.logger : org.elasticsearch.repositories.blobstore.BlobStoreRepository
message    : [default/oci-repro] Found stale root level blobs [index-0, index-1]. Cleaning them up
```

```
@timestamp : 2026-08-24T19:50:25.098Z
log.level  : WARN
log.logger : org.elasticsearch.repositories.blobstore.BlobStoreRepository
message    : [default/oci-repro] The following blobs are no longer part of any snapshot
             [[index-0, index-1, index-2, meta-Hrv33udFQNSFJK1cLTdQVA.dat,
             snap-Hrv33udFQNSFJK1cLTdQVA.dat]] but failed to remove them
error.message : Failed to delete blobs [ObjectIdentifier(Key=index-0),
  ObjectIdentifier(Key=index-1), ObjectIdentifier(Key=index-2),
  ObjectIdentifier(Key=meta-Hrv33udFQNSFJK1cLTdQVA.dat), ObjectIdentifier(Key=snap-Hr…
```

Every `Caused by` in the log bottoms out at the same S3 400: *Missing required
header for this request: Content-Md5*, and every one carries the same Extended
Request ID prefix `dd9025bab4ad464b0…`: one MinIO instance, one rejection rule,
five distinct delete paths tripping over it.

---

## 5. Post-sweep proof (live rig, read-only)

All commands below were run against the live rig at `http://localhost:9200` via
the running port-forward, after the sweep. Every one is a read: `GET`, or the
`POST` verbs `_verify_integrity` and `_cat` that ES defines as non-mutating. No
delete, no `--execute`, no write to MinIO.

The `elastic` password was read into a shell variable from a mode-`0600` file and
is not reproduced here.

### 5a. Snapshot list

```
$ curl -s -u "elastic:$PW" \
    "http://localhost:9200/_snapshot/oci-repro/_all?filter_path=snapshots.snapshot,snapshots.state,snapshots.uuid,snapshots.indices"
```

```json
{
    "snapshots": [
        {
            "snapshot": "snap-2",
            "uuid": "Akwm7mJyTw-vZoEtd13mRw",
            "indices": [
                ".security-7",
                ".ds-ilm-history-7-2026.08.24-000001",
                "logs-app",
                "metrics-sys",
                ".ds-.logs-elasticsearch.deprecation-default-2026.08.24-000001"
            ],
            "state": "SUCCESS"
        },
        {
            "snapshot": "snap-3",
            "uuid": "vOlMaZYhRRegyN_e3zV3pA",
            "indices": [
                ".security-7",
                ".ds-ilm-history-7-2026.08.24-000001",
                "logs-app",
                "metrics-sys",
                ".ds-.logs-elasticsearch.deprecation-default-2026.08.24-000001"
            ],
            "state": "SUCCESS"
        }
    ]
}
```

Two snapshots, both `SUCCESS`, both listing all five indices. `snap-1` is gone,
as intended. It was deleted before the audit, and its blobs are the ones that
leaked.

### 5b. Repository integrity verification

```
$ curl -s -u "elastic:$PW" -X POST "http://localhost:9200/_snapshot/oci-repro/_verify_integrity"
```

The `results` block, verbatim:

```json
{
  "status": {
    "repository": { "name": "oci-repro", "uuid": "9QLApiRcTdC86SxVVE-SrQ", "generation": 3 },
    "snapshots":       { "verified": 2,  "total": 2  },
    "indices":         { "verified": 5,  "total": 5  },
    "index_snapshots": { "verified": 10, "total": 10 },
    "blobs":           { "verified": 40 }
  },
  "final_repository_generation": 3,
  "total_anomalies": 0,
  "result": "pass"
}
```

`total_anomalies: 0`, `result: "pass"`. All five per-index restorability entries
in the `log` array report `total_snapshot_count: 2, restorable_snapshot_count: 2`:

| Index | UUID | Total snapshots | Restorable |
|---|---|---:|---:|
| `.ds-ilm-history-7-2026.08.24-000001` | `-6gHKxGPT_-EZY3_ShXJEg` | 2 | 2 |
| `.ds-.logs-elasticsearch.deprecation-default-2026.08.24-000001` | `GVsRrzdESB-K1azSkyk4fA` | 2 | 2 |
| `metrics-sys` | `BtIUzj_XSz-Ygq0AfcOMIg` | 2 | 2 |
| `logs-app` | `FKf_36IxTWyQnF1j3fme5A` | 2 | 2 |
| `.security-7` | `uWYEV8XgTRq9NPAtuedM9Q` | 2 | 2 |

**One number to flag honestly:** `blobs.verified` is 40, while `repo-mirror2/`
contains 39 files. The mirror is a filesystem copy and ES's blob accounting is not
guaranteed to be a 1:1 map onto it (directory-marker objects and the
`index.latest` pointer are handled differently by each). The one-object gap is
not explained by anything measured here, and it is left as an open discrepancy
rather than papered over. It does not affect the `total_anomalies: 0` verdict or
any orphan/live count in §1, both of which reconcile exactly against the
pre-sweep mirror.

### 5c. Document counts

The `restored-*` indices were created by the earlier restore validation, from the
post-sweep repository. Their counts are the actual proof that deleting 28 blobs
cost nothing.

```
$ curl -s -u "elastic:$PW" "http://localhost:9200/_cat/count/logs-app?v"
epoch      timestamp count
1787602755 20:19:15  3500

$ curl -s -u "elastic:$PW" "http://localhost:9200/_cat/count/metrics-sys?v"
epoch      timestamp count
1787602755 20:19:15  3500

$ curl -s -u "elastic:$PW" "http://localhost:9200/_cat/count/restored-logs-app?v"
epoch      timestamp count
1787602755 20:19:15  3500

$ curl -s -u "elastic:$PW" "http://localhost:9200/_cat/count/restored-metrics-sys?v"
epoch      timestamp count
1787602755 20:19:15  3500
```

Store sizes match to the kilobyte as well:

```
$ curl -s -u "elastic:$PW" "http://localhost:9200/_cat/indices?v&h=index,docs.count,store.size"
index                docs.count store.size
metrics-sys                3500    281.4kb
logs-app                   3500    280.9kb
restored-logs-app          3500    280.9kb
restored-metrics-sys       3500    281.4kb
```

Two restored indices, not four. The task brief anticipated four `restored-*`
indices; only `restored-logs-app` and `restored-metrics-sys` exist on the rig.
The three system indices were snapshotted but never restored under a
`restored-` prefix, so there are four count outputs total (two originals, two
restores) rather than four restores. Stated as found.

### 5d. Sizing recommendation

```
$ python3 snapshot_sizes.py \
    --es http://localhost:9200 --repo oci-repro --user "elastic:$PW" --recommend
```

```
# 2 snapshots in oci-repro
# fetched 2/2

period       snaps  added (incremental)   largest snapshot (total)
2026-08-24       2            562.4 KiB                  627.7 KiB
SUM              2            562.4 KiB

'added' sums incremental bytes = real repo growth per period.
'total' is what one snapshot references; totals across snapshots overlap — never sum them.

=== Repository sizing recommendation ===

WARNING (precondition): if this repository backs searchable
snapshots (frozen tier), regular snapshots upload ZERO files for
already-mounted indices, so the baseline below UNDERCOUNTS by the
entire frozen footprint (it lives in separate pinned per-index
mount snapshots). For such repositories the reachable-blob manifest
byte count (oci_repo_sweeper.py --emit reachable) is the sizing
source of truth, not this recommendation.

Measured inputs (from _snapshot/<repo>/_status):
  largest snapshot total (snap-2) : 627.7 KiB
  growth samples: per-calendar-day incremental sums over the last
  1 day(s) with data (2026-08-24 .. 2026-08-24):
    median daily growth : 562.4 KiB
    mean daily growth   : 562.4 KiB
    p95 daily growth    : 562.4 KiB
    note: window includes the repository's FIRST snapshot day,
    whose incremental == a full upload; growth is overstated.

Formula (retention_days = 7):
  baseline (largest snapshot total)         : 627.7 KiB
  + retention growth (7 x median daily)     : 3.8 MiB
  + upgrade-day headroom (1 x baseline)     : 627.7 KiB
  = recommended repository capacity         : 5.1 MiB
  = with +20% operational margin            : 6.1 MiB
  conservative variant (7 x p95 daily):
  = recommended repository capacity (p95)   : 5.1 MiB
  = with +20% operational margin (p95)      : 6.1 MiB

Assumptions:
  * Snapshots are incremental: each copies only new segments since
    the previous snapshot; the first is ~full. [Elastic docs]
  * The true repo floor is the UNION of all retained snapshots'
    referenced bytes; the largest single snapshot total is a lower
    bound on that union, used here as the baseline.
  * Elastic recommends a fresh snapshot before upgrading, and large
    segment rewrites (e.g. a version upgrade merging/rewriting
    segments) make the next snapshot re-upload far more than a
    normal day. Modeling that as 1x baseline full is a heuristic,
    not an official Elastic figure.
  * The +20% margin is a heuristic, not an official Elastic figure.
  * Elastic publishes no official repo-capacity formula; sizing here
    is derived from documented incremental behavior only.

Matching SLM retention for a 7-day window, e.g.:
  "retention": { "expire_after": "7d", "min_count": 5 }
  (avoid max_count here: with multiple snapshots per day — SLM
  dailies plus ILM mount snapshots — a count bound can delete
  snapshots that are still inside the time window.)

Sources (fetched 2026-08-24):
  https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore
  https://www.elastic.co/docs/deploy-manage/upgrade/prepare-to-upgrade
  https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/create-snapshots
```

Two things worth reading carefully in that output. Median, mean, and p95 daily
growth are all identical at 562.4 KiB because there is exactly one day of data,
and the tool says so rather than pretending to a distribution. And the
`largest snapshot total` of 627.7 KiB is the size of what `snap-2` *references*,
which is close to but distinct from the 742,258 bytes actually on disk post-sweep.
The difference is the second snapshot's incremental blobs plus root state. Both
caveats are the tool's own; neither is added here.

---

## 6. Chain of custody (campaign 1)

The rig ran ES 9.5.2 under ECK against a MinIO configured to reject
`DeleteObjects` without `Content-Md5`, which is the fault being reproduced. All
times below are local (UTC-7); the pod log is in UTC, 7 hours ahead.

`es-pod.log` was captured at **12:50:33** and covers 465 lines spanning
`19:49:58Z` to `19:50:25Z` UTC: repository registration, three snapshot creations,
the `snap-1` delete, and every one of the five `Failed to delete` WARNs.
`repo-mirror/` was pulled at **12:50:50** (67 objects, 1,120,400 B) and is the
pre-sweep state. `orphans.tsv` was written at **12:50:58** by
`oci_repo_sweeper.py` running in dry-run against that mirror; because it read the
mirror rather than MinIO, its timestamp columns are mirror filesystem times (see
the caveat in §2). `log-manifest.tsv` was written at **12:53:52** by
`es_log_driven_sweeper.py` reading `es-pod.log`, with its console summary in
`lg.err` (same run, same minute). The sweep itself ran between
those two points, and `repo-mirror2/` was pulled at **12:54:36** (39 objects,
742,258 B) as the post-sweep state. The live-rig commands in §5 were run at read
time, roughly 20:19Z, against the still-running port-forward.

Script versions, from `git log` in this repository. The
repository is under active development and moved forward while this document was
being written, so two columns are given: the commit whose code actually produced
each artifact, and where that file stood at the moment this section was written.

| Script | Produced | Version that produced it | Latest commit at write time |
|---|---|---|---|
| `oci_repo_sweeper.py` | `orphans.tsv`, `sweeper-stderr.txt`, the sweep | `1bc0634` (12:40), *dated dry-run manifest + typed delete confirmation* | `fa8a095` (13:21), *adversarial hardening: legacy meta, active-scope guard, fail-closed mtimes, Jackson-exact SMILE tables* |
| `es_log_driven_sweeper.py` | `log-manifest.tsv`, `lg.err`, `log-keys.txt` | working tree as of 12:53, committed 3 minutes later as `c69d4d7`, *dedupe cross-form keys; normalize log timestamps to UTC* | `46ba64d` (13:20), *adversarial hardening: logger anchoring, live-gen cross-check, max-log-age* |
| `snapshot_sizes.py` | §5d output | `bbdeabf` (13:10), *adversarial-review fixes for --recommend*, clean tree at run time | `4b95b57` (13:23), *fix test-suite findings: order-independent caveat, graceful network errors* |

Three provenance caveats, none of which change a number in this document but all
of which a reader deserves:

1. **The sweepers on disk today are newer than the ones that produced the
   manifests.** Both received adversarial-hardening commits (`fa8a095`,
   `46ba64d`) after the audit artifacts were captured, and `snapshot_sizes.py`
   received `4b95b57` after §5d was run. The evidence in §1 to §3 and §5d is
   therefore a record of specific earlier versions, named in the table above.
   Re-running the current code against the same inputs may produce different
   output. That is the point of the hardening, and any such difference is a
   change in the tools, not a correction to the data here. To reproduce §1 to §3
   exactly, check out `1bc0634` / `c69d4d7`.

2. **`log-manifest.tsv` (12:53) predates the commit of the dedup fix (`c69d4d7`,
   12:56).** The manifest was produced by the dedup-capable code sitting in the
   working tree before it was committed; the commit timestamp is later than the
   run. The 20-row output is the post-dedup result as intended, confirmed by
   `lg.err` from the same run.

3. **Two log-sweeper stderr files exist and only one matches the shipped
   manifest.** `lg.err` (12:53) is the post-dedup run: `eligible keys: 20`,
   `20 rows appended to log-manifest.tsv`, `index-gen: 14`. It is the one quoted
   in §3d and the one that describes `log-manifest.tsv` as it stands.
   `log-sweeper-stderr.txt` (12:51) is an earlier pre-dedup run of the same
   input: `eligible keys: 22`, `22 rows written`, `index-gen: 16`. Both runs
   parsed the same `465 lines, 26 unique keys` and both skipped the same
   `index-2` and the same three `tests-*` keys; the dedup pass collapsed exactly
   two duplicate `index-gen` spellings, 16 → 14, giving 22 → 20. The `limit(10)`
   NOTE is byte-identical in both. Nothing in this document sources from the
   pre-dedup file.

The live rig was still running when §5 was captured. Everything in §5 is
re-runnable verbatim against it as long as the port-forward holds; everything in
§1 to §4 is fixed evidence from files on disk and does not depend on the rig staying
up.

---

# Part II. Campaign 2: split-repo hybrid (Strategy D)

Campaign 1 established the leak and measured how much of it a pod log can
reconstruct. Campaign 2 asks the operational follow-up: if you stop asking the
leaking object store to do retention, does the problem go away, and what is left
over?

Strategy D splits the two jobs a snapshot repository normally does. Backups
(the SLM dailies, which get deleted on a retention schedule and therefore
generate the delete traffic that leaks) move to a filesystem repository, where a
delete is a real unlink. Frozen searchable-snapshot mounts stay on S3,
because they are written once, never deleted by retention, and want cheap object
storage. Deletes stop hitting the store that cannot service them.

The run took the existing campaign-1 rig forward through eleven steps, labelled
`d0` through `d10` in the artifact names.

**Source artifacts** captured during the campaign-2 run (session scratch files in
a `hybrid/` subdirectory, not committed):

| Artifact | Size | mtime (local, UTC-7) | What it is |
|---|---:|---|---|
| `d0-heal.json` | 21 B | 14:32 | acknowledgment for the step that cleared the pre-flight DANGER state |
| `d1-sizing.txt` | 5,116 B | 14:32 | `snapshot_sizes.py --recommend --split-frozen` output |
| `d1-classified.tsv` | 352 B, 3 data rows | 14:32 | `--emit-classified` per-snapshot inventory |
| `d1-classified.err` | 231 B | 14:32 | classifier console summary |
| `d2-register-fs.json` | 21 B | 14:32 | `PUT /_snapshot/backups-fs` acknowledgment |
| `d3-repoint.json` | 21 B | 14:32 | `PUT /_slm/policy/rig-daily` acknowledgment (repository → `backups-fs`) |
| `d3-snap1.txt` | 58 B | 14:32 | name of the first backup taken on the fs repository |
| `d3b-unlink-proof.txt` | 22 B | 14:33 | file count in the fs repository after deleting that backup |
| `d3c-standing.txt` | 61 B | 14:33 | name of the standing backup taken after the unlink test |
| `d4-verify-false.json` | 21 B | 14:33 | `PUT /_snapshot/oci-repro?verify=false` acknowledgment |
| `d5-frozen-serving.json` | 56 B | 14:33 | frozen-tier search result |
| `d6-del-snap2.json` | 21 B | 14:33 | `DELETE /_snapshot/oci-repro/snap-2` acknowledgment |
| `d6-del-scrap.json` | 21 B | 14:33 | `DELETE /_snapshot/oci-repro/hybrid-scrap` acknowledgment |
| `d6-es-pod.log` | 250,132 B, 338 lines | 14:33 | raw ES pod log (ECS JSON, one object per line) |
| `d7-mounted.txt` | 66 B, 1 row | 14:33 | mounted-snapshot pre-flight set, exported from the cluster |
| `d8-log-manifest.tsv` | 7,749 B, 53 data rows | 14:33 | log-driven sweeper manifest |
| `d8-summary.txt` | 1,153 B | 14:33 | log-driven sweeper console summary |
| `d8b-applied.txt` | 29 B | 14:33 | result of applying the manifest |
| `d9-catalog.txt` | 104 B, 2 rows | 14:34 | post-sweep snapshot catalog |
| `d9-frozen.json` | 56 B | 14:34 | post-sweep frozen search, after a shared-cache clear |
| `d9-integrity.json` | 301 B | 14:34 | `_verify_integrity` status block, **truncated on capture**, see §15 |
| `d9-residual-orphans.tsv` | 868 B, 4 data rows | 14:34 | residual reachability audit manifest |
| `d10-restore.txt` | 59 B | 14:34 | restore-from-fs shard result |
| `d10-count.txt` | 5 B | 14:34 | document count of the restored index |
| `repo-mirror3/` | 70 files, 896,266 B | 14:14 | S3 bucket mirror, pre-`d6` |
| `repo-mirror4/` | 37 files, 482,114 B | 14:34 | S3 bucket mirror, post-sweep (final state) |
| `exec.log`, `mounted.txt`, `cls.err` | - | 14:13 to 14:14 | the pre-`d0` DANGER state that step `d0` healed |

The MinIO credentials and the ES `elastic` password live in files with mode
`0600` in that directory and are deliberately not reproduced anywhere in this
document.

**Rig at write time**, re-verified read-only: ES `9.5.2`, single node, cluster
health `green`, 11 active shards, 0 unassigned. License type redacted; the tier permits searchable snapshots,
`status: active`. A frozen-tier searchable snapshot needs an Enterprise-class
license, so this is recorded rather than assumed.

```
$ curl -s -u "elastic:$PW" "http://localhost:9202/_license?filter_path=license.type,license.status"
{ "license" : { "status" : "active", "type" : "<REDACTED>" } }
```

Two repositories are registered, which is the whole point of the topology:

```
$ curl -s -u "elastic:$PW" "http://localhost:9202/_cat/repositories?v"
id         type
oci-repro    s3
backups-fs   fs
```

### The starting position: step `d0` healed a DANGER state

Campaign 2 did not start from a clean rig. At 14:13 the mounted-snapshot export
(`mounted.txt`) listed two frozen mounts, and one of them was pinned to a
snapshot that no longer existed in the repository catalog:

```
frozen-base-metrics  Nv-V5l2SSUuq5bW3f4iqgQ  partial  frozen-metrics
snap-3               vOlMaZYhRRegyN_e3zV3pA  partial  frozen-logs-app
```

Both tools refused to proceed. `oci_repo_sweeper.py` aborted before touching
anything (`exec.log`, verbatim, trimmed):

```
INFO 70 objects, 875.3 KiB
INFO generation 6: 3 live snapshots, 6 live indices

  !!! DANGER STATE: mounted snapshot missing from the repository catalog !!!
  1 entry/entries from mounted.txt are NOT in the current repository
  catalog (index-N snapshots[].name / snapshots[].uuid):
      snap-3
  …
ERROR ABORTING before any deletion: mounted-snapshot pre-flight failed (1 missing) — nothing deleted
```

and `snapshot_sizes.py --emit-classified` raised the same alarm independently
(`cls.err`): `# 1 mounted snapshot(s) MISSING-FROM-CATALOG`.

Step `d0` cleared it. The artifact is a bare acknowledgment
(`d0-heal.json`: `{"acknowledged":true}`), so the exact call is not recorded, but
the before/after state is: `frozen-logs-app` is absent from the live index list
at write time, and the post-heal pre-flight export (`d7-mounted.txt`) carries
only the one surviving mount. Stated as inference, because that is what the
artifact supports.

This matters for reading everything below: campaign 2's numbers begin at
repository generation 6, not at a fresh repository.

---

## 7. Sizing inputs under `--split-frozen`

### 7a. The classified inventory

`snapshot_sizes.py --emit-classified` labels every snapshot in the repository by
what it is *for*, because the sizing arithmetic is different for each class. All
three rows of `d1-classified.tsv`, verbatim:

| Snapshot | Class | Policy | Tier | Mounted by | State | Start (UTC) | Incremental B | Total B |
|---|---|---|---|---|---|---|---:|---:|
| `snap-2` | other | - | - | - | SUCCESS | 2026-08-24T19:50:24Z | 575,883 | 642,810 |
| `rig-daily-2026.08.24-xev01ty8rawen8yasu9qsw` | slm | `rig-daily` | - | - | SUCCESS | 2026-08-24T21:04:40Z | 58,806 | 389,993 |
| `frozen-base-metrics` | frozen-pinned | - | partial | `frozen-metrics` | SUCCESS | 2026-08-24T21:04:44Z | 0 | 288,242 |

The console summary (`d1-classified.err`), verbatim:

```
# 3 snapshots in oci-repro
# --emit-classified: 1 snapshot(s) pinned by mounted indices, 1 SLM-created
# fetched 3/3
# classified: 3 snapshot(s) total -- slm=1, frozen-pinned=1, other=1
# 0 mounted snapshot(s) MISSING-FROM-CATALOG
```

The `frozen-pinned` row's `incremental_bytes` is **0**. That single zero is the
reason the split exists. A regular snapshot uploads nothing for a shard that is
already mounted as a searchable snapshot, so any sizing model that sums
incrementals across all classes reports the frozen tier as costing nothing at
all. It does not; it costs `total_bytes`, which lives in the pinned mount
snapshot and is invisible to growth arithmetic.

### 7b. The split-frozen recommendation

From `d1-sizing.txt`. The per-class breakdown first:

```
period       class          snaps  added (incremental)   largest snapshot (total)
2026-08-24   slm                1             57.4 KiB                  380.9 KiB
2026-08-24   frozen-pinned      1                0.0 B                  281.5 KiB
2026-08-24   other              1            562.4 KiB                  627.7 KiB
SUM          slm                1             57.4 KiB
SUM          frozen-pinned      1                0.0 B
SUM          other              1            562.4 KiB
SUM          (all)              3            619.8 KiB
```

```
=== Snapshot classes (--split-frozen) ===
  slm               1 snapshot(s), incrementals (real repo growth): 57.4 KiB
  frozen-pinned     1 snapshot(s), frozen footprint (pinned mount snapshots; totals may overlap if mounts share lineage): 281.5 KiB
                        partial mounts (frozen tier, shared_cache): 1 snapshot(s), 281.5 KiB
                        full mounts (cold tier, full copy)       : 0 snapshot(s), 0.0 B
  other             1 snapshot(s), incrementals: 562.4 KiB (manual / ILM-orphaned)
  note: summed incrementals are only meaningful for the slm class —
  a regular backup snapshot uploads ZERO bytes for shards already
  mounted as searchable snapshots, so the frozen tier shows up only
  as the pinned mount snapshots' totals above (a floor, not growth).
```

Then the recommendation itself. The formula lines, verbatim:

```
Measured inputs (from _snapshot/<repo>/_status):
  largest SLM snapshot total (rig-daily-2026.08.24-xev01ty8rawen8yasu9qsw) : 380.9 KiB
  frozen footprint (pinned mount snapshots; totals may overlap if mounts share lineage): 281.5 KiB
    partial mounts (frozen tier) : 1 snapshot(s), 281.5 KiB
    full mounts (cold tier)      : 0 snapshot(s), 0.0 B
  note: 2 non-slm snapshot(s) excluded from baseline/growth
  (frozen-pinned mounts are a footprint floor, not growth;
  'other' snapshots have no policy and no mount pinning them).
  growth samples (slm class ONLY): per-calendar-day incremental
  sums over the last 1 day(s) with data (2026-08-24 .. 2026-08-24):
    median daily growth : 57.4 KiB
    mean daily growth   : 57.4 KiB
    p95 daily growth    : 57.4 KiB
    note: window includes the repository's FIRST snapshot day,
    whose incremental == a full upload; growth is overstated.

Formula (retention_days = 7):
  baseline (largest slm snapshot total)     : 380.9 KiB
  + retention growth (7 x median daily)     : 402.0 KiB
  + upgrade-day headroom (1 x baseline)     : 380.9 KiB
  + frozen footprint (pinned mounts)        : 281.5 KiB
  = recommended repository capacity         : 1.4 MiB
  = with +20% operational margin            : 1.7 MiB
  conservative variant (7 x p95 daily):
  = recommended repository capacity (p95)   : 1.4 MiB
  = with +20% operational margin (p95)      : 1.7 MiB
```

The tool states its own reason for splitting, verbatim:

```
NOTE: --split-frozen is active — baseline and growth below
come from the slm (regular backup) class ONLY, and the measured
frozen footprint is added as its own term instead of being an
unquantified undercount. The reachable-blob manifest byte count
(oci_repo_sweeper.py --emit reachable) remains the ground truth
for total repository capacity: the repo floor is the UNION of
all retained snapshots, which these per-snapshot totals can
only bound from below.
```

### 7c. What the split means for the hybrid

**Three of the four terms in that formula are slm-only, and they are the terms
that size the fs backup target.** Baseline (380.9 KiB), retention growth
(402.0 KiB), and upgrade-day headroom (380.9 KiB) all derive from the SLM class
and nothing else: 1,163.8 KiB, or 1.14 MiB, of filesystem capacity. The fourth
term, the 281.5 KiB frozen footprint, is a *measured* number rather than an
estimate, and under Strategy D it does not move: those bytes stay on S3 as the
pinned mount snapshot backing `frozen-metrics`.

So the same output sizes two different stores at once. The fs repository needs
roughly 1.14 MiB plus margin; the S3 bucket needs to keep holding 281.5 KiB of
frozen footprint and, once retention stops running against it, is not expected to
grow at all.

Two of the tool's caveats apply directly and are reproduced because they bound
the numbers above:

- Median, mean, and p95 daily growth are all 57.4 KiB because there is exactly
  one day of SLM data. The tool says so rather than implying a distribution.
- The frozen footprint is a *floor*: mounts that share segment lineage
  double-count, and it excludes any blob the repository retains that no snapshot
  references. Verbatim from the assumptions block: *"The frozen footprint is
  MEASURED (sum of pinned mount snapshot totals), not estimated. It is a floor."*

Contrast with campaign 1's §5d, where the same tool without `--split-frozen`
could only emit a warning that the baseline *"UNDERCOUNTS by the entire frozen
footprint"* and had no number to put in its place. Here it has one.

---

## 8. Registering the fs repository and repointing SLM

Three acknowledgments, each verbatim from its artifact:

| Step | Artifact | Content |
|---|---|---|
| `d2`, register `backups-fs` | `d2-register-fs.json` | `{"acknowledged":true}` |
| `d3`, repoint the `rig-daily` SLM policy at it | `d3-repoint.json` | `{"acknowledged":true}` |
| `d4`, re-register `oci-repro` with `verify=false` | `d4-verify-false.json` | `{"acknowledged":true}` |

The repository definition, re-verified at write time:

```
$ curl -s -u "elastic:$PW" \
    "http://localhost:9202/_snapshot/backups-fs?filter_path=*.type,*.settings.location"
{"backups-fs":{"type":"fs","settings":{"location":"/mnt/es-repo/backups"}}}
```

The SLM policy, re-verified at write time. Note `"repository":"backups-fs"`,
and the 7-day retention window that §7b's recommendation was computed against:

```
$ curl -s -u "elastic:$PW" "http://localhost:9202/_slm/policy?filter_path=*.policy,*.stats"
{"rig-daily":{
  "policy":{"name":"<rig-daily-{now/d}>","schedule":"0 30 3 * * ?",
            "repository":"backups-fs",
            "retention":{"expire_after":"7d","min_count":5}},
  "stats":{"policy":"rig-daily","snapshots_taken":3,"snapshots_failed":0,
           "snapshots_deleted":0,"snapshot_deletion_failures":0}}}
```

Two snapshot names were captured on the fs repository:

```
d3-snap1.txt   : backup on fs: rig-daily-2026.08.24-dkyrqloyqr20tf6ynstxsa
d3c-standing.txt: standing backup: rig-daily-2026.08.24-qszkz9gvqqwc7hhngsy4vg
```

The first is the one destroyed in §9 to prove that a delete on this repository
actually deletes. The second is the standing backup taken afterwards, and it is
still the only snapshot on `backups-fs`, re-verified at write time:

```
$ curl -s -u "elastic:$PW" "http://localhost:9202/_cat/snapshots/backups-fs?h=id,status,indices&v"
id                                          status indices
rig-daily-2026.08.24-qszkz9gvqqwc7hhngsy4vg SUCCESS       4
```

Four indices: `metrics-sys`, `logs-app`, `.snapshot-blob-cache`, `.security-7`
(re-verified at write time from `_snapshot/backups-fs/_all`).

---

## 9. The retention unlink proof

This is the core of the hybrid. Everything else is plumbing; this is the claim
being tested.

On the S3 repository, `DeleteObjects` is rejected and the blobs stay (campaign 1,
§1 to §4: 28 orphans, 378,142 bytes, from one snapshot delete). On a filesystem
repository, the same delete is an `unlink`. The test was: take a backup, count
the files, delete the backup, count again.

The captured artifact is the after-count, verbatim and complete
(`d3b-unlink-proof.txt` is 22 bytes long, and this is the whole file):

```
files after delete: 2
```

Two files. Those two are the repository root pointer `index.latest` and the
current root generation blob `index-N`, which is the irreducible floor of a
snapshot repository with no snapshots in it. Every shard blob, every segment,
every `snap-*.dat` and `meta-*.dat` was gone from disk.

**The before-count is not in the captured artifacts, and that gap is stated
rather than filled from memory.** What is available instead is a live
re-verification of the equivalent state. The fs repository today holds exactly
one backup with the same four-index shape, and it is 26 files,
re-verified at write time, read-only, inside the ES pod:

```
$ kubectl exec -n es-rig <es-pod> -c elasticsearch -- \
    sh -c 'find /mnt/es-repo/backups -type f | wc -l'
26
```

So the shape of the proof is 26 → 2: a single four-index backup occupies 26 files
and 697,682 bytes on the fs repository, and deleting it leaves 2 files behind.
The 26 is re-verified at write time against an equivalent snapshot; the 2 is the
captured artifact from the actual delete.

The current 26-file tree, re-verified at write time (this is the *standing*
backup `rig-daily-…-qszkz9gvqqwc7hhngsy4vg`, so the UUIDs differ from the deleted
one; the shape is what is being shown):

| Path | Files | Bytes |
|---|---:|---:|
| repository root (`index-2`, `index.latest`, `meta-RAb4…dat`, `snap-RAb4…dat`) | 4 | 40,270 |
| `indices/6Xext7g1SpiESKXrOJ-0OQ/` | 5 | 289,350 |
| `indices/egB8RFkjTFmzncr6TTTDsw/` | 5 | 46,905 |
| `indices/mu-qLsg5TSaZLGbHWNpx5w/` | 5 | 289,965 |
| `indices/o3XNaZ4vQvmBKl7KG9epiw/` | 7 | 31,192 |
| **TOTAL** | **26** | **697,682** |

Compare that against campaign 1's §1: a snapshot delete on the S3 repository
removed the snapshot from the catalog and left 28 blobs and 378,142 bytes behind.
On the fs repository the catalog delete and the byte delete are the same
operation. That is the entire argument for Strategy D, and it is one line of
evidence.

One caveat, recorded because it bounds the claim: the fs repository lives on a
volume inside the Elasticsearch pod, standing in for the shared network
filesystem a production deployment would use. See §15.

---

## 10. `verify=false` and frozen serving

### 10a. `verify=false`

Campaign 1's very first fault (§4a, log line 442) was `PUT _snapshot`'s
*verification teardown* failing: the repository-verification scratch blobs could
not be deleted, so registration returned a 500 even though the repository was
fine. Campaign 2 re-registers `oci-repro` with `verify=false` to sidestep it.

`d4-verify-false.json`: `{"acknowledged":true}`. No 500, no exception.

Independently checked at write time by scanning all 338 lines of
`d6-es-pod.log`: zero occurrences of `RepositoryVerificationException` and
zero log lines from the `rest.suppressed` logger, versus campaign 1's log
where line 442 was exactly that pair. The registration path that failed in
campaign 1 does not fail here because it is not exercised.

The two campaign-1 verification scratch blobs are still in the bucket, still 22
bytes each, still classified PROTECTED; they appear in the final mirror in §13e.

### 10b. Frozen serving

`d5-frozen-serving.json`, verbatim and complete:

```json
{"_shards":{"failed":0},"hits":{"total":{"value":3500}}}
```

3,500 documents, zero failed shards, served out of a partially-mounted
searchable snapshot whose bytes live in the S3 bucket. `frozen-metrics` reports
`store.size` of `0b` in `_cat/indices` because a partial mount holds no local
copy, re-verified at write time:

```
$ curl -s -u "elastic:$PW" "http://localhost:9202/_cat/indices?v&h=index,docs.count,store.size,health"
index                docs.count store.size health
logs-app                   3500    280.9kb green
fs-restored-logs-app       3500    280.9kb green
restored-logs-app          3500    280.9kb green
metrics-sys                3500    281.4kb green
frozen-metrics             3500         0b green
restored-metrics-sys       3500    281.4kb green
```

```
$ curl -s -u "elastic:$PW" "http://localhost:9202/frozen-metrics/_settings?filter_path=**.store.type"
{"frozen-metrics":{"settings":{"index":{"store":{"type":"snapshot"}}}}}
```

`store.type: snapshot` is what makes it a searchable snapshot rather than an
ordinary index. It is re-checked after a cache clear in §13c.

---

## 11. The residual leak

Moving retention to the fs repository stops the *scheduled* deletes from hitting
S3. It does not stop a manual delete. Step `d6` deleted two snapshots directly
from `oci-repro` and captured the pod log to see what that costs.

Both deletes were acknowledged:

```
d6-del-snap2.json : {"acknowledged":true}
d6-del-scrap.json : {"acknowledged":true}
```

Both leaked. `d6-es-pod.log` is 338 lines spanning `20:50:21.123Z` to
`21:33:19.474Z` (333 of them carry an `@timestamp`; the remainder are non-ECS
startup output). It contains 14 failed-delete WARN lines, every one of them
bottoming out at the same MinIO rejection as campaign 1.

### 11a. All 14 WARN lines

Extracted at write time by parsing the ECS JSON and counting distinct
`ObjectIdentifier(Key=…)` entries per line.

| # | Log line | Timestamp (UTC) | Keys named | Message |
|---:|---:|---|---:|---|
| 1 | 209 | 20:51:58.609Z | 4 | `[default/oci-repro] The following blobs are no longer part of any snapshot […] but failed to remove them` |
| 2 | 210 | 20:51:58.609Z | **10** | `[snap-3/vOlMaZYhRRegyN_e3zV3pA] Failed to delete some blobs during snapshot delete` |
| 3 | 241 | 21:04:41.318Z | 4 | `Failed to clean up old metadata blobs` |
| 4 | 249 | 21:04:44.628Z | 3 | `Failed to clean up old metadata blobs` |
| 5 | 322 | 21:33:15.059Z | 5 | `Failed to clean up old metadata blobs` |
| 6 | 326 | 21:33:17.253Z | **10** | `[default/oci-repro] The following blobs are no longer part of any snapshot […] but failed to remove them` |
| 7 | 327 | 21:33:17.254Z | **10** | `[snap-2/Akwm7mJyTw-vZoEtd13mRw] Failed to delete some blobs during snapshot delete` |
| 8 | 328 | 21:33:17.257Z | 8 | `[default/oci-repro] index GVsRrzdESB-K1azSkyk4fA is no longer part of any snapshot in the repository, but failed to clean up its index folder` |
| 9 | 329 | 21:33:17.257Z | 8 | `[default/oci-repro] index -6gHKxGPT_-EZY3_ShXJEg is no longer part of any snapshot in the repository, but failed to clean up its index folder` |
| 10 | 333 | 21:33:19.467Z | **10** | `[default/oci-repro] The following blobs are no longer part of any snapshot […] but failed to remove them` |
| 11 | 334 | 21:33:19.469Z | **10** | `[hybrid-scrap/fkvwBzpBR_2TNCwiHHkWWA] Failed to delete some blobs during snapshot delete` |
| 12 | 335 | 21:33:19.471Z | 8 | `[default/oci-repro] index -6gHKxGPT_-EZY3_ShXJEg is no longer part of any snapshot in the repository, but failed to clean up its index folder` |
| 13 | 336 | 21:33:19.473Z | **10** | `[default/oci-repro] index FKf_36IxTWyQnF1j3fme5A is no longer part of any snapshot in the repository, but failed to clean up its index folder` |
| 14 | 337 | 21:33:19.473Z | 8 | `[default/oci-repro] index GVsRrzdESB-K1azSkyk4fA is no longer part of any snapshot in the repository, but failed to clean up its index folder` |

**Attribution, stated precisely.** The brief for this section was "14 WARN lines
from two catalog deletions," and that is not quite what the log shows. Nine of
the fourteen (lines 326 to 337) fall in the `21:33:17` to `21:33:19` window and belong
to the two `d6` deletions and their cascading index-folder cleanups: `snap-2` at
line 327, `hybrid-scrap` at line 334, plus the root-blob and index-folder cleanup
each triggered. The other five predate `d6`. Line 322 is a metadata cleanup
during snapshot creation two seconds earlier, lines 241 and 249 are metadata
cleanups from `21:04`, and lines 209 and 210 are the earlier `snap-3` delete at
`20:51`. All 14 are in the captured window and all 14 leaked; only 9 are `d6`'s.

**Six of the fourteen name exactly 10 keys.** That is `limit(10)` in
`S3BlobStore.deleteBlobs` (campaign 1, §3d) firing six separate times in one run.

### 11b. Two lines, quoted

Line 327, the `snap-2` delete, ten keys, truncated. Trimmed with `…`:

```
@timestamp : 2026-08-24T21:33:17.254Z
log.level  : WARN
log.logger : org.elasticsearch.repositories.blobstore.BlobStoreRepository
message    : [snap-2/Akwm7mJyTw-vZoEtd13mRw] Failed to delete some blobs during snapshot delete

error.message : Failed to delete blobs
  [ObjectIdentifier(Key=indices/FKf_36IxTWyQnF1j3fme5A/0/index-XFd_ujjMTaGqEhHi5SZPrg),
   ObjectIdentifier(Key=indices/FKf_36IxTWyQnF1j3fme5A/0/snap-vOlMaZYhRRegyN_e3zV3pA.dat),
   ObjectIdentifier(Key=indices/FKf_36IxTWyQnF1j3fme5A/0/index-hcX2zb5lQ1ChwC5q5nWEzA), …

stack tail : …, ObjectIdentifier(Key=indices/BtIUzj_XSz-Ygq0AfcOMIg/0/index-gW2D_OyTQUOtpbRGh-FDqA),
   ObjectIdentifier(Key=indices/BtIUzj_XSz-Ygq0AfcOMIg/0/index-eLMojwX6SB-dF9v5eqf08w)]
```

Line 333 is more useful, because it catches the truncation **inside a single log
line**. The human-readable `message` field carries an untruncated bracket list of
**13** blob names; the machine-readable `error.message` on the same line carries
only **10** `ObjectIdentifier` entries. Trimmed with `…`:

```
@timestamp : 2026-08-24T21:33:19.467Z
log.level  : WARN
log.logger : org.elasticsearch.repositories.blobstore.BlobStoreRepository
message    : [default/oci-repro] The following blobs are no longer part of any snapshot
             [[index-2, meta-fkvwBzpBR_2TNCwiHHkWWA.dat, meta-vOlMaZYhRRegyN_e3zV3pA.dat,
               index-7, index-8, index-3, index-4, index-5,
               snap-fkvwBzpBR_2TNCwiHHkWWA.dat, index-6,
               snap-Akwm7mJyTw-vZoEtd13mRw.dat, snap-vOlMaZYhRRegyN_e3zV3pA.dat,
               meta-Akwm7mJyTw-vZoEtd13mRw.dat]] but failed to remove them

error.message : Failed to delete blobs [ObjectIdentifier(Key=index-2),
  ObjectIdentifier(Key=meta-fkvwBzpBR_2TNCwiHHkWWA.dat), … ,
  ObjectIdentifier(Key=snap-fkvwBzpBR_2TNCwiHHkWWA.dat), ObjectIdentifier(Key=index-6)]
```

Thirteen named in prose, ten in the structured list, no marker of the difference
in either. Campaign 1 had to reconstruct that gap from a batch-size argument
(§3d); here the same line proves it.

Note `index-8` in the prose list. Elasticsearch itself considered `index-8` a
deletable stale root blob. The sweeper disagrees, and §12c is why.

---

## 12. The cleanup

### 12a. The mounted-set export

Before sweeping anything, the pre-flight set. `d7-mounted.txt`, verbatim and
complete (one row):

```
frozen-base-metrics	Nv-V5l2SSUuq5bW3f4iqgQ	partial	frozen-metrics
```

One snapshot name, one snapshot UUID, the mount type, and the index mounted
against it. This is the file both sweepers read to answer "is any live index
reading blobs that the catalog no longer references?" It is the check that aborted
the pre-`d0` run and that passes here.

### 12b. The sweeper's summary

`d8-summary.txt`, verbatim:

```
WARNING --prefix '/' given: treating the whole bucket as the repository root
INFO parsed 338 lines, 66 unique keys
INFO manifest: 53 rows written to d8-log-manifest.tsv

--- sweep summary ------------------------------------------
lines read           : 338
lines matched        : 17
lines skipped        : 321
eligible keys        : 53
  index-gen             : 24
  meta-dat              : 6
  segment-data          : 10
  snap-dat              : 13
skipped [guard: numerically-highest index-N in its directory]: 1
  - index-8
skipped [unknown blob-name shape (use --allow-any-key to include)]: 2
  - indices/-6gHKxGPT_-EZY3_ShXJEg/
  - indices/GVsRrzdESB-K1azSkyk4fA/
NOTE: 28 SdkException-path line(s) parsed. ES caps these at 10 keys from the LAST delete batch only (S3BlobStore.java:382). For full coverage enable TRACE on 'logger.org.elasticsearch.repositories.blobstore' for one retention cycle and re-feed the logs; re-runs are idempotent via the state file.
```

(The manifest path in the original output is a session scratch path; only the
filename is reproduced.)

The four blob-kind counts sum exactly: 24 + 6 + 10 + 13 = 53.

### 12c. The manifest-side highest-generation guard

One line in that summary is doing the most important work:

```
skipped [guard: numerically-highest index-N in its directory]: 1
  - index-8
```

Elasticsearch's own log named `index-8` as a stale root blob no longer part of
any snapshot (§11b, line 333). Deleting the numerically highest `index-N` in the
repository root destroys the repository's current generation pointer. The sweeper
refuses on principle, without needing to know which generation is live.

That refusal is vindicated in §13a: the repository's live generation at
verification time is **9**, and both `index-8` and `index-9` are present in the
final bucket state. `index-8` is the N-1 guard's PROTECTED object; `index-9` is
the live root. Had the sweeper trusted the log, it would have deleted a root
generation blob that the repository was about to advance past.

The two other skips are the bare index-folder prefixes
`indices/-6gHKxGPT_-EZY3_ShXJEg/` and `indices/GVsRrzdESB-K1azSkyk4fA/`, which
are directory paths rather than blob names and are rejected as an unknown key
shape.

### 12d. The full 53-key manifest

All 53 rows of `d8-log-manifest.tsv`, grouped by directory. The
`size_bytes` / `created` / `last_modified` / `last_accessed` columns in that file
are all literal `-`: the log-driven path never lists the bucket, so it genuinely
does not know how big any of these blobs are. Those columns are omitted rather
than padded from another source. `source` is `sdk-exception` unless noted.

Index UUIDs resolve via campaign 1's §1a, plus one new mapping read at write time
from `repo-mirror4/index-9`: `2ROCq0IpRKaD_tSgwrcq4A` → `.snapshot-blob-cache`.

**Repository root, 12 keys (the six `index-N` rows are listed explicitly, as
they are the ones the guard in §12c had to reason about):**

| # | Key | first_seen_in_logs | last_seen_in_logs | source |
|---:|---|---|---|---|
| 1 | `index-2` | 20:51:58.583Z | 21:33:19.467Z | sdk-exception, stale-root |
| 2 | `index-3` | 20:51:58.609Z | 21:33:19.467Z | sdk-exception, stale-root |
| 3 | `index-4` | 21:04:41.318Z | 21:33:19.467Z | sdk-exception, stale-root |
| 4 | `index-5` | 21:04:44.628Z | 21:33:19.467Z | sdk-exception, stale-root |
| 5 | `index-6` | 21:33:15.059Z | 21:33:19.467Z | sdk-exception, stale-root |
| 6 | `index-7` | 21:33:17.253Z | 21:33:19.467Z | sdk-exception, stale-root |
| 7 | `meta-Akwm7mJyTw-vZoEtd13mRw.dat` | 21:33:17.253Z | 21:33:19.464Z | sdk-exception, stale-root |
| 8 | `meta-fkvwBzpBR_2TNCwiHHkWWA.dat` | 21:33:19.467Z | 21:33:19.467Z | sdk-exception |
| 9 | `meta-vOlMaZYhRRegyN_e3zV3pA.dat` | 20:51:58.609Z | 21:33:19.467Z | sdk-exception, stale-root |
| 10 | `snap-Akwm7mJyTw-vZoEtd13mRw.dat` | 21:33:17.253Z | 21:33:19.464Z | sdk-exception, stale-root |
| 11 | `snap-fkvwBzpBR_2TNCwiHHkWWA.dat` | 21:33:19.467Z | 21:33:19.467Z | sdk-exception |
| 12 | `snap-vOlMaZYhRRegyN_e3zV3pA.dat` | 20:51:58.609Z | 21:33:19.464Z | sdk-exception, stale-root |

`index-0` and `index-1` do not appear: they were already swept in campaign 1. The
run is a strict continuation, not a repeat. Six consecutive root generations
`index-2` … `index-7` leaked across the campaign, which is one per delete-bearing
operation.

**`indices/-6gHKxGPT_-EZY3_ShXJEg/`, `.ds-ilm-history-7-2026.08.24-000001`, 7 keys:**

| # | Key (below the index directory) | first_seen | last_seen |
|---:|---|---|---|
| 13 | `0/__0dkExkwySPmPT2cmnFgyCg` | 21:33:17.257Z | 21:33:19.471Z |
| 14 | `0/__ScLvAcH2Si2QMN1a_A6HMQ` | 21:33:17.257Z | 21:33:19.471Z |
| 15 | `0/index-5-eGzXmZSEymyZgowVVf7w` | 20:51:58.609Z | 21:33:19.471Z |
| 16 | `0/index-YOZtMlV8R4upAV-HmbcSzw` | 21:33:17.257Z | 21:33:19.471Z |
| 17 | `0/snap-Akwm7mJyTw-vZoEtd13mRw.dat` | 21:33:17.257Z | 21:33:19.471Z |
| 18 | `0/snap-vOlMaZYhRRegyN_e3zV3pA.dat` | 20:51:58.609Z | 21:33:19.471Z |
| 19 | `meta-R9pSNaABvGPSWU9_xcpw.dat` | 21:33:17.257Z | 21:33:19.471Z |

**`indices/2ROCq0IpRKaD_tSgwrcq4A/`, `.snapshot-blob-cache`, 7 keys:**

| # | Key (below the index directory) | first_seen | last_seen |
|---:|---|---|---|
| 20 | `0/__8GitRdOIRc-y0L7PAiTeZg` | 21:33:19.469Z | 21:33:19.469Z |
| 21 | `0/__FUqgkWRzQN6zQLg44dzepw` | 21:33:19.469Z | 21:33:19.469Z |
| 22 | `0/__Uxhcjrl_R92TNO0YJ8MiAA` | 21:33:19.469Z | 21:33:19.469Z |
| 23 | `0/__nmzCGcusQfWvOqJiINpCtQ` | 21:33:19.469Z | 21:33:19.469Z |
| 24 | `0/index-I4YkT8B4QzyNUNjqeYFbmw` | 21:33:19.469Z | 21:33:19.469Z |
| 25 | `0/index-esb_003tQU2w3rV_YCgKDw` | 21:33:15.059Z | 21:33:19.469Z |
| 26 | `0/snap-fkvwBzpBR_2TNCwiHHkWWA.dat` | 21:33:19.469Z | 21:33:19.469Z |

**`indices/BtIUzj_XSz-Ygq0AfcOMIg/`, `metrics-sys`, 5 keys:**

| # | Key (below the index directory) | first_seen | last_seen |
|---:|---|---|---|
| 27 | `0/index-Onv4bHMsQZOX7N_w2mwyqQ` | 21:33:17.254Z | 21:33:17.254Z |
| 28 | `0/index-eLMojwX6SB-dF9v5eqf08w` | 21:04:44.628Z | 21:33:17.254Z |
| 29 | `0/index-gW2D_OyTQUOtpbRGh-FDqA` | 20:51:58.609Z | 21:33:17.254Z |
| 30 | `0/index-lLJFGgIRRBafPcnwq6qgWw` | 21:04:41.318Z | 21:33:17.254Z |
| 31 | `0/snap-vOlMaZYhRRegyN_e3zV3pA.dat` | 20:51:58.609Z | 21:33:17.254Z |

**`indices/FKf_36IxTWyQnF1j3fme5A/`, `logs-app`, 10 keys:**

| # | Key (below the index directory) | first_seen | last_seen |
|---:|---|---|---|
| 32 | `0/__RaFdrEadT5aj9VGiXT6AZg` | 21:33:19.473Z | 21:33:19.473Z |
| 33 | `0/__Zbwrai23TYW86WEBsMQ5eQ` | 21:33:19.473Z | 21:33:19.473Z |
| 34 | `0/index-WrPIq5UNRLWNuh1AqyewQg` | 21:33:19.473Z | 21:33:19.473Z |
| 35 | `0/index-XFd_ujjMTaGqEhHi5SZPrg` | 20:51:58.609Z | 21:33:19.473Z |
| 36 | `0/index-hcX2zb5lQ1ChwC5q5nWEzA` | 21:33:17.254Z | 21:33:19.473Z |
| 37 | `0/index-monf8lAwTb6AsQrNG3OOmg` | 21:33:15.059Z | 21:33:19.473Z |
| 38 | `0/snap-Akwm7mJyTw-vZoEtd13mRw.dat` | 21:33:17.254Z | 21:33:19.473Z |
| 39 | `0/snap-fkvwBzpBR_2TNCwiHHkWWA.dat` | 21:33:19.473Z | 21:33:19.473Z |
| 40 | `0/snap-vOlMaZYhRRegyN_e3zV3pA.dat` | 20:51:58.609Z | 21:33:19.473Z |
| 41 | `meta-RtpSNaABvGPSWU9_xcpw.dat` | 21:33:19.473Z | 21:33:19.473Z |

**`indices/GVsRrzdESB-K1azSkyk4fA/`, `.ds-.logs-elasticsearch.deprecation-default-2026.08.24-000001`, 7 keys:**

| # | Key (below the index directory) | first_seen | last_seen |
|---:|---|---|---|
| 42 | `0/__-u8f0RY5Tt2Mugf307V5EA` | 21:33:17.257Z | 21:33:19.473Z |
| 43 | `0/__3RdBb9CeTY2DzGAG_oBU-A` | 21:33:17.257Z | 21:33:19.473Z |
| 44 | `0/index-0IFZ0oIgR5-nQj8EVNtJMg` | 21:33:17.257Z | 21:33:19.473Z |
| 45 | `0/index-6m36_dxXREWZnQ3gFbyPOA` | 20:51:58.609Z | 21:33:19.473Z |
| 46 | `0/snap-Akwm7mJyTw-vZoEtd13mRw.dat` | 21:33:17.257Z | 21:33:19.473Z |
| 47 | `0/snap-vOlMaZYhRRegyN_e3zV3pA.dat` | 20:51:58.609Z | 21:33:19.473Z |
| 48 | `meta-RdpSNaABvGPSWU9_xcpw.dat` | 21:33:17.257Z | 21:33:19.473Z |

**`indices/uWYEV8XgTRq9NPAtuedM9Q/`, `.security-7`, 5 keys:**

| # | Key (below the index directory) | first_seen | last_seen |
|---:|---|---|---|
| 49 | `0/index-DTuAjT7_QQ2-nuSqWeLX0A` | 21:04:41.318Z | 21:33:19.469Z |
| 50 | `0/index-SaAVz3TUQxmdIFF0FUuMvA` | 20:51:58.609Z | 21:33:19.469Z |
| 51 | `0/index-_TWeLty-S7W6lvsc0MU0_A` | 21:33:15.059Z | 21:33:15.059Z |
| 52 | `0/index-klC8nCXeQxi9L6js9YsMlw` | 21:33:19.469Z | 21:33:19.469Z |
| 53 | `0/snap-vOlMaZYhRRegyN_e3zV3pA.dat` | 20:51:58.609Z | 20:51:58.609Z |

Row counts by group: 12 + 7 + 7 + 5 + 10 + 7 + 5 = **53**.

### 12e. Applying it

`d8b-applied.txt`, verbatim and complete:

```
deleted: 53, already-gone: 0
```

Fifty-three requested, fifty-three deleted, zero already absent. The
`already-gone: 0` is the interesting half: it means every key the log condemned
was still physically present in the bucket at delete time. Nothing had been
cleaned up in the interval, which is exactly what "the delete leaked" means.

See §15 for how these deletes were applied and why that matters.

---

## 13. Post-sweep verification

Everything in this section is read-only. All of it was re-verified at write time
against the still-running rig except §13e, which is recomputed from the captured
mirror.

### 13a. Catalog and generation

`d9-catalog.txt`, verbatim and complete:

```
rig-daily-2026.08.24-xev01ty8rawen8yasu9qsw SUCCESS
frozen-base-metrics                         SUCCESS
```

Re-verified at write time, with index counts:

```
$ curl -s -u "elastic:$PW" "http://localhost:9202/_cat/snapshots/oci-repro?h=id,status,indices&v"
id                                          status indices
rig-daily-2026.08.24-xev01ty8rawen8yasu9qsw SUCCESS       3
frozen-base-metrics                         SUCCESS       1
```

Two snapshots survive on the S3 repository: one leftover SLM daily from before
the repoint in §8, and the pinned mount snapshot backing `frozen-metrics`. The
`snap-2` and `hybrid-scrap` snapshots deleted in §11 are gone from the catalog,
as intended.

The live root generation is **9**, read at write time from
`repo-mirror4/index-9` and confirmed by `_verify_integrity` in §13d. Its
`indices` block names three live indices:

| Index | UUID | Referenced by |
|---|---|---|
| `.snapshot-blob-cache` | `2ROCq0IpRKaD_tSgwrcq4A` | `rig-daily-…-xev01ty8` |
| `metrics-sys` | `BtIUzj_XSz-Ygq0AfcOMIg` | `rig-daily-…-xev01ty8`, `frozen-base-metrics` |
| `.security-7` | `uWYEV8XgTRq9NPAtuedM9Q` | `rig-daily-…-xev01ty8` |

### 13b. `index-8` and `index-9` both survived

This is §12c's guard paying off. The final bucket root, from `repo-mirror4/`:

| Root object | Bytes | Classification |
|---|---:|---|
| `index.latest` | 8 | LIVE (root pointer) |
| `index-9` | 1,483 | LIVE (current root generation) |
| `index-8` | 2,121 | **PROTECTED (previous root generation, guarded)** |
| `meta-4JDrYJluRo6MNA1UTFqsOA.dat` | 38,286 | LIVE |
| `meta-Nv-V5l2SSUuq5bW3f4iqgQ.dat` | 37,879 | LIVE |
| `snap-4JDrYJluRo6MNA1UTFqsOA.dat` | 452 | LIVE |
| `snap-Nv-V5l2SSUuq5bW3f4iqgQ.dat` | 326 | LIVE |

Elasticsearch's log at `21:33:19.467Z` listed `index-8` among blobs "no longer
part of any snapshot" (§11b). The sweeper skipped it as the numerically-highest
`index-N` in its directory. The repository then advanced to generation 9. Both
blobs are present; the repository is intact.

Note also that `index-2` through `index-7`, all six root generations in the
manifest's first group, are gone from the final root listing. The guard is
narrow: it withheld exactly one key, and it was the right one.

### 13c. Frozen search after a shared-cache clear

`d9-frozen.json`, verbatim and complete:

```json
{"_shards":{"failed":0},"hits":{"total":{"value":3500}}}
```

Identical to the pre-sweep result in §10b: 3,500 hits, zero failed shards. The
shared cache was cleared before this search, so the query had to re-fetch from
the S3 bucket rather than serve from a warm local cache. This is the check that
would fail if the sweep had deleted a blob the mount depends on.

Re-verified at write time (with `_shards.total` added):

```
$ curl -s -u "elastic:$PW" \
    "http://localhost:9202/frozen-metrics/_search?size=0&filter_path=_shards.failed,_shards.total,hits.total.value"
{"_shards":{"total":1,"failed":0},"hits":{"total":{"value":3500}}}
```

### 13d. `_verify_integrity`

`d9-integrity.json` was truncated on capture: the file is 301 bytes and ends
mid-token at `"total_a`. What it does contain, verbatim:

```json
{"status": {"repository": {"name": "oci-repro", "uuid": "9QLApiRcTdC86SxVVE-SrQ",
 "generation": 9}, "snapshots": {"verified": 2, "total": 2},
 "indices": {"verified": 3, "total": 3},
 "index_snapshots": {"verified": 4, "total": 4},
 "blobs": {"verified": 28}}, "final_repository_generation": 9, "total_a
```

The missing tail is the verdict, which is the part that matters. Rather than
guess it, the check was re-run at write time against the live rig:

```
$ curl -s -u "elastic:$PW" -X POST "http://localhost:9202/_snapshot/oci-repro/_verify_integrity"
```

```json
{
  "results": {
    "status": {
      "repository": { "name": "oci-repro", "uuid": "9QLApiRcTdC86SxVVE-SrQ", "generation": 9 },
      "snapshots":       { "verified": 2, "total": 2 },
      "indices":         { "verified": 3, "total": 3 },
      "index_snapshots": { "verified": 4, "total": 4 },
      "blobs":           { "verified": 28 }
    },
    "final_repository_generation": 9,
    "total_anomalies": 0,
    "result": "pass"
  }
}
```

Every field the truncated capture does contain matches the live re-run exactly,
and the tail resolves to `total_anomalies: 0, result: "pass"`. The repository is
sound after deleting 53 blobs from it.

`index_snapshots: 4` reconciles: `rig-daily-…-xev01ty8` covers 3 indices,
`frozen-base-metrics` covers 1.

### 13e. The residual reachability audit

The log-driven sweep only knows what the log told it. The reachability sweeper
was then run against the post-sweep mirror to find what was left. Re-run at write
time, read-only, against `repo-mirror4/` with the mounted-set file from §12a
(the file path in the pre-flight line is a session scratch path and is elided):

```
$ python3 oci_repo_sweeper.py --local-repo <repo-mirror4> \
    --mounted-snapshots <mounted-set file> --min-age-hours 0 --emit orphans

INFO listing repository objects...
INFO 37 objects, 470.8 KiB
INFO loading root state (index.latest + index-N)...
INFO generation 9: 2 live snapshots, 3 live indices
INFO mounted-snapshot pre-flight OK: 1 entry from <mounted-set file> all present
     in the catalog (their blobs are LIVE via index-9)
class         objects          bytes
LIVE               30      465.2 KiB
ORPHAN              4        3.5 KiB
PROTECTED           3        2.1 KiB
```

**The pre-flight OK line is the counterpart to the DANGER abort quoted at the top
of Part II.** Same check, same file format, opposite verdict, and it names why:
the mounted snapshot's blobs are LIVE via `index-9`, so sweeping cannot destroy
the mount.

The breakdown, verbatim:

```
breakdown:
  LIVE              14  segment vs current shard file set
  LIVE               4  shard snap-<uuid> vs live snapshots
  LIVE               3  index meta vs metadata identifiers
  LIVE               3  shard index-<gen> vs current gen
  ORPHAN             3  shard snap-<uuid> vs live snapshots
  LIVE               2  root snap-<uuid> vs live snapshots
  LIVE               2  root meta-<uuid> vs live snapshots
  PROTECTED          2  unrecognized path shape
  LIVE               1  root pointer
  LIVE               1  current root generation
  PROTECTED          1  previous root generation (guarded)
  ORPHAN             1  shard index-<gen> vs current gen
INFO dry-run: 4 orphans (3.5 KiB) WOULD be deleted; re-run with --execute to delete
```

All 4 residual orphans, verbatim from `d9-residual-orphans.tsv` (the timestamp
columns are mirror filesystem times, same caveat as campaign 1's §2, so they are
not usable as evidence of object age):

| # | Index | Key | Bytes | Reason |
|---:|---|---|---:|---|
| 1 | `metrics-sys` | `indices/BtIUzj_XSz-Ygq0AfcOMIg/0/snap-Akwm7mJyTw-vZoEtd13mRw.dat` | 936 | shard snap-\<uuid\> vs live snapshots |
| 2 | `.security-7` | `indices/uWYEV8XgTRq9NPAtuedM9Q/0/index-TY7jRihSR_Ka1K9TUde0oQ` | 924 | shard index-\<gen\> vs current gen |
| 3 | `.security-7` | `indices/uWYEV8XgTRq9NPAtuedM9Q/0/snap-Akwm7mJyTw-vZoEtd13mRw.dat` | 872 | shard snap-\<uuid\> vs live snapshots |
| 4 | `.security-7` | `indices/uWYEV8XgTRq9NPAtuedM9Q/0/snap-fkvwBzpBR_2TNCwiHHkWWA.dat` | 879 | shard snap-\<uuid\> vs live snapshots |
| | | **TOTAL** | **3,611** | |

3,611 bytes is 3.53 KiB, matching the sweeper's `3.5 KiB`.

The 3 PROTECTED objects are `index-8` (§13b) at 2,121 bytes and the two campaign-1
verification scratch blobs at 22 bytes each: 2,165 bytes, matching `2.1 KiB`.
Class bytes reconcile against the mirror exactly: 476,338 LIVE + 3,611 ORPHAN +
2,165 PROTECTED = **482,114** bytes = 37 files = the whole of `repo-mirror4/`.

**A reconciliation that works out, worth recording.** Campaign 1's §5b left an
unexplained one-object gap between `blobs.verified` (40) and the mirror file count
(39). Here the gap has an exact explanation in the other direction: 30 LIVE
objects minus `index.latest` (the root pointer) minus `index-9` (the current root
generation) is **28**, which is precisely `blobs.verified`. ES's blob accounting
excludes the root pointer and the live generation blob. That explains campaign 2's
numbers cleanly but does not retroactively explain campaign 1's, which went
the opposite way (more verified blobs than mirror files). Campaign 1's discrepancy
stays open.

### 13f. Recall this round

53 keys were swept from the log-driven manifest (§12e). 4 orphans remained
(§13e). Total orphans present at sweep time: **57**.

| | Campaign 1 | Campaign 2 |
|---|---:|---:|
| Orphans present | 28 | 57 |
| Recovered from logs | 20 | 53 |
| **Key recall** | **71.4%** | **93.0%** |
| Byte recall | 56.7% | *not computable, see below* |
| False positives | 0 | 0 |

Campaign 2's key recall is 53 / 57 = **93.0%**, against campaign 1's 71.4%.

**Byte recall is not computable for campaign 2, and no figure is offered.** It
would require a bucket mirror taken between the `d6` deletions (`21:33`) and the
`d8b` sweep (also `21:33`). No such mirror exists: `repo-mirror3/` predates the
deletions and `repo-mirror4/` postdates the sweep, so neither bounds the swept
bytes. Campaign 1's 56.7% has no counterpart here.

**Recall varies with batch composition, and neither number is a property of the
tool.** `limit(10)` truncates each failing delete batch to its first ten keys, so
what a log preserves depends entirely on how ES happened to partition the deletes:
how many batches, how large each one, and how the blobs distributed across
index shards. Campaign 1 lost 8 of 28 keys to a single 18-blob batch truncated at
10. Campaign 2 hit the 10-key ceiling six separate times but across fourteen WARN
lines and many more batches, so proportionally less fell off the end. A different
delete pattern would produce a different figure in either direction. **The number
that is stable across both campaigns is the false-positive count: zero, both
times.** Neither run condemned a key that was actually reachable. Recall is
best-effort; safety is not.

---

## 14. Restoring from the fs repository

The last step closes the loop: a backup that lives on the fs repository has to be
restorable, or the whole topology is theatre.

`d10-restore.txt`, verbatim and complete:

```
restore shards: {'total': 1, 'failed': 0, 'successful': 1}
```

`d10-count.txt`, verbatim and complete:

```
3500
```

One shard requested, one restored, zero failed, and the restored index holds
3,500 documents, the same count as the source index `logs-app` in §10b.

Re-verified at write time:

```
$ curl -s -u "elastic:$PW" "http://localhost:9202/_cat/count/fs-restored-logs-app?h=count"
3500
```

`fs-restored-logs-app` reports `store.size` of `280.9kb` in §10b's listing, byte-
for-byte identical to `logs-app` itself. The data made the round trip through a
filesystem repository that has real deletes, and came back whole.

---

## 15. Chain of custody (campaign 2)

All times below are local (UTC-7); the pod log is in UTC, 7 hours ahead. The run
occupied roughly two minutes of wall clock, **14:32** to **14:34**, on top of a
rig whose pod log reaches back to `20:50:21Z` (13:50 local).

`d6-es-pod.log` was captured at **14:33** and covers 338 lines spanning
`20:50:21.123Z` to `21:33:19.474Z` UTC. `repo-mirror3/` was pulled at **14:14**
(70 objects, 896,266 B), before the `d6` deletions. `d8-log-manifest.tsv` and
`d8-summary.txt` were written at **14:33** by `es_log_driven_sweeper.py` reading
that pod log; `d8b-applied.txt` records the deletions applied immediately after.
`repo-mirror4/` was pulled at **14:34** (37 objects, 482,114 B) as the final
post-sweep state, and `d9-*` was captured against it and the live rig in the same
minute. The live-rig commands marked "re-verified at write time" were run later,
against the still-running port-forward.

### 15a. Script versions

Unlike campaign 1, the code that produced these artifacts is still exactly what
is on disk. Every commit touching the three scripts predates the run, and the
working tree was clean for all three at write time.

```
$ git log --oneline -1
4c5e7ba docs: per-suite counts 72/53/191 = 316
```

| Script | Produced | Commit at run time | Commit time | Still current? |
|---|---|---|---|---|
| `snapshot_sizes.py` | `d1-sizing.txt`, `d1-classified.tsv`, `d1-classified.err`, `d7-mounted.txt` | `782b474`, *comments: how backups vs mounted (storage) snapshots are differentiated* | 14:27 | yes |
| `es_log_driven_sweeper.py` | `d8-log-manifest.tsv`, `d8-summary.txt` | `0e4a3aa`, *`--min-object-age` settling window (fail-closed)* | 14:28 | yes |
| `oci_repo_sweeper.py` | `d9-residual-orphans.tsv` | `782b474`, *comments: how backups vs mounted (storage) snapshots are differentiated* | 14:27 | yes |

`git status --porcelain` reports no modification to any of the three at write
time, and repository `HEAD` (`4c5e7ba`, 14:29) touches documentation only. So the
§13e re-run quoted above used byte-identical code to the original `d9` capture,
which is why it reproduces the 4-orphan / 3.5 KiB result exactly.

Campaign 1's §6 needed two columns here because both sweepers received
adversarial-hardening commits *after* their artifacts were captured. Campaign 2
does not.

### 15b. Caveat: the deletes bypassed the sweeper's `--execute` path

`d8b-applied.txt` (`deleted: 53, already-gone: 0`) is the result of applying the
53-key manifest through an S3 client (`mc rm`), not through
`es_log_driven_sweeper.py --execute`.

**What that skipped:** the sweeper's `--execute` path performs a delete-time
cross-check against the repository's *live* generation before removing each key.
That guard exists because a manifest is a snapshot of the past: between manifest
generation and deletion, a new snapshot can land, a generation can advance, and a
key that was orphaned when the manifest was written can become reachable.
Applying the manifest with a raw object-store client skips that entirely and
deletes whatever the file says.

**Why it was acceptable here, stated so a reader can check the reasoning:** the
manifest was generated and applied inside the same minute (both at 14:33) with no
snapshot activity in between; the mounted-snapshot pre-flight had already passed
(§12a, §13e); the rig has one client and no concurrent writers; and the outcome
was verified afterwards by both `_verify_integrity` (§13d, `total_anomalies: 0`)
and a frozen search after a cache clear (§13c). The `already-gone: 0` line also
confirms the bucket had not changed between manifest and delete.

**None of that transfers to production.** A production sweep must use
`es_log_driven_sweeper.py --execute` so the live-generation cross-check runs at
delete time. The evidence in §12e demonstrates that the manifest was correct; it
does not demonstrate that bypassing the guard is safe.

### 15c. Caveat: the fs repository is an ephemeral-volume stand-in

`backups-fs` points at `/mnt/es-repo/backups`, a volume inside the Elasticsearch
pod. A real deployment would use a shared network filesystem mounted identically
on every node, because a filesystem repository must be reachable from all of
them.

**What the stand-in does and does not prove.** It does prove the property the
hybrid depends on: on a POSIX filesystem repository, deleting a snapshot unlinks
its blobs (§9, 26 → 2), where the same operation against the rejecting object
store leaks (campaign 1, §1: 28 blobs, 378,142 bytes). That is a property of the
repository *type*, not of the specific volume. It does not exercise multi-node
mount consistency, network-filesystem locking, or the failure modes a shared
mount introduces, none of which this single-node rig can reach.

### 15d. Caveat (RESOLVED): the sweeper's "66 unique keys"

`d8-summary.txt` reports `parsed 338 lines, 66 unique keys` and then 53 eligible
keys with 3 declared skips.

An independent extraction at write time, a regex over every
`ObjectIdentifier(Key=…)` occurrence in the `error.message` and
`error.stack_trace` fields of all 338 lines, finds 56 distinct keys. That
figure reconciles perfectly with the manifest: 56 = 53 manifest rows + 1 guard
skip (`index-8`) + 2 unknown-shape skips, and the manifest contains no key absent
from the extraction. Widening the extraction to include the untruncated bracket
lists in the `message` field adds only one further string, and it is not a blob
key at all (a data-path fragment from an unrelated log line).

So the eligible/skipped arithmetic is internally sound, and the apparent
10-key gap resolved on further extraction: the independent count covered
only the `ObjectIdentifier(Key=…)` wire format, while the log's
`Found stale root level blobs [...]` INFO lines carry keys as bare
root-relative names in a plain bracket list. Re-extracting those lines yields
exactly 10 unique bare names (`index-2`…`index-7`, two `snap-*.dat`, two
`meta-*.dat`):

    56 ObjectIdentifier-form keys + 10 stale-root bare names = 66 unique parsed

The sweeper counts both wire forms before dedup; its cross-form merge then
collapses each relative name onto its absolute counterpart, which is how 66
parsed keys become 53 manifest rows + 3 skips. The two counting methods
differ; neither is wrong, and the discrepancy is fully reconciled.

It never affected any number used in this document: the manifest is 53 rows,
all 53 were applied, and the residual audit independently found the 4 that
remained.

### 15e. Caveat: `d9-integrity.json` is truncated

The captured file ends mid-token and does not contain the verdict. §13d quotes
what it does contain and supplies the missing tail from a live re-run whose every
overlapping field matches. Stated rather than silently completed.

### 15f. What depends on the rig staying up

Everything marked "re-verified at write time" is re-runnable against the live rig
as long as the port-forward holds: the license, the repository list, the SLM
policy, the catalog, `_verify_integrity`, the frozen search, the fs repository
file listing, and the restored document count. Everything else is fixed evidence
from files on disk, plus one recomputation (§13e) that runs against a captured
mirror and needs no rig at all.
