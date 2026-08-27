"""Real HTTP faults, through the tool's own retry policy.

Everything in bench_failures.py injects a failure the retry policy never sees,
which answers "what does the derivation do with a failed read". This answers
the question in front of it: what does a 503 at some rate per attempt actually
become by the time the derivation sees it, and what does that cost in time.

The two statuses are not the same event. 429 and 5xx are retried up to eight
times, so a store that throttles some fraction of calls mostly disappears
behind the policy and reappears as wall clock. A 403 or a 404 is an answer
rather than weather, so it is not retried at all and lands on the derivation
immediately. A tenancy that expires a token mid-run, or an object with a
policy on it, is the second kind.

Backoff is recorded rather than slept, so the run finishes; the seconds it
WOULD have slept are reported as their own number, because on a long chain
they dominate everything else.
"""
from __future__ import annotations

import json, os, statistics, sys, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import base_parser, emit, table, use_tool

parser = base_parser(__doc__)
parser.add_argument("--generations", type=int, default=100)
parser.add_argument("--indices", type=int, default=2)
parser.add_argument("--shards", type=int, default=2)
parser.add_argument("--rates", default="0,0.001,0.01,0.05")
parser.add_argument("--statuses", default="503,403")
parser.add_argument("--trials", type=int, default=8)
parser.add_argument("--out", default="http-faults")
args = parser.parse_args()
use_tool(args.tool_root)

import synthrepo, ocistub
from generation_chain.sources.oci import OciNativeSource
from generation_chain.sources.http_reads import HttpReader
from generation_chain.derivation.audit import run_audit


class SleeplessReader(HttpReader):
    """Records the backoff it was asked to take, and takes none of it."""

    def __init__(self):
        self.slept = 0.0
        super().__init__(sleep=self._record, jitter=lambda: 0.5)

    def _record(self, seconds):
        self.slept += seconds


writer = synthrepo.build(generations=args.generations, indices=args.indices,
                         shards=args.shards, blobs_per_shard_per_snapshot=2,
                         live_window=3)
objects = writer.objects
credentials = ocistub.credentials()

with ocistub.OciStub(objects) as clean_stub:
    source = OciNativeSource(endpoint=clean_stub.endpoint, namespace="ns",
                             bucket="b", credentials=credentials,
                             reader=SleeplessReader())
    clean = run_audit(source)
    baseline = set(c.key for c in clean.condemned)
    logical = clean_stub.requests
print(f"{len(objects)} objects, {args.generations} generations, "
      f"{logical} HTTP requests when nothing fails, {len(baseline)} condemned")

rows = []
for status in [int(x) for x in args.statuses.split(",")]:
    for rate in [float(x) for x in args.rates.split(",")]:
        trials = 1 if rate == 0 else args.trials
        refused = 0
        recovered, reported, attempts, backoff = [], [], [], []
        for seed in range(trials):
            reader = SleeplessReader()
            with ocistub.OciStub(objects, fail_rate=rate, fail_status=status,
                                 seed=seed) as stub:
                source = OciNativeSource(endpoint=stub.endpoint, namespace="ns",
                                         bucket="b", credentials=credentials,
                                         reader=reader)
                result = run_audit(source)
                attempts.append(stub.requests)
            backoff.append(reader.slept)
            if result.coverage.refused:
                refused += 1
                continue
            keys = set(c.key for c in result.condemned)
            recovered.append(len(keys & baseline) / max(len(baseline), 1))
            reported.append(result.coverage.explained_fraction or 1.0)
        rows.append({
            "status": status, "fail_rate_per_attempt": rate, "trials": trials,
            "logical_requests": logical,
            "http_attempts_mean": round(statistics.fmean(attempts)),
            "retry_amplification": round(statistics.fmean(attempts) / logical, 3),
            "runs_completed_pct": round(100 * (trials - refused) / trials, 1),
            "runs_refused": refused,
            "manifest_recovered_mean_pct":
                round(100 * statistics.fmean(recovered), 2) if recovered else None,
            "coverage_reported_mean_pct":
                round(100 * statistics.fmean(reported), 2) if reported else None,
            "backoff_seconds_mean": round(statistics.fmean(backoff), 1),
        })
        print(json.dumps(rows[-1]), flush=True)

print()
print(table(rows, ["status", "fail_rate_per_attempt", "logical_requests",
                   "http_attempts_mean", "retry_amplification",
                   "runs_completed_pct", "manifest_recovered_mean_pct",
                   "coverage_reported_mean_pct", "backoff_seconds_mean"]))
print(emit(args.out, rows))
