"""Counterexamples against the shard-identity subset check (A/B fix).

The check: for a CURRENT shard document read for indices/X/N/index-<gen>,
every `__` name in its files[] must be an object under indices/X/N/ in the
listing. It is a SUBSET test, and a subset test cannot separate two documents
when the smaller side is empty or is contained in the other.

Every repository below is a modern 7.12+ catalog: `index_metadata_identifiers`
and `index_metadata_lookup` are present throughout. None of this depends on
the pre-7.12 shape.

Run:  python3 REPRO3.py [path-to-worktree]      exit 1 while any stands.
"""
import json
import os
import shutil
import sys

WT = sys.argv[1] if len(sys.argv) > 1 else "/home/thanatostyrannos/projects/wt-issue-43"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WT)
sys.path.insert(0, os.path.join(WT, "tests"))
from generation_chain import run_audit                        # noqa: E402
from generation_chain.sources.local import LocalMirrorSource   # noqa: E402
import genchain_fixtures as fx                                 # noqa: E402

OUT = os.path.join(HERE, "repro3")


def build(name, history, **kw):
    root = os.path.join(OUT, name)
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    fx.build_repository(root, history, **kw)
    return root


class Store:
    """A store that succeeds and answers one key with bytes of my choosing."""

    def __init__(self, root, swap_bytes, extra=()):
        self.inner = LocalMirrorSource(root)
        self.swap_bytes = dict(swap_bytes)
        self.extra = list(extra)

    def describe(self):
        return "a store that answers one key with the wrong object"

    def list_keys(self):
        return self.inner.list_keys() + self.extra

    def fetch(self, key):
        if key in self.swap_bytes:
            return self.swap_bytes[key]
        return self.inner.fetch(key)

    def exists(self, key):
        return key in self.extra or self.inner.exists(key)


def shard_document(snapshots):
    """A well-formed BlobStoreIndexShardSnapshots with the given file lists."""
    names = sorted({f for files in snapshots.values() for f in files})
    document = {
        "files": [{"name": n, "physical_name": "_" + n.lstrip("v_"),
                   "length": 42, "checksum": "a", "written_by": "9.11.1"}
                  for n in names],
        "snapshots": {name: {"files": list(files), "shard_state_id": "st"}
                      for name, files in snapshots.items()},
    }
    return fx.codec_wrap(json.dumps(document).encode())


def keys(source):
    return set(run_audit(source).keys)


def show(title, before, after, live=()):
    grew = sorted(set(after) - set(before))
    lost = sorted(set(before) - set(after))
    named_live = [k for k in grew if k in live]
    print(f"\n=== {title}")
    print(f"    before={len(before)}  after={len(after)}  "
          f"{'GREW' if grew else 'did not grow'}")
    for key in grew:
        print(f"    + {key}" + ("   <-- LIVE DATA" if key in live else ""))
    if lost and grew:
        print(f"    (traded for: {', '.join(lost)})")
    return bool(named_live)


ONE_SHARD = [
    {"s1": {"one": ["__a1", "__sh1"]}},
    {"s1": {"one": ["__a1", "__sh1"]}, "s2": {"one": ["__b1", "__sh1"]}},
    {"s2": {"one": ["__b1", "__sh1"]}},
]
TWO_INDICES = [
    {"s1": {"one": ["__a1", "__sh1"], "two": ["__a2", "__sh2"]}},
    {"s1": {"one": ["__a1", "__sh1"], "two": ["__a2", "__sh2"]},
     "s2": {"one": ["__b1", "__sh1"], "two": ["__b2", "__sh2"]}},
    {"s2": {"one": ["__b1", "__sh1"], "two": ["__b2", "__sh2"]}},
]
OVERLAP = [
    {"s1": {"one": {0: ["__x", "__sh"], 1: ["__x"]}}},
    {"s1": {"one": {0: ["__x", "__sh"], 1: ["__x"]}},
     "s2": {"one": {0: ["__z", "__sh"], 1: ["__x"]}}},
    {"s2": {"one": {0: ["__z", "__sh"], 1: ["__x"]}}},
]

VICTIM_ONE = "indices/iuuid-one/0/index-sg-one-0-2"
failures = []

# D1. The document names no files at all. It parses, its snapshot name set
#     matches the catalog, and its blob set is empty, so the subset test is
#     satisfied against every directory in the store. The live set for the
#     shard becomes empty and the shared segment is condemned.
#     shard_snapshots.py's own docstring says "a document that yields no blob
#     names must raise, never return an empty list".
root = build("d1-empty", ONE_SHARD)
before = keys(LocalMirrorSource(root))
after = keys(Store(root, {VICTIM_ONE: shard_document({"s2": []})}))
if show("D1 the current document parses and names nothing",
        before, after, {"indices/iuuid-one/0/__sh1"}):
    failures.append("D1")

# D2. The document names only INLINE `v__` entries, which have no object
#     behind them. Same empty blob set, but this is a document Elasticsearch
#     really writes: a shard whose whole Lucene commit fits inline, which is
#     what a snapshot of an empty index produces.
root = build("d2-inline", TWO_INDICES)
before = keys(LocalMirrorSource(root))
after = keys(Store(root, {
    VICTIM_ONE: shard_document({"s2": ["v__segments3", "v__si3"]})}))
if show("D2 the current document names only inline v__ entries",
        before, after, {"indices/iuuid-one/0/__sh1"}):
    failures.append("D2")

# D3. A real document from another shard of the same index, whose blob set
#     happens to be contained in the victim directory. The subset test passes,
#     the manifest keeps its SIZE, and a live segment quietly replaces a dead
#     one. A check that compares only counts would call this clean.
root = build("d3-overlap", OVERLAP)
before = keys(LocalMirrorSource(root))
donor = open(os.path.join(root, "indices/iuuid-one/1/index-sg-one-1-2"),
             "rb").read()
after = keys(Store(root, {"indices/iuuid-one/0/index-sg-one-0-2": donor}))
if show("D3 another shard's document whose blobs are a subset of this directory",
        before, after, {"indices/iuuid-one/0/__sh"}):
    failures.append("D3")

# D4. The identity check's evidence comes from the listing and is never
#     confirmed against the store, while KeyIndex confirms every key before
#     naming it. The same listing is distrusted where it could add a key and
#     trusted where it protects live data. A listing that over-reports the
#     victim directory lets a real foreign document through unchanged.
root = build("d4-overreport", TWO_INDICES)
before = keys(LocalMirrorSource(root))
donor = open(os.path.join(root, "indices/iuuid-two/0/index-sg-two-0-2"),
             "rb").read()
after = keys(Store(root, {VICTIM_ONE: donor},
                   extra=["indices/iuuid-one/0/__b2",
                          "indices/iuuid-one/0/__sh2"]))
if show("D4 a listing that over-reports the victim directory admits the swap",
        before, after, {"indices/iuuid-one/0/__sh1"}):
    failures.append("D4")

print("\n" + "=" * 60)
print("counterexamples reproduced:", failures or "none")
sys.exit(1 if failures else 0)
