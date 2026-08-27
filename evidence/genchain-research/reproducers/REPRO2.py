"""Counterexamples to "every failure mode produces a SMALLER output list".

Run:  python3 REPRO2.py [path-to-worktree]
Reads the package out of the worktree, writes only under this directory.
Exit 1 while any counterexample still stands.
"""
import json
import os
import shutil
import sys

WT = sys.argv[1] if len(sys.argv) > 1 else "/home/thanatostyrannos/projects/wt-issue-43"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WT)
sys.path.insert(0, os.path.join(WT, "tests"))
from generation_chain import run_audit                       # noqa: E402
from generation_chain.sources.local import LocalMirrorSource  # noqa: E402
import genchain_fixtures as fx                                # noqa: E402

OUT = os.path.join(HERE, "repro2")


def build(name, history, **kw):
    root = os.path.join(OUT, name)
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    fx.build_repository(root, history, **kw)
    return root


class WrongObject:
    """A store that SUCCEEDS and answers one key with another object's bytes.

    A key normalisation bug, a caching proxy, or an eventually consistent
    store. Not a store that fails: a store that lies while answering 200.
    """

    def __init__(self, root, swap):
        self.inner = LocalMirrorSource(root)
        self.swap = swap

    def describe(self):
        return "a store that answers with the wrong object"

    def list_keys(self):
        return self.inner.list_keys()

    def fetch(self, key):
        return self.inner.fetch(self.swap.get(key, key))

    def exists(self, key):
        return self.inner.exists(key)


def show(title, before, after, live=()):
    grew = sorted(set(after) - set(before))
    verdict = "GREW" if grew else "did not grow"
    print(f"\n=== {title}")
    print(f"    before={len(before)}  after={len(after)}  {verdict}")
    for key in grew:
        print(f"    + {key}" + ("   <-- LIVE DATA" if key in live else ""))
    return bool(grew)


def keys(source):
    return set(run_audit(source).keys)


failures = []

# A. Two shards of one index. The current shard document carries neither its
#    own shard number nor its own generation, and the corroboration in
#    _live_blobs compares only the set of snapshot NAMES, which is identical
#    across the shards of one index. Shard 1's document passes as shard 0's,
#    the live set for shard 0 becomes shard 1's blobs, and the segment both
#    snapshots share in shard 0 is condemned.
HA = [
    {"s1": {"idx": {0: ["__a0", "__sh0"], 1: ["__a1", "__sh1"]}}},
    {"s1": {"idx": {0: ["__a0", "__sh0"], 1: ["__a1", "__sh1"]}},
     "s2": {"idx": {0: ["__b0", "__sh0"], 1: ["__b1", "__sh1"]}}},
    {"s2": {"idx": {0: ["__b0", "__sh0"], 1: ["__b1", "__sh1"]}}},
]
root = build("a-two-shards", HA)
before = keys(LocalMirrorSource(root))
after = keys(WrongObject(root, {
    "indices/iuuid-idx/0/index-sg-idx-0-2": "indices/iuuid-idx/1/index-sg-idx-1-2"}))
if show("A  the store serves shard 1's current document for shard 0",
        before, after, {"indices/iuuid-idx/0/__sh0"}):
    failures.append("A")

# B. The same hole across INDICES, which is the common shape: one snapshot
#    covers many indices, so every index in the repository has the same set of
#    snapshot names and the corroboration cannot separate them. One shard each
#    is enough.
HB = [
    {"s1": {"one": ["__a1", "__sh1"], "two": ["__a2", "__sh2"]}},
    {"s1": {"one": ["__a1", "__sh1"], "two": ["__a2", "__sh2"]},
     "s2": {"one": ["__b1", "__sh1"], "two": ["__b2", "__sh2"]}},
    {"s2": {"one": ["__b1", "__sh1"], "two": ["__b2", "__sh2"]}},
]
root = build("b-two-indices", HB)
before = keys(LocalMirrorSource(root))
after = keys(WrongObject(root, {
    "indices/iuuid-one/0/index-sg-one-0-2": "indices/iuuid-two/0/index-sg-two-0-2"}))
if show("B  the store serves index 'two' current document for index 'one'",
        before, after, {"indices/iuuid-one/0/__sh1"}):
    failures.append("B")

# C. No transport involved. A current generation blob that is well formed JSON
#    and omits one index from its `indices` map. _live_blobs reads a missing
#    index as positive evidence of an empty live set, so every blob the
#    deleted snapshot named in that index is condemned, including the one the
#    surviving snapshot still uses. The cross-check that would catch this runs
#    off `index_metadata_lookup`, which repositories written before 7.12 do
#    not carry at all, and this package supports those repositories by name.
HC = [
    {"s1": {"idx": ["__a", "__shared"], "other": ["__o", "__osh"]}},
    {"s1": {"idx": ["__a", "__shared"], "other": ["__o", "__osh"]},
     "s2": {"idx": ["__b", "__shared"], "other": ["__p", "__osh"]}},
    {"s2": {"idx": ["__b", "__shared"], "other": ["__p", "__osh"]}},
]
root = build("c-pre712", HC, index_metadata=False)
before = keys(LocalMirrorSource(root))
document = json.load(open(os.path.join(root, "index-2")))
for snapshot in document["snapshots"]:
    snapshot.pop("index_metadata_lookup", None)
document["indices"].pop("other")
json.dump(document, open(os.path.join(root, "index-2"), "w"))
after = keys(LocalMirrorSource(root))
if show("C  a pre-7.12 shaped catalog whose current generation omits an index",
        before, after, {"indices/iuuid-other/0/__osh"}):
    failures.append("C")

print("\n" + "=" * 60)
print("counterexamples reproduced:", failures or "none")
sys.exit(1 if failures else 0)
