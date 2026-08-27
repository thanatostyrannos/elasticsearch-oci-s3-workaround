# Scale and failure harness for the generation-chain auditor

Every number in the report next to this file came out of one of these scripts.
Re-run them to reproduce it, or point them at a newer build of the tool.

## What it measures

| Script | Question |
|---|---|
| `harness/validate_generator.py` | does the synthetic repository cost the tool what a real one does |
| `harness/realcheck.py` | the tool against the captured 9.5.2 repository |
| `harness/bench_depth.py` | generation chain depth: requests, time, memory |
| `harness/bench_breadth.py` | index and shard breadth at fixed depth |
| `harness/bench_memory.py` | resident memory against object count |
| `harness/bench_listing.py` | bucket listing past a hundred pages, S3 and OCI |
| `harness/bench_endtoend_s3.py` | a whole audit over a real MinIO |
| `harness/bench_signing.py` | per-request signing cost on each transport |
| `harness/bench_critical.py` | which single read refuses the run, and which only shortens it |
| `harness/bench_refusal.py` | refusal probability against listing depth and failure rate |
| `harness/bench_failures.py` | coverage lost per failure rate, and whether the report says so |
| `harness/bench_http_faults.py` | real 503s and 403s through the tool's own retry policy |

Results land in `results/*.json`. Every script takes `--tool-root` and size
and rate flags, so a later version can be pushed harder or less hard.

## Running it

```bash
./harness/run_all.sh
```

Against a different build:

```bash
GENCHAIN_TOOL_ROOT=/path/to/worktree ./harness/run_all.sh
```

The MinIO parts need a port forward and credentials for the rig, the local test
lab reproducing the fault (see [FACTS.md](https://github.com/thanatostyrannos/elasticsearch-oci-s3-workaround/blob/main/FACTS.md#the-test-lab-henceforth-the-rig)):

```bash
kubectl -n es-rig port-forward svc/minio 19000:9000 &
export MINIO_ENDPOINT=http://127.0.0.1:19000
export MINIO_ACCESS=$(kubectl -n es-rig get secret s3-credentials \
    -o jsonpath='{.data.s3\.client\.default\.access_key}' | base64 -d)
export MINIO_SECRET=$(kubectl -n es-rig get secret s3-credentials \
    -o jsonpath='{.data.s3\.client\.default\.secret_key}' | base64 -d)
```

MinIO is pinned at `RELEASE.2025-01-18T00-31-37Z` and must stay there. The
harness only reads, lists and puts objects under its own bucket.

`GENCHAIN_SNAPSHOT_DOCUMENTS=1` makes the generator write real
`snap-<uuid>.dat` documents, which later versions of the tool read and check
an extent against. Without it the generator writes the placeholder the
snapshot build was happy with.

## What was measured against what

`tool-snapshot/` holds a frozen copy of the `generation_chain` package taken
from `wt-issue-43` at commit `b689f66`, 2026-08-25 19:49Z, and every headline
number was measured against that copy. The worktree is under active
development and has moved since; `SNAPSHOT.md5` records the copy's digest.
The two runs labelled `-live-worktree` were taken against the worktree as it
stood later the same day.

Nothing in this harness writes to any repository checkout. It imports the
package with `sys.dont_write_bytecode` set, so it does not even leave a
`__pycache__` behind.

## How to read the numbers

The benchmarks answer "what does this cost" and not "is this correct". A green
run here says the tool completed and how expensive it was; it says nothing about
whether the manifest was right. Correctness lives in the unit suite and in the
adversarial reproducers, not here.

Two figures drive everything else. **Requests scale as shard directories times
generations**, because the tool reads one shard document per directory per
generation, which is why breadth and depth are measured separately.
**Resident memory scales with the parsed documents held**, not with the object
count, which is easy to get backwards.

### The memory model, and its correction

`bench_memory.py` produced `RESIDENT_BYTES_PER_OBJECT = 1900`, which
`sources/budget.py` uses to decide whether a repository fits before reading it.

A later measurement on a live repository, at two different sizes, gives a
different shape:

```
 94,600 objects -> 404,904 KB peak
209,420 objects -> 526,516 KB peak

marginal cost   1.06 KB per object
fixed baseline  298 MB
```

The marginal cost is BELOW the model, so the ceiling is conservative and refuses
runs that would in fact fit. That is the safe direction. But the 298 MB fixed
baseline is not modelled at all: `MemoryBudget` computes `objects *
bytes_per_object` with no constant term, so on a host or container under roughly
512 MB the ceiling can admit a run the baseline alone would kill.

A single measurement cannot tell these apart. Dividing one peak by one object
count gave 4.28 KB per object and pointed the opposite way. Two sizes give a
slope, which is the only thing that separates a constant from a rate.

## Prerequisites

Python 3 standard library only, the same as the tool. The offline benchmarks
need nothing else. The MinIO benchmarks need the port forward and credentials
above, and a bucket the harness may write to.
