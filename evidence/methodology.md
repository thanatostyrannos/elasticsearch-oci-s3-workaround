# Testing methodology


> [!NOTE]
> **The sweep runs described here were driven by tools that are now retired.**
> `s3_repo_sweeper.py`, `oci_repo_sweeper.py` and `es_log_driven_sweeper.py`
> have been removed; see
> the main README.
>
> The measurements are kept because they are not measurements of those tools.
> What a wrong delete costs, what a mounted searchable snapshot is linked to,
> what a restore returns and what `_verify_integrity` fails to notice are
> properties of Elasticsearch and the object store. The cost is the same
> whichever tool made the delete.
>
> Read the classification decisions and command invocations as history of
> retired tooling. Read the consequences as current.
How this tooling is validated, written so someone with no prior context
can execute it from scratch and get the same answers. `test-results.md` records
what happened on a given day; this document is the playbook that produced it.

The headline validation target is the split-repo hybrid ("Strategy D"): move
backups off the S3-compatible endpoint onto block/NFS storage as an `fs` repository,
leave the frozen tier mounted where it already is on the S3-compatible repository
with Elastic support's `verify=false` registration applied, and keep that
repository swept with log-driven cleanup plus an age-settling window. Layer 2
below runs two campaigns against a live rig, the local test lab that reproduces
the fault (defined in [FACTS.md](../FACTS.md#the-test-lab-henceforth-the-rig)): the first reproduces the underlying
fault and proves the sweepers, the second stands the hybrid up end to end and
proves each of its load-bearing claims.

Three layers, cheapest first. Each catches a class of defect the previous layer
structurally cannot see.

| Layer | Cost | Runtime | Catches |
|---|---|---|---|
| 1. Unit + embedded self-test | free, offline | a few seconds | logic, guards, parsers, CLI contract |
| 2a. Live-rig E2E, fault reproduction and sweeping | one laptop | ~15 min | wrong assumptions about real ES bytes and real failure modes |
| 2b. Live-rig E2E, split-repo hybrid | one laptop | ~20 min | whether the recommended production architecture actually holds together, step by step |
| 3. Adversarial review | reviewer time | hours | designed-in blind spots nobody wrote a test for |

---

## 1. Principles

### 1.1 The safety invariant under test

Everything in this suite exists to defend one sentence:

> **Nothing is ever deleted without `--execute`, and `--execute` alone is not
> enough interactively: a typed `DELETE <count>` confirmation is required.
> Non-TTY runs refuse outright, and no flag overrides that.**

Both sweepers delete objects out of a production snapshot repository. A false
positive is not a failed test, it is unrecoverable data loss. So the invariant is
attacked from several directions per script rather than asserted once.

### 1.2 Fail-safe-protect classification

The reachability sweeper answers one of three things about every object: `LIVE`,
`ORPHAN`, `PROTECTED`. The rule is that any surprise degrades the affected scope
to `PROTECTED`: a parse error, an unrecognized path shape, a missing timestamp, a
mid-flight upload, a guarded generation.

That biases every *detected* failure toward under-deletion. Under-deletion costs
storage; over-deletion costs a restore. When you write a test for this codebase,
ask "which way does the failure fall?" A test that only proves an orphan gets
deleted is half a test. The other half is proving that everything else survived.

**The rule covers thrown failures, and a second layer covers one shape of silent
failure.** A decoder that throws degrades its scope to `PROTECTED`. A decoder that
returns something which is not a shard file list at all does not throw, so it is
caught separately by a shape gate, which protects the affected shard and turns the
run into a danger state: `--execute` aborts, dry-run reclassifies every orphan
`PROTECTED`. Alongside it sit a shape gate on the root catalog, a per shard
circuit breaker for a cleanly parsed shard that condemns all of its own segments,
a stale-pointer pre-flight, and a blocking blob-count reconciliation under
`--cross-check`.

**What remains uncovered is a decode that returns a plausible wrong file list at
the right file entry count**, on a run that did not pass `--cross-check` or that
overrode a guard. Do not write "bugs cause under-deletion, never data loss"
anywhere. It is true of the classes of bug these layers catch and false of the one
they do not, which is the worst possible shape for a safety claim.

The practical consequence for test design: a test that garbles a blob and asserts
`PROTECTED` proves the fail-safe path, and a test that decodes to an empty file
list proves the shape gate. Neither says anything about a well formed wrong
answer. The runbooks of the day answered that case outside the tool, with bucket
versioning plus a dated manifest, and every path that reached `--execute` carried
both as hard prerequisites.

> **Correction, written when the sweepers were retired.** That answer does not
> hold on the endpoint this project exists for. Oracle's Amazon S3 Compatibility
> API has no `ListObjectVersions`, so the version id of a deleted object can
> never be discovered, and an id nobody can discover is an id nobody can ask
> for. `GetBucketVersioning` and `PutBucketVersioning` are absent from that
> surface as well, so an operator restricted to it can neither turn versioning
> on nor confirm that it is on. Read the sentence above as what the runbooks
> claimed while these runs happened, not as advice. The recovery path is a copy
> held outside the bucket. The operation lists are in
> [blast radius](../docs/blast-radius.md#there-is-no-recovery-path-through-the-amazon-s3-compatibility-api).

### 1.3 The architecture under validation: the split-repo hybrid

The fault is narrow: ES sends `x-amz-checksum-crc32` on `DeleteObjects`, the
S3-compatible endpoint rejects the batch for a missing `Content-Md5`, and the
snapshot delete reports success while the blobs stay. Everything downstream of
that follows from deletes silently failing: leaked bytes, retention that never
reclaims, a repository that only grows.

The hybrid splits the problem by *which repository actually needs to delete*:

- Backups move to an `fs` repository on block or NFS storage. Deletes there
  are filesystem unlinks. No checksum header is involved, so SLM retention works
  on its own, with no tooling in the loop at all. This is where the fault hurt
  most, and it is removed rather than mitigated.
- The frozen tier stays on the S3-compatible repository. Searchable snapshots are
  mounted against a specific repository; relocating them means re-uploading and
  re-mounting the entire tier, which is exactly the cost the frozen tier exists to
  avoid. Instead the repository is re-registered with `?verify=false`, Elastic
  support's documented workaround for this fault, so registration succeeds and
  reads keep working. Reads never issue a `DeleteObjects`, so the fault is not on
  the serving path.
- That repository still leaks on delete, so it is maintained by log-driven
  cleanup: parse the keys ES itself condemned out of its own WARN lines, settle
  them against an object-age window, and delete exactly those.

The consequence worth stating plainly: the leak is not eliminated, it is confined
and bounded. It applies only to a repository whose deletion traffic is now
occasional rather than daily, and the residue is measurable. Section 4 measures
it.

### 1.4 Why three layers

- Layer 1 proves the logic. It runs against synthetic repositories the test
  builds itself, so it can assert exact expected classifications for every object.
  It cannot tell you whether your idea of an ES blob matches the bytes ES writes.
- Layer 2 proves the assumptions. A real Elasticsearch, a real Jackson SMILE
  (binary JSON, [format specification](https://github.com/FasterXML/smile-format-specification/blob/master/smile-specification.md))
  encoder, a real S3 endpoint that really rejects the delete. This is the only
  layer that can invalidate a format assumption, or an architecture assumption,
  which is what campaign 2b is for.
- Layer 3 proves the design. Both other layers only test what somebody thought
  to test. Adversarial review asks "what did the author not think of?" and reads
  the ES source as ground truth instead of the tool's own docstrings.

### 1.5 The loop-closing rule

Every confirmed adversarial finding becomes a fix plus a regression test before
the loop closes. Not a fix. Not a TODO. Fix + test + full suite green. A finding
that is fixed without a test is a finding that will come back, because the next
refactor has nothing telling it to stop.

The same rule applies to bugs found in Layer 2: the two E2E bugs (duplicate
manifest rows, naive/aware timestamp crash) each shipped with a unit test that
fails against the pre-fix code.

---

## 2. Layer 1: unit tests and embedded self-tests

Run before every commit. No network, no cluster, no credentials.

```bash
cd elasticsearch-oci-s3-workaround
python3 -m unittest discover -s tests -v      # the unit suite
python3 oci_repo_sweeper.py --self-test        # embedded, builds a synthetic repo
python3 es_log_driven_sweeper.py --self-test   # embedded, parses log fixtures
```

Expected: every test passes in a few seconds, and both self-tests print an `OK` line
naming what they verified. `oci_repo_sweeper.py` prints `SELF-TEST OK` followed by
its classification count and confirmation of the SMILE round-trip, the CRC guard,
and delete plus state resume. `es_log_driven_sweeper.py` prints `self-test: OK`
with its unique-key count and how many are eligible under `es-snapshots/`. Any
non-zero exit is a hard stop.

### 2.1 How the tests actually prove things

A synthetic repository is built on disk per test. `build_repo()` in
`tests/test_oci_repo_sweeper.py` writes a real 8-byte big-endian `index.latest`, a
real root `index-N` JSON with snapshot/index/shard-generation maps, and a shard
`index-<gen>` blob that is genuinely SMILE-encoded, DEFLATE-compressed, and
Lucene-codec-wrapped by `_smile_encode_for_test()` / `_wrap_for_test()`. The
parse path under test is therefore the production parse path, not a mock. The
function returns a `{relpath: expected class}` map, so classification assertions
are exhaustive over the repo, not spot checks.

Deletion is observed at the filesystem level, over the full file set.
`all_files()` walks the tree and returns every relative path. Every deletion
assertion compares the complete set before and after, which proves both "the
orphans are gone" and "nothing else was touched". This is what makes the tests a
real guard against over-deletion rather than a check that deletion works.

The typed-confirmation gate is driven through a real pty. `run_cli_pty()` uses
`pty.fork()` + `os.execvp()` so the CLI sees a genuine TTY, then types the answer
under test: wrong phrase (`DELETE 999` against 4 orphans), empty line, and the
correct phrase. Exit code and surviving file set are asserted for each. Non-TTY
refusal is a separate path, driven with `stdin=subprocess.DEVNULL`.

The `oci` SDK is deliberately absent from the test environment. Any dry-run
code path that tried to construct an `oci` SDK client would raise `ImportError` and fail
the test loudly. A green suite is therefore positive evidence that dry-runs touch
no network. The guard is structural, not a promise in a docstring. Keep it that
way: do not `pip install oci` into the test environment.
`TestOciModeGuards.test_oci_mode_without_oci_package_fails_closed` skips itself if
the SDK is present, so an accidental install degrades the signal silently. Check
for the skip in verbose output.

The same absence is why `--min-object-age` has no live coverage, as §6 records.
Its HEAD pass needs the Object Storage API, so it is exercised here against an
injectable fake client rather than on the rig.

Real ES 9.5.2 blobs are pinned in `tests/fixtures/`. Three files harvested from
the live rig, so the decoders are exercised against bytes Elasticsearch actually
wrote rather than bytes the test wrote:

| Fixture | What it is |
|---|---|
| `real-es952-index.latest` | 8 bytes, `00 00 00 00 00 00 00 03`, the root-generation pointer |
| `real-es952-root-index-3.json` | the real `RepositoryData` root JSON (snapshot uuids, `index_metadata_lookup`, shard generations) |
| `real-es952-shard-index-gen.bin` | a shard-level `BlobStoreIndexShardSnapshots` blob: Lucene codec header (`3f d7 6c 17`, name `snapshots`), `DFL\0` marker, DEFLATE payload, Jackson-written SMILE inside |

These are the regression anchor for the SMILE decoder. Synthetic SMILE only
exercises what the test encoder emits; the Jackson-written fixture exercises what
Jackson emits, including shared-string-table behavior.

### 2.2 Test inventory

`tests/test_oci_repo_sweeper.py`, the reachability sweeper:

| Class | Covers |
|---|---|
| `TestNothingDeletedWithoutExecute` | default dry-run deletes nothing (exit 0, file set identical, "WOULD be deleted" on stderr); dry-run with `--out FILE`; `--emit reachable`; `--execute` with non-TTY stdin refuses (exit 1, "refusing to delete"); pty wrong phrase aborts; pty empty answer aborts |
| `TestExecuteDeletesExactlyOrphans` | correct `DELETE <n>` deletes exactly the orphans and nothing else; state-file resume, where after run 1 the orphans are recreated on disk and run 2 with the same state file reports `0 deleted` and leaves them, proving keys are skipped by state, not merely by absence |
| `TestClassification` | the whole synthetic repo classifies exactly as expected; corrupt shard `index-<gen>` flips that shard's segments (including a known orphan) to `PROTECTED`; unknown path shapes are `PROTECTED`; the previous root generation `index-N-1` is guarded; a truncated `index.latest` raises `BlobFormatError` instead of guessing |
| `TestManifest` | orphan TSV header is exactly `key size_bytes created last_modified last_accessed reason`, one row per orphan and no others, ISO dates present; `--emit reachable` contains every non-orphan and no orphan |
| `TestBlobParsing` | SMILE round-trip over objects/arrays/small+large ints/strings/null/bool; garbage and truncated SMILE raise `SmileError`; Lucene footer CRC corruption rejected; bad header magic rejected; both the JSON and DEFLATE payload paths unwrap; the DEFLATE fixture really contains the `DFL` header (so the compressed path is genuinely exercised) |
| `TestOciModeGuards` | bucket mode without the `oci` SDK exits non-zero with the `pip install oci` hint and touches nothing; missing source arguments exit 2 (argparse) |
| `TestMountedSnapshotListParsing`, `TestMountedSnapshotCatalogMatch`, `TestMountedSnapshotsAllPresent`, `TestMountedSnapshotDangerState`, `TestMountedSnapshotAdvisory` | the `--mounted-snapshots` pre-flight: the list file takes the first token per line, tolerates `#` comments, blank lines and duplicates; entries match the catalog by **either** snapshot name or uuid; an entry absent from the catalog (including a deleted snapshot's uuid) is reported missing, prints the DANGER banner, and under `--execute` aborts with exit 1 having deleted nothing; an all-present list passes, prints a one-line confirmation naming the entry count and root generation, and leaves orphan classification unchanged; omitting the flag entirely prints the advisory under `--execute` |

`tests/test_es_log_driven_sweeper.py`, the log-driven sweeper:

| Class | Covers |
|---|---|
| `TestNothingDeletedWithoutExecute` | dry-run is the default, emits the TSV manifest, exit 0, no `oci` SDK client constructed; `--out FILE` writes the manifest and `last_accessed` is `-` (Object Storage exposes no atime); `--execute` on non-TTY stdin exits 1 before any network code; the embedded `--self-test` passes when shelled out |
| `TestConfirmation` | `check_confirmation` as a pure function: accepts exactly `DELETE <N>` with outer whitespace tolerated; rejects `delete 5`, `DELETE 4`, doubled inner spaces, trailing words, `yes`, and empty |
| `TestGuards` | `index.latest` is never eligible, even with `--allow-any-key`; the highest `index-N` seen is guarded and lands in `plan.skipped` while lower generations stay eligible; prefix rail (another repo's keys quoted in the same line are never eligible); allowlist rail (non-ES-shaped names need `--allow-any-key`); segment blobs and `.partK` pieces are eligible; dedup regression, where a key condemned both root-relative and absolute appears once with merged stats, and repeated `build_plan` calls give identical results (no mutation of parse output) |
| `TestParser` | duplicate mentions across plain stacktrace + ECS + docker-wrapped ECS aggregate to `occurrences == 3` with coherent first/last-seen; pure noise lines count as skipped and match nothing; `... (N in total, M omitted)` truncation markers are counted; `classify_blob_name` accepts the real ES shapes and rejects lookalikes, because the segment rail demands ≥ 10 characters after `__`, so `__short` is rejected |
| `TestMinObjectAge` | the age-settling window against an injectable fake object-store client: an object whose `Last-Modified` is inside the window is excluded and annotated `too-young`; an object whose metadata cannot be read is excluded fail-closed as `age-unknown`; excluded keys stay in the manifest but are dropped from the `DELETE <N>` count; the flag refuses without `--fetch-metadata` |

### 2.3 What the embedded self-tests add

The `--self-test` flags are not duplicates of the unit suite; they ship inside the
scripts so an operator can validate a copy on a jump host with no test directory.

- `oci_repo_sweeper.py --self-test`: builds a 22-object synthetic repo covering
  live/stale root generations, live and dead snapshot and meta blobs, a dead index
  directory, an unparseable shard generation, and an age-guarded fresh blob; then
  runs SMILE round-trip, the CRC guard, the orphan manifest shape, a real delete
  pass against the local source, and a second delete pass proving state resume
  returns `0/0/0`.
- `es_log_driven_sweeper.py --self-test`: parses 13 keys out of four real ES
  log shapes (plain-text WARN + IOException stacktrace, per-key
  `[key][code][message]` WARN with truncation suffix, TRACE per-key line, INFO
  stale-root-blobs line) plus ECS JSON and docker-wrapped ECS variants, asserts the
  matched/skipped/sdk-exception line counts, then checks every safety rail, the
  no-prefix unresolvable path, the manifest column contract (including
  `already-gone` rows), and the confirmation gate.

---

## 3. Layer 2, campaign 1: fault reproduction and sweeping

A real ECK-managed Elasticsearch writing to a real S3 endpoint that reproduces the
fault. ~15 minutes end to end once the cluster is up. Everything below is
replayable.

### 3.0 kubectl context discipline

Every `kubectl` command in this playbook carries an explicit
`--context rancher-desktop`. Never rely on the default context. This rig creates
a namespace, deletes snapshots, and deletes bucket objects. Running any of it
against whatever context happens to be current is how a test rig becomes an
incident.

```bash
export CTX="--context rancher-desktop"
kubectl $CTX config current-context   # sanity: must print rancher-desktop
```

### 3.1 Bring the rig up

Manifests are in `manifests/`. Read `manifests/README.md` first. In particular,
`minio/minio:RELEASE.2025-01-18T00-31-37Z` is pinned because it is the last
MinIO release that rejects `DeleteObjects` carrying only `x-amz-checksum-crc32`
with no `Content-MD5`, exactly like the affected Amazon S3 Compatibility API. The next release
accepts. Upgrading it silently destroys the rig's entire purpose.

```bash
# 1. ECK operator (not vendored, ~850KB of CRDs)
curl -fsSL https://download.elastic.co/downloads/eck/3.5.0/crds.yaml | kubectl $CTX create -f -
curl -fsSL https://download.elastic.co/downloads/eck/3.5.0/operator.yaml | kubectl $CTX apply -f -

# 2. Replace every CHANGEME-access / CHANGEME-secret with throwaway credentials.
#    Three files, values must match across all three:
#    minio.yaml, minio-bucket-job.yaml, s3-credentials-secret.yaml

# 3. The rig
kubectl $CTX apply -f manifests/namespace.yaml
kubectl $CTX apply -f manifests/minio.yaml
kubectl $CTX apply -f manifests/minio-bucket-job.yaml
kubectl $CTX apply -f manifests/s3-credentials-secret.yaml
kubectl $CTX apply -f manifests/elasticsearch.yaml

kubectl $CTX get elasticsearch -n es-rig -w        # wait for health=green
```

Port-forward and grab the password:

```bash
kubectl $CTX -n es-rig port-forward svc/rig-es-http 9200:9200 &
kubectl $CTX -n es-rig port-forward svc/minio 9000:9000 &
PW=$(kubectl $CTX -n es-rig get secret rig-es-elastic-user \
      -o go-template='{{.data.elastic | base64decode}}')
ES="curl -sk -u elastic:$PW http://localhost:9200"
```

**Acceptance:** `$ES/_cluster/health` reports `"status":"green"`.

### 3.2 Reproduce the production fault

This step is not setup. It is the assertion that the rig reproduces the incident.

```bash
# Registration WITH verification must fail with the production error string:
$ES -XPUT '/_snapshot/oci-repro' -H 'Content-Type: application/json' -d '{
  "type":"s3","settings":{"bucket":"es-snapshots","endpoint":"http://minio:9000",
  "path_style_access":true,"client":"default"}}'
```

**Acceptance:** the response contains, byte for byte,
`Missing required header for this request: Content-Md5`. If it does not, the MinIO
image was upgraded or the endpoint is wrong. Stop and fix the rig; nothing after
this point means anything.

```bash
# Register without verification (the Elastic KB workaround), then create churn:
$ES -XPUT '/_snapshot/oci-repro?verify=false' -H 'Content-Type: application/json' -d '{...same body...}'

# Bulk-index two indices, ~7000 docs total. Then:
$ES -XPUT '/_snapshot/oci-repro/snap-1?wait_for_completion=true'
# index more docs, then:
$ES -XPOST '/logs-a,logs-b/_forcemerge?max_num_segments=1'
$ES -XPUT '/_snapshot/oci-repro/snap-2?wait_for_completion=true'
# index more docs, then:
$ES -XPUT '/_snapshot/oci-repro/snap-3?wait_for_completion=true'

# The deletion that becomes a lie:
$ES -XDELETE '/_snapshot/oci-repro/snap-1'
```

**Acceptance:** the delete returns `{"acknowledged":true}` and `GET
/_snapshot/oci-repro/_all` no longer lists `snap-1`, while every underlying blob
delete failed. That gap is the entire reason these tools exist. Confirm it in the
log:

```bash
kubectl $CTX -n es-rig logs rig-es-default-0 > es-pod.log
grep -c "Failed to delete" es-pod.log      # expect >= 1 WARN cycle
```

**Acceptance:** at least one `Failed to delete some blobs` WARN carrying
`ObjectIdentifier(Key=...)` entries. The reference run produced five.

### 3.3 Mirror the bucket

The reachability sweeper is validated against a filesystem copy so the same run can
be repeated without re-fetching, and so a botched run cannot touch the live bucket.

```bash
mc alias set rig http://localhost:9000 "$ACCESS" "$SECRET"
mc mirror rig/es-snapshots ./repo-mirror
find repo-mirror -type f | wc -l          # reference run: 67 objects
```

`mc mirror` does not carry each object's `Last-Modified` onto the local copy, not
even with `--preserve`. In `--local-repo` mode the sweeper reads filesystem mtime
and nothing else, so an unstamped mirror makes the per-object age guard and the
active-scope guard inert together, with nothing printed. That is why the campaign
steps below run at `--min-age-hours 0`, which is honest about having no freshness
defense rather than pretending to one, and it is why an operator sweeping a real
repository must restamp the mirror from the store's own listing. The runbook
step that spelled that out, Step 5 of the es-orphan-sweep skill, is removed with
the tool it drove. The constraint is not about that tool: any tool reading a
local mirror inherits it.

### 3.4 Run the reachability sweeper

```bash
python3 oci_repo_sweeper.py --local-repo repo-mirror --min-age-hours 0 --out orphans.tsv
```

**Acceptance:**

- Zero SMILE parse failures. Every live shard's Jackson-written
  `index-<gen>` blob must decode. This is the real-world exam for the decoder;
  a parse failure here is a decoder bug that Layer 1 cannot see, because Layer 1
  encodes its own SMILE.
- The `PROTECTED` set is explainable object by object. In the reference run it was
  exactly the `tests-*` repository-verification leftovers (unrecognized shape ⇒
  protect) and the previous root generation (guarded). If something else shows up
  as `PROTECTED`, find out why before proceeding: an unexplained protect is a
  parse failure hiding behind the fail-safe rule.
- Reference classification for a 67-object mirror: 36 LIVE / 28 ORPHAN / 3
  PROTECTED.

### 3.5 Run the log-driven sweeper

```bash
python3 es_log_driven_sweeper.py es-pod.log --prefix / --out log-manifest.tsv
```

**Acceptance:** the manifest lists only keys ES itself condemned, `last_accessed`
is `-` on every row, and the run reports its truncation-marker count.

### 3.6 The two-sweeper cross-check

This is the highest-value step in the whole playbook. The two tools reach their
answers by completely independent means, one walking repository metadata and the
other reading log lines, so agreement is real evidence rather than a tautology.

```bash
comm -23 <(cut -f1 log-manifest.tsv | tail -n +2 | sort) \
         <(cut -f1 orphans.tsv      | tail -n +2 | sort)
```

**Acceptance: empty output.** `comm -23` prints keys present in the first list and
absent from the second, that is, keys the log-driven sweeper would delete which
the reachability sweeper does not consider orphans. Empty means every
log-condemned key is independently provable garbage, which is the *can't-touch-live-data*
property observed empirically rather than argued from the design.

The reverse direction is expected to be non-empty and must be explained, not
ignored: run `comm -13` on the same inputs. Those are orphans the logs missed. In
the reference run there were 8, matching the predicted `limit(10)` WARN-truncation
gap exactly. If the count does not match the truncation accounting, something
about the parser or the guards is wrong.

### 3.7 Sweep, re-mirror, re-audit

```bash
# Delete the manifest keys from the LIVE bucket
tail -n +2 orphans.tsv | cut -f1 | while read -r k; do mc rm "rig/es-snapshots/$k"; done

rm -rf repo-mirror && mc mirror rig/es-snapshots ./repo-mirror
python3 oci_repo_sweeper.py --local-repo repo-mirror --min-age-hours 0 --emit report
```

**Acceptance:** 0 orphans, and the LIVE count is unchanged from step 3.4 (36 in
the reference run). A LIVE count that dropped means the sweep deleted something it
should not have, which is the failure this whole document exists to catch.

### 3.8 Prove the repository still works

Zero orphans is not success. Success is that Elasticsearch can still read what is
left.

```bash
$ES -XPOST '/_snapshot/oci-repro/_verify_integrity'

$ES -XPOST '/_snapshot/oci-repro/snap-3/_restore?wait_for_completion=true' \
  -H 'Content-Type: application/json' -d '{
    "indices":"logs-a,logs-b","rename_pattern":"(.+)","rename_replacement":"restored-$1"}'

$ES '/_cat/count/logs-a?h=count'; $ES '/_cat/count/restored-logs-a?h=count'
$ES '/_cat/count/logs-b?h=count'; $ES '/_cat/count/restored-logs-b?h=count'
$ES '/_cat/indices/restored-*?h=index,pri,docs.count'
```

**Acceptance:** `_verify_integrity` walks every remaining snapshot with no errors;
the restore completes; doc counts match exactly between original and restored
indices (reference run: 3,500 / 3,500 in both, shard counts 2 / 2). Anything less
than exact equality is a failed E2E pass.

### 3.9 Exercise the sizing tool

```bash
python3 snapshot_sizes.py --es http://localhost:9200 --repo oci-repro \
  --user "elastic:$PW" --group day --recommend
```

**Acceptance:** the report renders, and on a young repository the caveat about the
growth window including the repository's first snapshot day, which overstates
growth, fires. A caveat that does *not* fire on a three-snapshot repo means the
caveat logic is broken.

### 3.10 Acceptance criteria, condensed

| Step | Must be true |
|---|---|
| 3.2 | exact string `Missing required header for this request: Content-Md5`; delete `acknowledged:true` while blob deletes fail |
| 3.4 | zero SMILE parse failures; every `PROTECTED` object explainable |
| 3.6 | `comm -23` output **empty**; reverse-direction gap matches truncation accounting |
| 3.7 | 0 orphans on re-audit; LIVE count unchanged |
| 3.8 | `_verify_integrity` clean; restored doc counts equal originals exactly |

---

## 4. Layer 2, campaign 2: the split-repo hybrid

Campaign 1 proves the tools. Campaign 2 proves the architecture described in
§1.3, one claim at a time, on the same rig with a frozen tier added: two ordinary
indices under an SLM policy, plus one index mounted as a partial (frozen)
searchable snapshot.

Eleven steps, Step 0 through Step 10, run in order. Each one exists because it is
the only thing that can falsify a specific claim in the migration plan. Write
every intermediate file to the session scratch directory; nothing here should land
in the repository.

The step numbers are the trail into the recorded run. Campaign 2's captured
artifacts in [`campaign-data.md`](campaign-data.md) are named `d0` through `d10`,
one prefix per step below: Step 6 is measured by `d6-es-pod.log` and the `d6-*`
acknowledgments, Step 9's residual audit is `d9-residual-orphans.tsv`, and so on.
The same numbering runs through
[`../skills/es-hybrid-migration/SKILL.md`](../skills/es-hybrid-migration/SKILL.md),
which is the operator-facing version of this section.

### Step 0: heal broken mounts first

The reachability sweeper's mounted-snapshot pre-flight refuses to operate when an
index is mounted on a snapshot the repository catalog does not contain. That is
the correct behavior, because an unresolvable mount means the tool cannot prove
what is still needed, but it means healing comes first, not later.

```bash
python3 snapshot_sizes.py --es http://localhost:9200 --repo oci-repro \
  --user "elastic:$PW" --emit-classified --out classified.tsv

# For every index whose mount snapshot reports MISSING-FROM-CATALOG:
$ES -XDELETE '/<mounted-index>'
```

**Acceptance:** re-running `--emit-classified` prints
`0 mounted snapshot(s) MISSING-FROM-CATALOG`. Anything else and you stop here: no
subsequent step in this campaign is meaningful while a mount is dangling.

### Step 1: size the new backup storage

This is where the split-repo shape shows up in the numbers. `--split-frozen`
separates snapshots into `slm` (regular backups), `frozen-pinned` (the snapshots
pinned by searchable-snapshot mounts) and `other` (manual or ILM-orphaned), and
sizes each class on its own terms.

```bash
python3 snapshot_sizes.py --es http://localhost:9200 --repo oci-repro \
  --user "elastic:$PW" --split-frozen --recommend

python3 snapshot_sizes.py --es http://localhost:9200 --repo oci-repro \
  --user "elastic:$PW" --emit-classified --out classified.tsv
```

**The sizing rule for the hybrid: use the `slm`-only terms, and exclude the frozen
footprint line.** The recommendation prints four terms:

```
  baseline (largest slm snapshot total)     : ...
  + retention growth (N x median daily)     : ...
  + upgrade-day headroom (1 x baseline)     : ...
  + frozen footprint (pinned mounts)        : ...   <- NOT yours to buy
  = recommended repository capacity         : ...
```

The first three size the new block/NFS volume, because that is what the `fs`
repository will hold. The fourth stays on the S3-compatible repository, where those
bytes already live and where they will remain. Buying NFS capacity for the frozen
footprint is buying the same terabytes twice. Add the operational margin to the
three-term sum, not to the printed total.

**Acceptance:** the classified inventory accounts for every snapshot in the
repository with a class, and the `frozen-pinned` rows carry a `tier` and a
`mounted_by` index. In the reference run: 3 snapshots, `slm=1`,
`frozen-pinned=1`, `other=1`, zero missing from catalog; the three `slm` terms
summed to ~1.1 MiB against a printed total of ~1.4 MiB, the ~281 KiB difference
being exactly the frozen footprint that does not move.

### Step 2: provision and register the `fs` repository

An `fs` repository requires the parent directory to be listed in `path.repo` on
every node, and `path.repo` is a static setting. This is the campaign's one
rolling restart. Plan it as such: it is the only disruptive step in the whole
migration, and it happens before any data moves.

```bash
# 1. Attach the block/NFS volume to every node and add its parent to path.repo.
#    Under ECK this is an edit to the Elasticsearch resource's nodeSets
#    (volume + config), which the operator applies as a rolling restart.

# 2. Register, WITH verification. No ?verify=false here:
$ES -XPUT '/_snapshot/backups-fs' -H 'Content-Type: application/json' -d '{
  "type":"fs","settings":{"location":"/mnt/es-repo/backups","compress":true}}'
```

**Acceptance:** registration returns `{"acknowledged":true}` **with verification
left on**. That is the point of the step. Verification writes and then deletes a
probe blob; on the S3-compatible repository that delete is what fails. On an `fs`
repository the delete is an unlink, so verification passes cleanly. A successful
verified registration is the first direct evidence that the fault does not exist
on this storage.

### Step 3: repoint SLM and prove retention works again

```bash
# Repoint every SLM policy's "repository" to the fs repo. Nothing else changes.
$ES -XPUT '/_slm/policy/rig-daily' -H 'Content-Type: application/json' -d '{
  "schedule":"0 30 3 * * ?","name":"<rig-daily-{now/d}>","repository":"backups-fs",
  "config":{"indices":["logs-*"]},"retention":{"expire_after":"7d","min_count":5}}'

$ES -XPOST '/_slm/policy/rig-daily/_execute'
```

Then prove retention actually reclaims bytes. An acknowledged delete is exactly
what the S3-compatible repository already returns while leaking, so acknowledgement
proves nothing. Count files on disk instead:

```bash
COUNT='find "$LOC" -type f | wc -l'   # $LOC = the fs repo location, on any node
kubectl $CTX -n es-rig exec rig-es-default-0 -- sh -c "$COUNT"   # before
$ES -XDELETE '/_snapshot/backups-fs/<one-backup-snapshot>'
kubectl $CTX -n es-rig exec rig-es-default-0 -- sh -c "$COUNT"   # after
```

**Acceptance:** the file count drops. Blobs genuinely unlink. In the reference
run it went from 26 files to 2: the repository emptied down to its root metadata
because the deleted snapshot was the only one holding those segments. A count that
stays flat means you are still on the faulty transport and the repoint did not take.

Leave one standing backup snapshot in place; Step 10 restores from it.

### Step 4: formalize the frozen repository's registration

The frozen tier stays where it is. Make its registration match the supported
configuration rather than leaving it in whatever state history left it.

```bash
$ES -XPUT '/_snapshot/oci-repro?verify=false' -H 'Content-Type: application/json' -d '{
  "type":"s3","settings":{"bucket":"es-snapshots","endpoint":"http://minio:9000",
  "path_style_access":true,"client":"default"}}'
```

The rig's `oci-repro` has no `base_path`, so the body above is its complete
settings block. **On any repository that does have one, a `PUT` without it
repoints the repository at the bucket root**, because `PUT` replaces the settings
rather than merging them. Read the existing settings first and carry every key
across, and accept on the snapshot listing being unchanged rather than on
`acknowledged` alone. See Step 4 of
[`../skills/es-hybrid-migration/SKILL.md`](../skills/es-hybrid-migration/SKILL.md).

**Acceptance:** `{"acknowledged":true}`, and `_cat/snapshots/oci-repro` lists the
same snapshots as before the PUT. `?verify=false` is Elastic support's
documented workaround for this fault: it skips the write-then-delete probe whose
delete leg fails, and nothing else. It does not suppress any error on the read
path, and it does not make deletes work; Step 6 measures exactly what it does not fix.
Re-registering an existing repository this way does not disturb its contents or its
mounts, which is what makes it safe to do with the frozen tier live.

### Step 5: prove the frozen tier still serves from the repository

Two claims are under test here, and they need different instruments. That the
frozen tier still answers queries is a smoke test. That the repository still holds
the blobs behind the mount is the gate.

```bash
# Smoke test: the mount answers, cold.
$ES -XPOST '/_searchable_snapshots/cache/clear'
$ES -XPOST '/<frozen-index>/_search?size=0' -H 'Content-Type: application/json' \
  -d '{"track_total_hits":true}'

# The gate.
$ES -XPOST '/_snapshot/oci-repro/_verify_integrity'
```

**Acceptance:** `total_anomalies: 0` and `result: "pass"`. The reference run's
cold search returned 3,500 hits with 0 failed shards, which is the claim that lets
the frozen tier stay put: reads never issue a `DeleteObjects`, so the fault is not
on the serving path.

**A search cannot be the acceptance criterion.** The obvious recipe, a cache clear
followed by a `track_total_hits` search accepted on `"failed": 0`, passes on a
mount whose blobs are gone. Measured on the rig against a mount with all 8 of its
data blobs deleted: HTTP 200, `total=200`, `"failed": 0`. Three repairs were tried
and all three failed:

- `&request_cache=false` changes nothing. The pass survives it, survives an
  explicit request-cache clear, and appears on the first query against a mount
  created after the blobs were already gone, so no cache entry could have existed.
  The count comes from the open Lucene reader.
- `size=1` catches total loss and misses partial loss. With one segment's blob
  deleted, `size=1`, sorts and aggregations all passed, and the aggregations
  returned correct values covering the destroyed segment. A `max` aggregation
  returned 1393.0, a document living in the segment whose only blob no longer
  existed.
- Closing and reopening the index detects nothing on either mount type. Both
  return to green with no error in the cluster log.

The `.snapshot-blob-cache` system index is part of the reason. It caches blob byte
ranges per repository and per snapshot, survives
`_searchable_snapshots/cache/clear`, and survives a remount under a new name, so
any recipe built on "clear the cache, then query" has to account for it.

A `full_copy` mount is worse. One that finished prewarming keeps serving from its
local copy with the repository destroyed, and no search-shaped check works there
at all. `full_copy` in 9.5.2 fetches lazily and depends on prewarm: a `full_copy`
mount created after the blobs were deleted reported green and 46.7kb in
`_cat/indices`, then returned 404 against S3 on the first document fetch.

`_verify_integrity` caught every case the search missed: total loss (8 anomalies),
partial loss (1 anomaly, named with its Lucene filename), and corruption by length
mismatch. Two limits carry with it. It is repository-scoped and slow on a large
repository, so it is a scheduled gate rather than a per-index probe. And by
default it compares blob names and lengths without downloading contents, so it
catches a missing or wrong-sized blob and not a blob whose bytes are wrong at the
right length.

### Step 6: measure the residual leak honestly

The hybrid does not fix deletes on the S3-compatible repository. Prove that, on the
record, rather than letting it be an assumption.

```bash
# Delete a couple of snapshots from the S3-compatible repo (scrap + an old one)
$ES -XDELETE '/_snapshot/oci-repro/<scrap-snapshot>'
$ES -XDELETE '/_snapshot/oci-repro/<old-snapshot>'

kubectl $CTX -n es-rig logs rig-es-default-0 > es-pod.log
grep -c "Failed to delete" es-pod.log
```

**Acceptance:** both deletes return `{"acknowledged":true}` and the log carries
failed-delete WARN lines. Reference run: 14 `Failed to delete` WARN lines from
two snapshot deletions, alongside the `no longer part of any snapshot ... but
failed to remove them` lines that name the condemned keys. That residue is the
sweeper's entire workload under the hybrid, and it is bounded: the
S3-compatible repository now sees occasional frozen-tier churn instead of a
daily retention cycle.

### Step 7: export the mounted set

Repository metadata cannot see which snapshots are pinned by searchable-snapshot
mounts; that fact lives in index settings on the cluster. Export it, so the
reachability audit can be pre-flighted against it.

```bash
python3 snapshot_sizes.py --es http://localhost:9200 --repo oci-repro \
  --user "elastic:$PW" --emit-mounted --out mounted.txt
grep -cv '^#' mounted.txt      # the count is the check, not the exit code
```

**Acceptance:** one line per mounted snapshot, first token the snapshot name, with
uuid, tier and mounting index alongside, and a row count matching the mounts the
cluster actually has. An empty file when frozen indices exist is a failure, not a
pass: it means the export did not see them, and feeding an empty pre-flight file
to the sweeper would silently prove nothing.

The export filters mounts on `repository_name` matching `--repo` exactly, is case
sensitive, and does not validate the name against the cluster, so a typo produces
`# 0 snapshot(s) ...` and exit 0. That is indistinguishable from a repository with
no mounts by exit code alone, which is why the acceptance is the row count and not
the exit status.

### Step 8: log-driven cleanup

```bash
# Dry-run first. Always.
python3 es_log_driven_sweeper.py es-pod.log --prefix / --out log-manifest.tsv

# Then apply, via the tool's own --execute path (see the caveats in §6.2):
python3 es_log_driven_sweeper.py es-pod.log --prefix / \
  --namespace <ns> --bucket <bucket> \
  --fetch-metadata --min-object-age 24 --execute
```

`--min-object-age` is the age half of the logs-plus-age policy: it refuses any
candidate whose `Last-Modified` is newer than the window, which cushions both
in-flight writes and operator error. It requires `--fetch-metadata`, because that
is where the object dates come from, and it fails closed: a key whose metadata
cannot be read is excluded too.

**Acceptance:** the dry-run summary accounts for every parsed line
(matched + skipped = read), reports its eligible-key total by blob class, and names
each guard that fired with the keys it withheld. Reference run: 338 lines parsed,
66 unique keys, 53 eligible rows, with the highest `index-N` in its directory
withheld by the live-generation guard and two directory-shaped names withheld as
unknown blob shapes. The apply pass reported `deleted: 53, already-gone: 0`.

The `already-gone` count matters as much as the deleted count: it is the tool
finding keys ES condemned that something else had already removed, and it must not
be an error.

### Step 9: reachability audit with the mounted pre-flight

Independent confirmation, by the other tool, that the log-driven pass left nothing
live behind and that the residue is the size the truncation accounting predicts.

```bash
python3 oci_repo_sweeper.py --local-repo repo-mirror --min-age-hours 0 \
  --mounted-snapshots mounted.txt --out residual-orphans.tsv
```

**Acceptance, in this order:**

1. The mounted pre-flight passes: every snapshot in `mounted.txt` is present
   in the repository catalog at the current root generation. A failed pre-flight
   deletes nothing and is a hard stop; go back to Step 0.
2. The residual set is small and entirely explained by WARN truncation.
   Reference run: 4 orphans, 3.5 KiB, against 53 keys recovered from the logs,
   about 93% key recall for that round.

Recall varies with the composition of the deletion batches, because ES renders at
most 10 keys from the last partition of each failed cycle. State the figure you
measure, and expect it to move: campaign 1 recovered 71.4% of keys (20 of 28
orphans), campaign 2 recovered 93.0% (53 of 57). A recall figure quoted without its
round is not a property of the tool.

Key recall flatters the result, so quote bytes too. In campaign 1 the same run
that recovered 71.4% of keys recovered only 56.7% of bytes (214,267 of
378,142), because one of the eight missed keys was the single largest orphan in
the repository. Four segment blobs were 83.5% of the wasted bytes at 14% of the
object count. That asymmetry is the whole argument for the reachability sweeper:
the cheap log-driven pass tends to leave the *big* orphans behind.

### Step 10: final proofs

Two proofs that gate, one smoke test that does not. Both gates must pass.

```bash
# Gate 1. The S3-compatible repository is internally consistent after the sweep, and
#         the frozen tier's blobs are all still there. One call covers both,
#         because a search against the mount does not (see Step 5).
$ES -XPOST '/_snapshot/oci-repro/_verify_integrity'

# Gate 2. The fs repository can actually restore a backup
$ES -XPOST '/_snapshot/backups-fs/<standing-backup>/_restore?wait_for_completion=true' \
  -H 'Content-Type: application/json' -d '{
    "indices":"logs-a","rename_pattern":"(.+)","rename_replacement":"restored-$1"}'
$ES '/_cat/count/logs-a?h=count'; $ES '/_cat/count/restored-logs-a?h=count'

# Smoke test. Records that the frozen tier still answers cold. Not a gate.
$ES -XPOST '/_searchable_snapshots/cache/clear'
$ES -XPOST '/<frozen-index>/_search?size=0' -H 'Content-Type: application/json' \
  -d '{"track_total_hits":true}'
```

**Acceptance:** `_verify_integrity` verifies every snapshot with no errors and
reports `total_anomalies: 0`, `result: "pass"` (reference run: 2/2 snapshots, 3/3
indices, 4/4 index-snapshots, 28 blobs, final repository generation 9); the
restore completes with `failed: 0` shards and exact doc-count equality against the
original index (3,500 / 3,500). Exact equality, not approximate. Anything less is
a failed campaign. The cold frozen search returned the full doc count with 0
failed shards (3,500 / 0), which is recorded and not gated on.

**Read the right fields.** On Elasticsearch 9.5.2 the `results` object of
`_verify_integrity` contains exactly `status`, `final_repository_generation`,
`total_anomalies` and `result`. It carries no `snapshot_restorability` and no
`restorable_snapshot_count`; per-index restorability entries live in the streamed
`log` array instead. An acceptance criterion written against
`results.snapshot_restorability` or `results.restorable_snapshot_count` matches
nothing and reads as a pass forever.

**Run gate 1 after every sweep, not only at the end of a campaign.** A snapshot
taken after a bad delete reports `SUCCESS` and cannot be restored: Elasticsearch
deduplicates shard files on physical name, length and checksum and never checks
that the blob is still in the store, so the next snapshot reuses a reference to a
blob that is gone and succeeds doing it. Nothing surfaces until a restore is
attempted. On a daily SLM schedule that is weeks of green, unrestorable backups.

### Acceptance criteria, condensed

| Step | Must be true |
|---|---|
| Step 0 | `--emit-classified` reports `0 mounted snapshot(s) MISSING-FROM-CATALOG` |
| Step 1 | every snapshot classified; new-volume sizing uses baseline + retention growth + upgrade headroom, frozen footprint excluded |
| Step 2 | `fs` repository registers `acknowledged:true` with verification on |
| Step 3 | on-disk file count drops sharply after a backup delete (observed after-count 2; the before-count of 26 was re-derived from an equivalent snapshot at write time, not captured pre-delete, see [`campaign-data.md`](campaign-data.md) §15) |
| Step 4 | S3-compatible repository re-registers `acknowledged:true` with `?verify=false`; mounts undisturbed |
| Step 5 | `_verify_integrity` on the S3-compatible repository: `total_anomalies: 0`, `result: pass`. A cache-clear-then-search check does not substitute; it passes on a destroyed mount |
| Step 6 | deletes still `acknowledged:true` and failed-delete WARNs appear (reference: 14) |
| Step 7 | one line per mounted snapshot; never empty while frozen indices exist |
| Step 8 | matched + skipped = lines read; every guard names what it withheld; apply reports deleted + already-gone |
| Step 9 | mounted pre-flight OK; residual explained by truncation accounting; recall figure recorded with its round |
| Step 10 | `_verify_integrity` clean (`total_anomalies: 0`, `result: pass`); restore doc counts equal exactly |

---

## 5. Layer 3: adversarial review

Layers 1 and 2 test what somebody thought of. Layer 3 attacks what they did not.

### 5.1 Process

Reviewers are independent and report-only, with no write access to the code during
review. Report-only matters: a reviewer who can fix things starts fixing instead of
finding, and stops looking after the first bug. The output of Layer 3 is a findings
list, nothing else.

Each reviewer gets one attack question, a single adversarial framing rather than
"review this code":

1. Reachability sweeper: *"Can it ever delete a blob ES still needs?"*
   Concurrency windows (in-flight snapshots, root generation mid-move), silent
   SMILE misdecode, format assumptions versus the ES source, prefix and pagination
   handling, age-guard timezone handling, whether non-orphans can reach the delete
   path, mounted searchable snapshots invisible to repository metadata, and test gaps.
2. Log-driven sweeper: *"Can it delete something live, or extract keys it was
   never told about?"* Parser anchoring and injection, guard soundness under
   stale-log replay and UUID-style shard generations, relative-key resolution,
   confirmation-gate bypass, state-file semantics, truncation accounting.
3. Migration plan and sizing formula: every API claim, behavior claim, and
   price claim checked against source and vendor docs. For the hybrid specifically:
   whether `?verify=false` really has no read-path consequence, whether an `fs`
   repository's `path.repo` requirement really implies a rolling restart, and
   whether the frozen footprint is genuinely excludable from new-volume sizing.

The ES source tree is ground truth. Reviewers get the Elasticsearch source
(pinned: `main` @ `c714edd`) alongside the scripts and the tests. A claim about
what ES writes is settled by reading `BlobStoreRepository`, `S3BlobStore`,
`S3BlobContainer`, or the x-pack searchable-snapshots code, never by reading this
repo's own docstrings, which are the thing under test.

Findings require evidence. A finding is admissible only with:

- quoted code, the actual lines from the tool and, where relevant, from ES;
- a concrete failure scenario, the sequence of events that produces the bad
  outcome, not "this could be a problem";
- a severity: what is lost, and whether it is recoverable.

Speculative findings are rejected. "This might race" without a scenario, "this
looks fragile" without a case, "consider adding" without a failure: all rejected,
explicitly, so the fix list stays trustworthy. A fix list where half the entries
are hunches is a fix list nobody triages.

### 5.2 Fix mandates

Each confirmed finding becomes a mandate: fix + regression test + suite green.

The regression test must fail against the pre-fix code. If it passes both before
and after, it is not testing the finding. Write it, run it on the old code, watch
it fail, then fix.

The kinds of defect this layer produced on this codebase show why the earlier
layers could not: SMILE shared-string-table fidelity against Jackson's exact rules
(a synthetic encoder that shares the same wrong assumption round-trips fine),
unsupported SMILE version bytes accepted instead of rejected, an in-flight snapshot
whose earliest uploads are older than the age guard while the root generation is
not yet committed, a missing object timestamp treated as old rather than fresh, a
state file reused across repositories causing cross-repo "already handled" skips,
and repository metadata being structurally blind to searchable-snapshot mounts,
the finding that produced the `--mounted-snapshots` pre-flight the hybrid campaign
now depends on. Every one of them is a correct-looking program with a wrong
assumption, invisible to a test suite built on the same assumption.

The loop closes only when `python3 -m unittest discover -s tests` and both
`--self-test` runs are green again, and, for anything touching classification or
blob parsing, the Layer 2 pass has been re-run.

---

## 6. Known limits

State these out loud. A methodology that oversells its coverage is worse than one
with gaps, because the gaps stop getting watched.

### 6.1 What the rig cannot reach

A real Object Storage endpoint is never exercised. No step in this playbook talks to
Oracle Cloud Infrastructure. The delete transport (`oci` SDK
`ObjectStorageClient.delete_object`) is validated only through local filesystem
deletes and unit-level 404-idempotent paths; MinIO stands in for the Amazon S3
Compatibility API's *rejection* behavior, which is the fault being reproduced, not the delete path
being used to fix it. What is proven: which keys get selected, and that nothing
selects a live key. What is not proven: provider-specific Object Storage API behavior such
as auth, throttling, pagination at scale, regional endpoint quirks, or error shapes
other than 404. A first production run should be `--emit orphans` to a manifest,
eyeballed, then `--execute` on a small slice.

WARN truncation makes log-driven coverage inherently partial. ES renders at
most 10 keys per failed delete cycle (`partition.stream().limit(10)`), and only
from the last partition. If the failed key count is an exact multiple of the batch
size, the rendered list is empty and the cycle contributes zero keys. The
log-driven sweeper is therefore a supplement, never a complete sweep, at default
log levels. Measured key recall was 71.4% (20 of 28 orphans, campaign 1)
and 93.0% (53 of 57, campaign 2), and in campaign 1 that 71.4% of keys was
only 56.7% of bytes, with the misses skewing toward the largest blobs. The
variation is not noise. It is a direct function of how the failed deletes happened
to be batched. Quote recall with the round it came from, never as a headline
property. Full coverage needs the TRACE-logging drain procedure described in the
script's module docstring, or the reachability sweeper.

A shard-metadata decode that returns a plausible wrong answer at the right file
entry count is not detected anywhere. The fixtures in `tests/fixtures/` pin the
decoder against bytes Elasticsearch 9.5.2 actually wrote, which is validation
against the version that was tested. The stated risk is upstream format drift,
which is exactly the case those results do not cover. Renaming one field produced
a parse that returned no file names without raising and deleted 96.4% of a rig
repository by bytes; that specific shape is now caught by the shard shape gate,
and the circuit breaker catches a shard that condemns all of its own segments.
Neither sees a file list that is well formed and wrong. The blob-count
reconciliation under `--cross-check` does see a count that moved, and it blocks,
but `--cross-check` is an opt-in flag and `--allow-blob-count-mismatch` turns the
block back into a warning. Outside the tool, the defence the runbooks named was
bucket versioning plus the dated delete record, carried as a hard prerequisite
rather than a recommendation. That defence is unavailable through the Amazon S3
Compatibility API, for the reason given in the correction under section 1.2: the
one operation that enumerates versions is not on Oracle's supported list, so the
id a recovery needs can never be discovered. On that endpoint the honest answer
is that a wrong delete has no undo.

Scale is untested. The rig is tens of objects. Pagination, `--workers`
concurrency under real latency, and memory behavior on a repository with millions
of blobs are covered by code review only.

Object versioning was exercised on MinIO, not on Object Storage. Manifest plus versioning
recovered a repository to `result=pass, total_anomalies=0` after two destructive
runs, twice, on the rig. MinIO also recovered an object written *before*
versioning was enabled, which is standard AWS S3 behaviour and contradicts
Oracle's documented "versioning must be enabled at the time of the object's
uploading". Nothing here settles which behaviour a real Object Storage bucket
has, so the runbooks tell operators to treat Object Storage as non-retroactive
and to prove otherwise on
a throwaway bucket before relying on it. None of that recovery reaches an
operator whose only credential is a Customer Secret Key. Every command in it runs
against the Object Storage API, and the Amazon S3 Compatibility API exposes no
part of it.

Time and clock behavior is only partly covered. The age guard and the
active-scope guard are unit-tested with synthetic mtimes. Clock skew between the
ES nodes, the object store, and the machine running the sweeper is not simulated.
Keep `--min-age-hours` well above any plausible skew; the default of 24 exists for
this reason, and `--unsafe-min-age` is lab-only.

### 6.2 What the hybrid campaign did not prove

The campaign in §4 stands up the recommended architecture and exercises every leg
of it, but four things about it were not validated on the rig. Each is stated
here in full so nobody reads §4 as broader coverage than it is.

1. The storage substrate under the `fs` repository was a stand-in. The rig's
`fs` repository lived on an ephemeral in-pod volume, not on real NFS or a real
block device. What that proves is the *Elasticsearch* half: registration passes
with verification on, SLM writes there, and retention deletes genuinely unlink
blobs. What it does not touch is the storage half: mount options (`hard`,
`intr`, `sync`, `noac`), server-side failover, stale file handles under a
failover, permission and ownership behavior across nodes, or capacity exhaustion.
Validate the volume itself separately, before the migration, with the storage
team's own acceptance tests.

2. The rig applied the cleanup manifest with a raw S3 client, which bypasses a
safety check. MinIO does not speak the Object Storage API, so the rig deleted the
manifest's keys with `mc rm` rather than through the sweeper's `--execute` path.
That bypass is significant: `--execute` re-reads `index.latest` and the live
`RepositoryData` at delete time and re-checks every candidate against the live
root generation, and a raw client does none of that. It was acceptable on the
rig because the in-manifest highest-generation guard had already withheld the top
`index-N`, and because the logs were minutes old, so there was no window for the
live generation to have moved. Neither condition is guaranteed in production.

> **Production rule: apply manifests through the tool's own `--execute` path
> against the Object Storage API, which enforces the delete-time live-generation
> cross-check. Never apply a manifest with a raw object-store client without
> first running the reachability pre-flight. If you do it anyway, treat the
> manifest as stale the moment anything writes to the repository.**

3. `--min-object-age` could not be exercised live. The age half of the
logs-plus-age policy needs a HEAD pass over candidate objects through the
Object Storage API to read `Last-Modified`, and MinIO is not that API. It is covered at
Layer 1 instead, against an injectable fake client: objects inside the window are
excluded and annotated `too-young`, objects whose metadata cannot be read are
excluded fail-closed as `age-unknown`, excluded keys stay in the manifest but drop
out of the `DELETE <N>` count, and the flag refuses without `--fetch-metadata`.
That is good coverage of the *logic* and no coverage of the *transport*. Treat the
first production run with `--min-object-age` as the transport's real first test:
dry-run it, read the `too-young` rows, and confirm the dates look like dates.

4. The pre-existing limits in §6.1 all carry over. The hybrid campaign does not
narrow the WARN-truncation gap, does not exercise the TRACE drain procedure, does
not touch a real Object Storage endpoint, and does not test scale. The frozen tier is now in
the rig, which retires the older "searchable snapshots are not in the rig" caveat,
since `--split-frozen`, `--emit-mounted` and the `--mounted-snapshots` pre-flight
are all exercised against real mounts. But it is one partial mount on a
tens-of-objects repository. Frozen tiers with many mounts sharing segment lineage,
where the measured frozen footprint double-counts, remain sized by a floor rather
than a figure.
