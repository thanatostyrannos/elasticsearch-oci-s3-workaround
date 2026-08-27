"""What a read failure does to a run, and how much coverage it costs.

This is the question that is not about performance. The tool's safety rule is
that it refuses when it cannot establish what is still live. On a repository
of a few dozen objects a read failure is rare. On one that costs tens of
thousands of round trips, at least one of them failing is close to certain on
every run, so the behaviour under failure IS the behaviour.

Three things get measured, separately, because they are three different
answers:

  * does the run refuse, or does it complete with less in it
  * how much of the manifest a completing run loses
  * whether the coverage report tells the operator that it lost anything

The third is the one that decides whether the guard is worth having. A run
that quietly returns 60% of the orphans and prints "100% explained" is worse
than a run that refuses, because an operator acts on it.
"""
from __future__ import annotations

import argparse, collections, json, os, shutil, statistics, sys, tempfile, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import base_parser, emit, table, use_tool

parser = base_parser(__doc__)
parser.add_argument("--generations", type=int, default=200)
parser.add_argument("--indices", type=int, default=5)
parser.add_argument("--shards", type=int, default=4)
parser.add_argument("--blobs", type=int, default=2)
parser.add_argument("--live-window", type=int, default=3)
parser.add_argument("--rates", default="0,0.0001,0.001,0.01,0.05")
parser.add_argument("--trials", type=int, default=40)
parser.add_argument("--fail-ops", default="list,fetch,exists",
                    help="which call kinds may fail")
parser.add_argument("--out", default="failures")
args = parser.parse_args()
use_tool(args.tool_root)

import synthrepo
from instrument import InstrumentedSource
from generation_chain.sources.local import LocalMirrorSource
from generation_chain.derivation.audit import run_audit

fail_ops = tuple(x for x in args.fail_ops.split(",") if x)
root = tempfile.mkdtemp(prefix="genchain-failures-")
rows = []
try:
    writer = synthrepo.build(generations=args.generations, indices=args.indices,
                             shards=args.shards,
                             blobs_per_shard_per_snapshot=args.blobs,
                             live_window=args.live_window, root=root)
    objects = len(writer.objects)
    mirror = LocalMirrorSource(root)

    clean = run_audit(InstrumentedSource(mirror))
    baseline = set(c.key for c in clean.condemned)
    probe = InstrumentedSource(mirror)
    run_audit(probe)
    requests = probe.counters.requests
    print(f"repository: {objects} objects, {args.generations} generations, "
          f"{args.indices * args.shards} shard directories, "
          f"{requests} store round trips per run, "
          f"{len(baseline)} keys condemned when nothing fails")

    for rate in [float(x) for x in args.rates.split(",")]:
        trials = 1 if rate == 0 else args.trials
        refused = 0
        recovered = []
        reported = []
        dropped_shards = []
        rejected_gens = []
        silent = 0
        extra = 0
        for seed in range(trials):
            source = InstrumentedSource(mirror, fail_rate=rate, seed=seed,
                                        fail_ops=fail_ops)
            result = run_audit(source)
            coverage = result.coverage
            if coverage.refused:
                refused += 1
                continue
            keys = set(c.key for c in result.condemned)
            extra += len(keys - baseline)
            fraction = len(keys & baseline) / max(len(baseline), 1)
            recovered.append(fraction)
            reported.append(coverage.explained_fraction
                            if coverage.explained_fraction is not None else 1.0)
            dropped_shards.append(len(coverage.shards_dropped))
            rejected_gens.append(len(coverage.generations_rejected))
            # A run that lost keys and reported full coverage said nothing an
            # operator could act on.
            if fraction < 0.999 and (coverage.explained_fraction or 1.0) >= 0.999 \
                    and not coverage.shards_dropped:
                silent += 1
        row = {
            "fail_rate": rate,
            "trials": trials,
            "requests_per_run": requests,
            "expected_failures_per_run": round(requests * rate, 2),
            "runs_completed_pct": round(100 * (trials - refused) / trials, 1),
            "runs_refused": refused,
            "manifest_recovered_mean_pct":
                round(100 * statistics.fmean(recovered), 2) if recovered else None,
            "manifest_recovered_min_pct":
                round(100 * min(recovered), 2) if recovered else None,
            "coverage_reported_mean_pct":
                round(100 * statistics.fmean(reported), 2) if reported else None,
            "keys_not_in_baseline": extra,
            "shards_dropped_mean": round(statistics.fmean(dropped_shards), 2) if dropped_shards else None,
            "generations_rejected_mean": round(statistics.fmean(rejected_gens), 2) if rejected_gens else None,
            "silent_loss_runs": silent,
            "silent_loss_pct_of_completing":
                round(100 * silent / max(len(recovered), 1), 1),
        }
        rows.append(row)
        print(json.dumps(row), flush=True)
finally:
    shutil.rmtree(root, ignore_errors=True)

print()
print(table(rows, ["fail_rate", "expected_failures_per_run",
                   "runs_completed_pct", "manifest_recovered_mean_pct",
                   "manifest_recovered_min_pct", "coverage_reported_mean_pct",
                   "silent_loss_pct_of_completing", "keys_not_in_baseline"]))
print(emit(args.out, rows))
