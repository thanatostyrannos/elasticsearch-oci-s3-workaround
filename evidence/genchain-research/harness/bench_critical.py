"""Which single read refuses the whole run, and which only shortens it.

Rates and averages say what usually happens. This says what the structure is:
every distinct read one run makes gets failed on its own, in its own run, and
the outcome is recorded. The result is the exact set of reads that are fatal,
which is what turns a measured failure rate into a probability of refusal
without guessing.
"""
from __future__ import annotations

import collections, json, os, shutil, sys, tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import base_parser, emit, table, use_tool

parser = base_parser(__doc__)
parser.add_argument("--generations", type=int, default=20)
parser.add_argument("--indices", type=int, default=2)
parser.add_argument("--shards", type=int, default=2)
parser.add_argument("--out", default="critical")
args = parser.parse_args()
use_tool(args.tool_root)

import synthrepo
from instrument import InstrumentedSource
from generation_chain.errors import SourceReadError
from generation_chain.sources.local import LocalMirrorSource
from generation_chain.derivation.audit import run_audit


class FailOne:
    """A source that fails exactly one named read, and nothing else."""

    def __init__(self, inner, key=None, fail_list=False, op="fetch"):
        self._inner = inner
        self.key = key
        self.op = op
        self.fail_list = fail_list

    def describe(self):
        return self._inner.describe()

    def list_keys(self):
        if self.fail_list:
            raise SourceReadError("injected: the listing failed")
        return self._inner.list_keys()

    def fetch(self, key):
        if self.op == "fetch" and key == self.key:
            raise SourceReadError(f"injected: cannot read {key}")
        return self._inner.fetch(key)

    def exists(self, key):
        if self.op == "exists" and key == self.key:
            raise SourceReadError(f"injected: cannot confirm {key}")
        return self._inner.exists(key)


def kind_of(key: str) -> str:
    if key == "index.latest":
        return "index.latest"
    if key.startswith("index-"):
        return "root generation blob"
    if "/index-" in key:
        return "shard generation document"
    if "/snap-" in key or key.startswith("snap-"):
        return "snapshot document"
    if "meta-" in key:
        return "metadata blob"
    return "segment blob"


root = tempfile.mkdtemp(prefix="genchain-critical-")
rows = []
try:
    synthrepo.build(generations=args.generations, indices=args.indices,
                    shards=args.shards, blobs_per_shard_per_snapshot=2,
                    live_window=3, root=root)
    mirror = LocalMirrorSource(root)

    class Recorder(InstrumentedSource):
        def __init__(self, inner):
            super().__init__(inner)
            self.fetched, self.checked = [], []

        def fetch(self, key):
            self.fetched.append(key)
            return super().fetch(key)

        def exists(self, key):
            self.checked.append(key)
            return super().exists(key)

    recorder = Recorder(mirror)
    clean = run_audit(recorder)
    baseline = set(c.key for c in clean.condemned)
    fetch_keys = list(dict.fromkeys(recorder.fetched))
    exists_keys = list(dict.fromkeys(recorder.checked))
    print(f"clean run: {len(fetch_keys)} distinct reads, "
          f"{len(exists_keys)} distinct existence checks, "
          f"{len(baseline)} keys condemned")

    trials = [("listing", None, "list")]
    trials += [(kind_of(k), k, "fetch") for k in fetch_keys]
    trials += [(kind_of(k) + " (HEAD)", k, "exists") for k in exists_keys]

    summary = collections.defaultdict(
        lambda: {"trials": 0, "refused": 0, "lost_keys": 0,
                 "coverage_moved": 0, "silent": 0, "grew": 0})
    for label, key, op in trials:
        source = FailOne(mirror, key=key, fail_list=(op == "list"), op=op)
        result = run_audit(source)
        bucket = summary[label]
        bucket["trials"] += 1
        if result.coverage.refused:
            bucket["refused"] += 1
            continue
        keys = set(c.key for c in result.condemned)
        lost = len(baseline - keys)
        bucket["lost_keys"] += lost
        if len(keys - baseline):
            bucket["grew"] += 1
        moved = ((result.coverage.explained_fraction or 1.0) < 0.999
                 or result.coverage.shards_dropped
                 or result.coverage.generations_rejected
                 or any("could not be read" in n for n in result.coverage.notes))
        if moved:
            bucket["coverage_moved"] += 1
        elif lost:
            bucket["silent"] += 1

    for label, bucket in sorted(summary.items()):
        rows.append({
            "read_that_failed": label,
            "trials": bucket["trials"],
            "refused_the_run": bucket["refused"],
            "keys_lost_total": bucket["lost_keys"],
            "keys_lost_per_trial": round(bucket["lost_keys"] / bucket["trials"], 2),
            "coverage_said_so": bucket["coverage_moved"],
            "lost_keys_silently": bucket["silent"],
            "manifest_grew": bucket["grew"],
        })
finally:
    shutil.rmtree(root, ignore_errors=True)

print()
print(table(rows, ["read_that_failed", "trials", "refused_the_run",
                   "keys_lost_per_trial", "coverage_said_so",
                   "lost_keys_silently", "manifest_grew"]))
print(emit(args.out, rows))
