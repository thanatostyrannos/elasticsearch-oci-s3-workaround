# Where the generation-chain auditor breaks under size

Measured against the `generation_chain` package as it stood in
`wt-issue-43` at commit `b689f66`, 2026-08-25 19:49Z, frozen into
`tool-snapshot/` so the numbers stay reproducible while the worktree moves.
Two runs labelled `-live-worktree` were taken against the worktree later the
same day and are called out where they differ.

Every number below came out of a script in `harness/`. Nothing is estimated
from reading the source. Where a figure is arithmetic on a measured request
count it says so and names both inputs.

## Which data is real and which is synthetic

| Measurement | Data | Store |
|---|---|---|
| Generator validation | real 9.5.2 capture vs synthetic | local mirror |
| Generation depth | synthetic | local mirror |
| Depth at 40 ms | synthetic | local mirror, delay injected per round trip |
| Index and shard breadth | synthetic | local mirror |
| Memory | synthetic | local mirror |
| Listing on S3 | synthetic objects | **real MinIO** `RELEASE.2025-01-18T00-31-37Z` |
| Whole audit on S3 | synthetic objects | **real MinIO**, same pin |
| Listing on OCI | synthetic | local stub, no OCI endpoint exists here |
| Signing cost | n/a | in process |
| Failure behaviour | synthetic | local mirror, faults injected |
| HTTP faults through the retry policy | synthetic | local stub |

## The generator was checked against the real repository first

`tests/fixtures/real-es952-repo.tar.gz` is a captured repository: 50 objects,
three root generations, three shard directories. The tool reads it with 13
object GETs and 10 existence checks, and explains all of it.

A synthetic repository built to the same shape costs the tool exactly the same
13 reads, and both match `1 + generations + shard_directories × generations`.

| repository | objects | generations | shard dirs | GETs | predicted | explained |
|---|---|---|---|---|---|---|
| real 9.5.2 capture | 50 | 3 | 3 | 13 | 13 | 100% |
| synthetic, same shape | 49 | 3 | 3 | 13 | 13 | 100% |

`harness/validate_generator.py`, `harness/realcheck.py`.

## Generation chain depth

The tool reads `index.latest`, then every `index-<N>` in the bucket, then for
every shard directory it reads that shard's document at every generation. The
cost per generation is flat and the total is a straight line.

Synthetic, one index, one shard, local mirror. `harness/bench_depth.py`.

| generations | objects | round trips | trips per generation | seconds (local disk) | µs per trip | max concurrent | condemned | explained |
|---|---|---|---|---|---|---|---|---|
| 10 | 52 | 43 | 4.30 | 0.005 | 114 | 1 | 20 | 100% |
| 100 | 457 | 448 | 4.48 | 0.044 | 98 | 1 | 245 | 100% |
| 1 000 | 4 507 | 4 502 | 4.50 | 0.464 | 103 | 1 | 2 495 | 100% |
| 5 000 | 22 507 | 22 520 | 4.50 | 2.441 | 108 | 1 | 12 495 | 100% |
| 10 000 | 45 007 | 45 043 | 4.50 | 5.045 | 112 | 1 | 24 995 | 100% |

**Linear, with a constant of 4.5 round trips per generation** on this shape:
one root generation GET, one shard document GET, and about 2.5 existence
checks. Nothing degrades: coverage stays at 100% and µs-per-trip is flat
across a thousandfold range.

**It serialises. There is no parallelism anywhere.** The concurrency counter
never exceeded 1 in any run in this harness, including the runs against real
MinIO. That was also measured directly by injecting a delay per round trip:

| generations | round trips | delay injected per trip | measured seconds | seconds if perfectly serial |
|---|---|---|---|---|
| 100 | 448 | 40 ms | 18.36 | 17.92 |
| 200 | 898 | 40 ms | 37.30 | 35.92 |

Wall clock is the round trip count times the round trip, plus about 2.5%.
Depth is therefore entirely a latency problem, and the fix nobody has applied
is concurrency.

### What depth costs at a real round trip

The reviewer's figure for the RETIRED containment code was 38 seconds for 894
generations at 40 ms. The same depth through this tool, measured with a 40 ms
delay injected per round trip:

| tool | generations | round trips | seconds |
|---|---|---|---|
| retired containment code (reviewer's number, different tool) | 894 | not stated | 38 |
| this one, one index, one shard | 894 | 4 025 | 163.5 |

One index and one shard is the cheapest repository that exists. Every extra
shard directory adds one more GET per generation.

## Index and shard breadth

Generations held at 20, the repository widened. `harness/bench_breadth.py`,
synthetic, local mirror.

| indices x shards | shard dirs | objects | round trips | GETs per shard dir per generation | seconds | max concurrent | explained |
|---|---|---|---|---|---|---|---|
| 1 x 1 | 1 | 97 | 88 | 2.05 | 0.009 | 1 | 100% |
| 2 x 5 | 10 | 575 | 520 | 1.11 | 0.06 | 1 | 100% |
| 5 x 10 | 50 | 2 698 | 2 442 | 1.02 | 0.275 | 1 | 100% |
| 10 x 20 | 200 | 10 653 | 9 650 | 1.005 | 1.119 | 1 | 100% |
| 20 x 50 | 1 000 | 53 063 | 48 093 | 1.001 | 5.86 | 1 | 100% |
| 50 x 50 | 2 500 | 132 593 | 120 172 | 1.000 | 15.17 | 1 | 100% |

**Exactly one shard document GET per shard directory per generation, serial.**
Reads are neither batched nor overlapped, and the shard document cache only
saves a repeat of the same key.

The whole cost model, confirmed on both the real capture and every synthetic
size:

```
GETs   = 1 + generations + shard_directories x generations
HEADs  ~ 0.6 x total round trips  (one per candidate key, memoised)
```

## The same audit over a real endpoint

Synthetic repositories, put into the MinIO of the rig (the local test lab
reproducing the fault, see [FACTS.md](../../../FACTS.md#the-test-lab-henceforth-the-rig)) at
`RELEASE.2025-01-18T00-31-37Z`, read back through the tool's own S3
compatibility transport over a `kubectl port-forward`.
`harness/bench_endtoend_s3.py`.

| objects | shard dirs | generations | HTTP requests | GETs | HEADs | seconds | ms per request | max concurrent | explained |
|---|---|---|---|---|---|---|---|---|---|
| 575 | 10 | 20 | 520 | 222 | 298 | 2.0 | 3.77 | 1 | 100% |
| 2 698 | 50 | 20 | 2 442 | 1 024 | 1 418 | 9.1 | 3.73 | 1 | 100% |
| 10 653 | 200 | 20 | 9 650 | 4 032 | 5 618 | 36.6 | 3.80 | 1 | 100% |
| 53 063 | 1 000 | 20 | 48 093 | 20 075 | 28 018 | 193.3 | 4.02 | 1 | 100% |

**58% of the traffic is HEAD requests.** The tool confirms every candidate key
through the store before naming it, and confirms the witness blob of every
shard document the same way. That is a deliberate safety decision and it
nearly doubles the round trip count.

Three and a bit minutes on a LAN. At a round trip of 40 ms rather than 4, the
same 48 093 requests take **32 minutes**, and that is arithmetic on a measured
request count with a measured serial execution.

**163.5 seconds against 38, on the smallest possible repository.** The
comparison is a scale indicator rather than a like-for-like, since the retired
code did a different job, but the shape is the same and this tool is 4.3 times
the cost at the same depth.

## Bucket listing at page depth

Every state this project has tested before was one page. Both endpoint
transports were driven past a hundred pages, and the result was checked by
identity: the set of keys returned has to equal the set of keys that were put
there. `harness/bench_listing.py`.

S3, against the rig's real MinIO:

| objects | pages | keys per page | seconds | ms per page | key set exact | max concurrent |
|---|---|---|---|---|---|---|
| 575 | 1 | 575 | 0.019 | 19.0 | yes | 1 |
| 2 698 | 3 | 899 | 0.077 | 25.7 | yes | 1 |
| 10 653 | 11 | 969 | 0.294 | 26.7 | yes | 1 |
| 53 063 | 54 | 983 | 1.316 | 24.4 | yes | 1 |
| 132 593 | 133 | 997 | 3.158 | 23.7 | yes | 1 |

OCI native, against a local stub, because no OCI endpoint exists here:

| objects | pages | seconds | ms per page | key set exact | max concurrent |
|---|---|---|---|---|---|
| 1 000 | 1 | 0.025 | 25.0 | yes | 1 |
| 10 000 | 10 | 0.264 | 26.4 | yes | 1 |
| 50 000 | 50 | 1.667 | 33.3 | yes | 1 |
| 132 000 | 132 | 6.110 | 46.3 | yes | 1 |

**Continuation is correct at depth on both.** No key was lost or duplicated
across 133 pages of real MinIO responses or 132 pages of stub responses. The
S3 transport also decodes keys only when the response echoes
`EncodingType: url`, and MinIO does echo it, so the round trip through
percent encoding is exercised at every size above.

Listing is 1 page per 1 000 objects and about 25 ms per page, so it is a
rounding error next to the per-object reads: 133 pages is 3 seconds out of a
run that spends minutes. Its importance is not its cost. It is that **every
one of those pages is a round trip that can end the run**, which the failure
section below turns into a number.

The paging ceiling in both transports is 100 000 pages, which is 100 million
objects, so it is not a practical limit.

## Memory

The tool holds the whole listing, indexes it twice, and builds one `Placement`
record per key with a sentence of explanation attached. Nothing streams.
`harness/bench_memory.py`, synthetic, local mirror.

| objects | shard dirs | generations | listing alone | audit RSS growth | traced peak | process peak RSS | bytes per object |
|---|---|---|---|---|---|---|---|
| 10 653 | 200 | 20 | 0.0 MB | 20.9 MB | 13.5 MB | 51.5 MB | 2 059 |
| 53 063 | 1 000 | 20 | 0.6 MB | 103.5 MB | 65.9 MB | 161.5 MB | 2 045 |
| 132 593 | 2 500 | 20 | 6.7 MB | 234.8 MB | 159.2 MB | 357.0 MB | 1 857 |
| 292 644 | 2 500 | 45 | 18.9 MB | 541.4 MB | 370.1 MB | 783.8 MB | 1 940 |
| 585 194 | 5 000 | 45 | 42.5 MB | 1 040.8 MB | 739.7 MB | 1 548.1 MB | 1 865 |

**About 1.9 KB of resident memory per object in the bucket, dead linear.**
The listing itself is cheap, 70 bytes or so per key. The weight is the
classification: one record per key, each carrying a disposition string and a
prose reason.

On an ordinary jump host:

| bucket | peak RSS |
|---|---|
| 100 000 objects | ~270 MB |
| 500 000 objects | ~1.3 GB |
| 1 000 000 objects | ~2.6 GB |
| 2 000 000 objects | ~5.3 GB |

A 2 GB container or a 4 GB VM running other things dies somewhere between
750 000 and 1 500 000 objects, and it dies at the END of the run, after
spending the hours the round trips cost. The two lines above 500 000 objects
are the measured 585 194-object figure scaled linearly, which the four points
below it justify to within 10%.

## The OCI native transport signs every request with RSA, in python

`harness/bench_signing.py`, in process, 200 and 4 000 samples.

| transport | signature cost per request | CPU at 50 000 requests |
|---|---|---|
| OCI native, RSA-2048, pure python | 16.03 ms | 13.4 minutes |
| S3 compatibility, sigv4 HMAC | 0.008 ms | 0.4 seconds |

**2 000 times the cost, and it is on the critical path of a serial loop.** A
48 093-request audit over the OCI native transport spends 12.8 minutes of jump
host CPU on signing before counting a single millisecond of network. At a
40 ms round trip that is 32 minutes of network plus 13 minutes of CPU, and
because the loop is serial they add rather than overlap.

The S3 compatibility transport does not have this problem at all.

## Failure, which is the finding that matters

### What is fatal and what is local

Rather than infer this, every distinct read one run makes was failed on its own,
in its own run, and the outcome recorded. 232 runs on a 20-generation, 4-shard
repository. `harness/bench_critical.py`.

| the read that failed | trials | refused the whole run | keys lost per trial | coverage said so | lost keys silently | manifest grew |
|---|---|---|---|---|---|---|
| the bucket listing | 1 | **1** | 0 | 0 | 0 | 0 |
| `index.latest` | 1 | **1** | 0 | 0 | 0 | 0 |
| a root generation blob | 20 | **1** (the anchor) | 11.9 | 19 | 0 | 0 |
| a shard generation document | 80 | 0 | 1.8 | 40 | 0 | 0 |
| a snapshot document HEAD | 45 | 0 | 1.0 | 0 | **45** | 0 |
| a segment blob HEAD | 76 | 0 | 0.95 | 0 | **72** | 0 |
| an index metadata HEAD | 9 | 0 | 1.0 | 0 | **9** | 0 |

Three answers fall out of this table.

**A single failed read does NOT refuse the run, unless it is one of three.**
The listing, `index.latest`, and the current root generation. Everything else
is local: it removes what that read would have contributed and the run
finishes. The safety rule is narrower than "refuses when it cannot establish
what is live", and that is the right design.

**The manifest never grew.** Not once in 232 single-failure runs, nor in any
of the 200-plus multi-failure runs below. Adding a failure only ever shortened
the list, which is the monotonicity the package claims, and it now has a
measurement behind it rather than an argument.

**126 of the 130 HEAD failures lost keys with nothing said about it.** A
failed existence check is caught inside `KeyIndex._still_there`, recorded as
"the store does not hold this", and the key leaves the manifest. No note, no
dropped shard, no movement in the coverage percentage. That is the honesty gap,
and the next section prices it.

### How often a run refuses, and what drives it

Refusal does not scale with how much work the run does. It scales with the
number of FATAL round trips, and the structural map says that is
`listing_pages + 2`. Measured by turning the page size down instead of the
object count up, 400 trials per cell. `harness/bench_refusal.py`.

| listing pages | equivalent bucket | failure rate | trials | refused | measured | predicted from pages+2 |
|---|---|---|---|---|---|---|
| 1 | 1 000 objects | 1 in 1 000 | 400 | 3 | 0.75% | 0.30% |
| 1 | 1 000 objects | 1 in 100 | 400 | 14 | 3.50% | 2.97% |
| 10 | 10 000 objects | 1 in 1 000 | 400 | 4 | 1.00% | 1.19% |
| 10 | 10 000 objects | 1 in 100 | 400 | 42 | 10.50% | 11.36% |
| 52 | 52 000 objects | 1 in 1 000 | 400 | 16 | 4.00% | 5.26% |
| 52 | 52 000 objects | 1 in 100 | 400 | 155 | 38.75% | 41.88% |
| 129 | 129 000 objects | 1 in 1 000 | 400 | 43 | 10.75% | 12.28% |
| 129 | 129 000 objects | 1 in 100 | 400 | 277 | 69.25% | 73.20% |
| 257 | 257 000 objects | 1 in 1 000 | 400 | 83 | 20.75% | 22.83% |
| 257 | 257 000 objects | 1 in 100 | 400 | 368 | 92.00% | 92.60% |

Every refusal was flagged transient, so the tool exits 4 and says a retry is
reasonable. **Every retry costs the whole run again.**

Passing `--elasticsearch` adds three more fatal round trips, and unlike the
store reads they are not retried at all: one attempt each, through
`urllib.request.urlopen` directly, and any of the three failing refuses the
run before an object is read. Measured in `harness/bench_corroboration.py`:
3 calls, 1 attempt each, 3 of 3 fatal.

### The headline: N = 52 068 objects, M = 1 000 generations

One repository, 52 068 objects, 1 000 root generations, 20 shard directories,
53 listing pages, **52 012 store round trips per run**, 30 938 keys condemned
when nothing fails. Failures injected per round trip, 30 trials per rate.
`harness/bench_failures.py`.

| failure rate | expected failures per run | runs that completed | manifest recovered, mean | manifest recovered, worst | coverage the report claimed | keys it added that a clean run did not |
|---|---|---|---|---|---|---|
| none | 0 | 100% | 100% | 100% | 100% | 0 |
| 1 in 10 000 | 5.2 | **100%** (30/30) | 99.98% | 99.79% | 99.83% | 0 |
| **1 in 1 000** | **52.0** | **100%** (30/30) | **99.52%** | **96.63%** | 94.89% | 0 |
| 1 in 100 | 520.1 | 63.3% (19/30) | 95.78% | 90.88% | 68.96% | 0 |

**At 1 in 1 000 on 52 068 objects and 1 000 generations, every run completed
and a completing run explained 99.52% of what a clean run explains, worst case
96.63%.** About 52 reads failed per run and the run absorbed all of them.

The tool does not do the bad thing. It does not refuse the run over one bad
read, so the operator does not learn to bypass the guard. Coverage degrades
locally and the report says so, in the conservative direction: at 1 in 1 000
it claims 94.89% when it actually recovered 99.52%, and at 1 in 100 it claims
68.96% when it recovered 95.78%. **The headline number understates what the
run achieved, which is the right direction to be wrong in.**

Refusal at 1 in 100 is the pressure point. Two thirds of runs complete, one
third refuse, and each refusal costs the whole run. The expected number of
attempts to get one completed run is 1.6, and at a 40 ms round trip a run of
this shape is 35 minutes, so a 1% error rate turns a 35 minute job into a
55 minute one on average. At 129 000 objects and 1 in 100 the refusal rate is
69%, the expected attempts is 3.3, and the job stops being something anyone
runs in a maintenance window.

### The part the report does not tell the operator

The same repository, the same rates, but only the existence checks fail.
`harness/bench_failures.py --fail-ops exists`, 20 trials per rate.

| failure rate | runs completed | manifest recovered, mean | manifest recovered, worst | coverage the report claimed | keys silently gone (of 30 938) |
|---|---|---|---|---|---|
| 1 in 10 000 | 100% | 99.99% | 99.98% | **100.0%** | ~3 |
| 1 in 1 000 | 100% | 99.90% | 99.86% | **100.0%** | ~31 |
| 1 in 100 | 100% | 99.00% | 98.87% | **100.0%** | ~309 |

**The coverage report says 100% explained in every one of these runs.** Not
99.9, not "a shard was dropped", not a note. 100%, with up to 309 keys the run
would have named and did not.

This is not the dangerous direction. A key that silently leaves the manifest
is a key that does not get deleted, so it is a leak rather than data loss, and
the whole package is built to fail that way. But it is the one place where the
report is not honest about what the run could not see, and the failure that
causes it, a HEAD that returns 503 or times out, is the single most common
thing that goes wrong on a large bucket.

The cause is one exception handler. `KeyIndex._still_there` catches every
exception from `source.exists` and records `False`, which is
indistinguishable from "the store says this object is gone". A store that
could not answer and a store that answered no are the same value.

The narrower isolation runs make the split unambiguous, on a smaller
repository at 12 trials per cell:

| what failed | rate | manifest recovered | coverage claimed | completing runs that lost keys with no signal |
|---|---|---|---|---|
| HEAD only | 1 in 1 000 | 99.88% | 100.0% | 50% |
| HEAD only | 1 in 100 | 99.10% | 100.0% | **100%** |
| GET only | 1 in 1 000 | 99.95% | 99.32% | 0% |
| GET only | 1 in 100 | 96.91% | 91.64% | 0% |

GET failures are reported. HEAD failures are not.

### Real HTTP faults, through the tool's own retry policy

Everything above injects a failure the retry policy never sees. This drives a
local stub that returns real statuses at a rate per ATTEMPT, so the tool's own
eight-attempt backoff runs. Backoff is recorded rather than slept, and
reported separately. 712 logical requests per run, 6 trials per cell.
`harness/bench_http_faults.py`.

| status | rate per attempt | HTTP attempts | amplification | runs completed | manifest recovered | coverage claimed | backoff it asked for |
|---|---|---|---|---|---|---|---|
| 503 | 1 in 1 000 | 712 | 1.00 | 100% | **100%** | 100% | 0.2 s |
| 503 | 1 in 100 | 719 | 1.01 | 100% | **100%** | 100% | 3.5 s |
| 503 | 1 in 20 | 752 | 1.06 | 100% | **100%** | 100% | 21.2 s |
| 403 | 1 in 1 000 | 710 | 1.00 | 100% | 99.38% | 99.44% | 0 |
| 403 | 1 in 100 | 702 | 0.99 | 100% | 96.55% | 97.71% | 0 |
| 403 | 1 in 20 | 676 | 0.95 | 100% | 85.10% | 82.45% | 0 |

**A 503 costs coverage nothing and time everything.** Eight attempts with
jittered exponential backoff make the terminal failure probability of a
retryable status negligible: not one key was lost at any rate up to 5%, and no
run refused. What it costs is wall clock. 21.2 seconds of backoff on 712
requests is 30 ms per logical request; scaled to the 48 093-request audit
measured against MinIO, a 5% throttling rate adds **24 minutes of sleeping**
and a 1% rate adds 4 minutes. That is arithmetic on a measured backoff figure.

**A 403 is not retried and lands immediately.** It is treated as an answer
rather than as weather, which is right for a wrong credential and wrong for a
per-object policy, an expired token mid-run, or a bucket policy that denies
some prefix. At 1 in 100 it costs 3.5% of the manifest.

So the honest answer to "what does a realistic error rate do" depends entirely
on which status the store sends. The retryable ones are absorbed and turn into
minutes. The others go straight through into lost coverage.

## Where the tool stops being usable, and why

The three ceilings are independent and they bite in this order.

**Wall clock, from about 2 000 generations or about 20 000 objects, on a
remote endpoint.** Measured: 4.5 round trips per generation, one GET per shard
directory per generation, serial, 40 ms a trip on a real remote store. 894
generations of the narrowest possible repository is 163.5 seconds measured. A
realistic 53 000-object repository is 48 093 round trips, which is 32 minutes
at 40 ms, plus 13 minutes of RSA on the OCI native path, plus whatever the
store throttles. Nothing in the tool overlaps a single request with another.

**Refusal probability, from about 50 000 objects at a 1% error rate.**
Measured: fatal round trips are `listing_pages + 2`, plus 3 unretried cluster
calls with `--elasticsearch`. At 129 000 objects and a 1% terminal error rate,
69% of runs refuse. The refusals are honest and retryable, but a retry is the
whole 30-plus minutes again. Note that this only bites for statuses the retry
policy does not absorb, since 503s at 5% cost nothing.

**Memory, from about 750 000 objects on a 2 GB host.** Measured: 1.9 KB
resident per object, linear, no streaming, and the peak arrives at the end of
the run.

None of these is a correctness cliff. Coverage stays at 100% at every size
measured when nothing fails, the listing is exact at 133 pages, and the
manifest never grew under any injected failure. The tool degrades by getting
slower and by explaining less, which is the correct way for this tool to fail.

## What would change these numbers

Ranked by the measurement that motivates each, not by how hard it is.

1. **Record a failed existence check in coverage.** One exception handler in
   `KeyIndex._still_there` conflates "the store says no" with "the store did
   not answer". It costs up to 309 keys out of 30 938 at a 1 in 100 rate while
   the report claims 100%. This is the only place measured here where the
   report is wrong rather than conservative.
2. **Overlap the reads.** 100% of the wall clock is serial round trips, proven
   by a concurrency counter that never left 1 and by a 40 ms injection that
   reproduced the total to within 2.5%. Eight-way concurrency turns the
   32 minute 53 000-object audit into four.
3. **Retry the fatal three like everything else, or say they are fatal.** The
   listing, `index.latest` and the anchor are `listing_pages + 2` chances to
   lose an hour of work. The listing already retries per page inside
   `HttpReader`; what it does not do is resume a listing that died on page 90,
   so one bad page throws away 89 good ones.
4. **Cache the RSA signature material, or use a native crypto path on OCI.**
   16 ms per request, 2 000 times the S3 path, 13 minutes of CPU on a
   50 000-request run.
5. **Do not build a Placement for every key in the bucket at once.** 1.9 KB
   per object is the whole memory story, and the classification is written out
   in one pass anyway.

## Caveats, stated where they are

* Every size number is synthetic data. The generator was validated against the
  real 9.5.2 capture on request count and on the cost model, and it agrees
  exactly, but it does not reproduce real blob size distributions, real
  `.partN` splitting, or a repository where some deletes succeeded and the
  chain has holes.
* The OCI native numbers are against a stub written for this harness. No OCI
  endpoint exists in this environment. The S3 numbers at 133 pages are against
  a real MinIO at the pinned release.
* The 40 ms round trip is injected, not observed against a remote store. The
  4 ms figure is real, measured against MinIO through a `kubectl port-forward`.
* Wall clock on local disk is a floor, not a prediction. What travels is the
  round trip count, which is exact.
* The worktree moved during this work. `bench_depth.py` and
  `bench_critical.py` were re-run against the live tree and are in
  `results/*-live-worktree.json`. The fatal set is unchanged there: the
  listing, `index.latest` and the anchor, and nothing else. The new
  `snap-<uuid>.dat` extent check does not refuse the run, and it is reported
  in coverage when it drops an index, but it drops a whole index at a time:
  72 keys of a 126-key manifest per failed read on a two-index repository. The
  silent HEAD channel is unchanged in the live tree, 126 of 130 checks.
* The harness left about 1.6 GB in the rig's MinIO under the bucket
  `scale-limits`, five prefixes named `r00500` to `r132000`, plus a
  `throughput-probe/` prefix from calibrating the uploader. It is kept so the
  listing and end-to-end benches re-run without a four minute upload. Drop the
  bucket when it is no longer wanted. Nothing outside that bucket was touched
  and MinIO was not upgraded.
* No file in `elasticsearch-oci-s3-workaround` or in any `wt-issue-*` worktree
  was written. The package is imported with `sys.dont_write_bytecode` set, so
  not even a `__pycache__` was left behind, and this was checked after every
  run against the live tree.
