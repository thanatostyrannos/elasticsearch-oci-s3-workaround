"""How often a run refuses, as a function of listing depth and failure rate.

Refusal does not depend on how big the repository is in the way the run cost
does. It depends on how many round trips are FATAL, and the structural map in
bench_critical.py says there are exactly three kinds: the listing, index.latest
and the current root generation blob. A listing is one round trip per page, so
the fatal count is (pages + 2) and it grows with the bucket.

To measure that at depth without paying for a huge repository on every trial,
the page size is turned down instead of the object count turned up. Thirty
objects at three per page is fifty pages, and fifty pages is fifty chances to
lose the run, which is the thing being measured.
"""
from __future__ import annotations

import json, math, os, shutil, sys, tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import base_parser, emit, table, use_tool

parser = base_parser(__doc__)
parser.add_argument("--generations", type=int, default=20)
parser.add_argument("--indices", type=int, default=2)
parser.add_argument("--shards", type=int, default=2)
parser.add_argument("--pages", default="1,10,53,133,500",
                    help="listing page counts to emulate")
parser.add_argument("--rates", default="0.001,0.01")
parser.add_argument("--trials", type=int, default=400)
parser.add_argument("--out", default="refusal")
args = parser.parse_args()
use_tool(args.tool_root)

import synthrepo
from instrument import InstrumentedSource
from generation_chain.sources.local import LocalMirrorSource
from generation_chain.derivation.audit import run_audit

root = tempfile.mkdtemp(prefix="genchain-refusal-")
rows = []
try:
    writer = synthrepo.build(generations=args.generations, indices=args.indices,
                             shards=args.shards, blobs_per_shard_per_snapshot=2,
                             live_window=3, root=root)
    objects = len(writer.objects)
    mirror = LocalMirrorSource(root)
    for pages in [int(x) for x in args.pages.split(",")]:
        page_size = max(1, math.ceil(objects / pages))
        for rate in [float(x) for x in args.rates.split(",")]:
            refused = transient = 0
            for seed in range(args.trials):
                source = InstrumentedSource(mirror, fail_rate=rate, seed=seed,
                                            page_size=page_size)
                result = run_audit(source)
                if result.coverage.refused:
                    refused += 1
                    if result.coverage.refusal_is_transient:
                        transient += 1
            actual_pages = math.ceil(objects / page_size)
            predicted = 1 - (1 - rate) ** (actual_pages + 2)
            rows.append({
                "emulated_pages": actual_pages,
                "objects_that_implies": actual_pages * 1000,
                "fail_rate": rate,
                "trials": args.trials,
                "refused": refused,
                "refused_pct": round(100 * refused / args.trials, 2),
                "predicted_pct_from_pages_plus_2": round(100 * predicted, 2),
                "refusals_flagged_transient": transient,
            })
            print(json.dumps(rows[-1]), flush=True)
finally:
    shutil.rmtree(root, ignore_errors=True)

print()
print(table(rows, ["emulated_pages", "objects_that_implies", "fail_rate",
                   "trials", "refused", "refused_pct",
                   "predicted_pct_from_pages_plus_2",
                   "refusals_flagged_transient"]))
print(emit(args.out, rows))
