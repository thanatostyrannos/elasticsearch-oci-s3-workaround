"""Reproducers for the growth counterexamples found against generation_chain.

Run:  python3 REPRO.py [path-to-worktree]
Reads the package from the worktree; writes only under this directory.
"""
import sys, os, json, shutil

WT = sys.argv[1] if len(sys.argv) > 1 else "/home/thanatostyrannos/projects/wt-issue-43"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WT)
sys.path.insert(0, os.path.join(WT, "tests"))
from generation_chain import run_audit
from generation_chain.sources.local import LocalMirrorSource
import genchain_fixtures as fx

OUT = os.path.join(HERE, "repro")


def build(name, history):
    root = os.path.join(OUT, name)
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    fx.build_repository(root, history)
    return root


def keys(root_or_source):
    src = root_or_source if not isinstance(root_or_source, str) \
        else LocalMirrorSource(root_or_source)
    return set(run_audit(src).keys)


def show(title, before, after, live_keys=()):
    grew = sorted(after - before)
    print(f"\n=== {title}")
    print(f"    before={len(before)}  after={len(after)}  "
          f"{'GREW' if grew else 'did not grow'}")
    for k in grew:
        tag = "  <-- LIVE DATA" if k in live_keys else ""
        print(f"    + {k}{tag}")
    return bool(grew)


HISTORY = [
    {"s1": {"idx": ["__a", "__shared"]}},
    {"s1": {"idx": ["__a", "__shared"]}, "s2": {"idx": ["__b", "__shared"]}},
    {"s2": {"idx": ["__b", "__shared"]}},
]

H2 = [
    {"s1": {"idx": ["__a", "__shared"], "other": ["__o"]}},
    {"s1": {"idx": ["__a", "__shared"], "other": ["__o"]},
     "s2": {"idx": ["__b", "__shared"], "other": ["__o"]}},
    {"s2": {"idx": ["__b", "__shared"], "other": ["__o"]}},
]

H5 = [
    {"s1": {"idx": ["__x", "__y"]}},
    {"s1": {"idx": ["__x", "__y"]}},
    {"s3": {"idx": ["__x"]}},
]

H4 = [
    {"s1": {"idx": ["__a", "__shared"]}, "s2": {"idx": ["__b", "__shared"]}},
    {"s2": {"idx": ["__b", "__shared"]}},
    {"s2": {"idx": ["__b", "__shared"]}},
    {"s2": {"idx": ["__b", "__shared"]}},
]

failures = []

# 1. One key removed from the live snapshot's index_metadata_lookup in the
#    CURRENT generation. The parser reads a short lookup as a complete one, so
#    the live metadata set comes out short and a blob the live snapshot needs
#    is named.
base = keys(build("m-base", H2))
root = build("m-short-lookup", H2)
with open(os.path.join(root, "index-2")) as fh:
    doc = json.load(fh)
doc["snapshots"][0]["index_metadata_lookup"].pop("iuuid-other")
with open(os.path.join(root, "index-2"), "w") as fh:
    json.dump(doc, fh)
if show("1  current generation: live snapshot's index_metadata_lookup is short",
        base, keys(root), {"indices/iuuid-other/meta-md-other.dat"}):
    failures.append(1)

# 1b. Same hole reached through a value of the wrong TYPE, which the parser
#     drops silently rather than refusing.
base = keys(build("m-base1", HISTORY))
root = build("m-typed-lookup", HISTORY)
with open(os.path.join(root, "index-2")) as fh:
    doc = json.load(fh)
doc["snapshots"][0]["index_metadata_lookup"] = {"iuuid-idx": 12345}
with open(os.path.join(root, "index-2"), "w") as fh:
    json.dump(doc, fh)
if show("1b current generation: one lookup VALUE is a number, silently dropped",
        base, keys(root), {"indices/iuuid-idx/meta-md-idx.dat"}):
    failures.append("1b")


class WrongObject:
    """A transport that succeeds and hands back the wrong object's bytes."""

    def __init__(self, root, swap):
        self.inner = LocalMirrorSource(root)
        self.swap = swap

    def describe(self):
        return "wrong-object transport"

    def list_keys(self):
        return self.inner.list_keys()

    def fetch(self, key):
        return self.inner.fetch(self.swap.get(key, key))


# 2. A transport that answers one key with another object's bytes. Nothing in
#    the derivation checks that the document it read is the document it asked
#    for, so the current shard document can be replaced by a foreign one and
#    the live set collapses.
root = build("t-base", H5)
base = keys(root)
foreign = build("t-foreign", [{"z1": {"idx": ["__q"]}}])
shutil.copy(os.path.join(foreign, "indices/iuuid-idx/0/index-sg-idx-0-0"),
            os.path.join(root, "foreign-doc"))
after = keys(WrongObject(
    root, {"indices/iuuid-idx/0/index-sg-idx-0-2": "foreign-doc"}))
if show("2  transport answers the CURRENT shard document with another object",
        base, after, {"indices/iuuid-idx/0/__x"}):
    failures.append(2)

# 3. Adding a failure to a repository that already has one GROWS the manifest.
#    An unreadable shard document drops the whole shard; making the ROOT
#    generation blob of the same era unreadable too takes that shard document
#    out of the wanted set, so the shard comes back and its keys reappear.
root = build("n-one", H4)
fx.corrupt(root, "indices/iuuid-idx/0/index-sg-idx-0-2")
one_fault = keys(root)
root = build("n-two", H4)
fx.corrupt(root, "indices/iuuid-idx/0/index-sg-idx-0-2")
fx.corrupt(root, "index-2")
if show("3  second failure added on top of the first grows the manifest",
        one_fault, keys(root)):
    failures.append(3)


class StaleListing:
    def __init__(self, root, extra):
        self.inner = LocalMirrorSource(root)
        self.extra = extra

    def describe(self):
        return "stale listing"

    def list_keys(self):
        return self.inner.list_keys() + list(self.extra)

    def fetch(self, key):
        return self.inner.fetch(key)


# 4. A listing that still reports an object the store has already deleted puts
#    that key back in the manifest.
root = build("l-one", HISTORY)
os.unlink(os.path.join(root, "indices/iuuid-idx/0/__a"))
base = keys(root)
after = keys(StaleListing(root, ["indices/iuuid-idx/0/__a"]))
if show("4  listing reports an object the store already deleted", base, after):
    failures.append(4)

# 5. The live set is filtered by a blob-name regex that rejects a part suffix,
#    while the condemnation path re-attaches part objects to a condemned stem.
root = build("p-parts", HISTORY)
os.rename(os.path.join(root, "indices/iuuid-idx/0/__b"),
          os.path.join(root, "indices/iuuid-idx/0/__a.part0"))
for gen, snaps in ((0, {"s1": ["__a", "__shared"]}),
                   (1, {"s1": ["__a", "__shared"],
                        "s2": ["__a.part0", "__shared"]}),
                   (2, {"s2": ["__a.part0", "__shared"]})):
    names = sorted({f for v in snaps.values() for f in v})
    shard = {
        "files": [{"name": n, "physical_name": "_" + n[2:], "length": 42,
                   "checksum": "a", "written_by": "9.11.1"} for n in names],
        "snapshots": {s: {"files": v, "shard_state_id": f"st-{gen}"}
                      for s, v in snaps.items()},
    }
    with open(os.path.join(root, f"indices/iuuid-idx/0/index-sg-idx-0-{gen}"),
              "wb") as fh:
        fh.write(fx.codec_wrap(json.dumps(shard).encode(),
                               deflate=(gen % 2 == 1)))
named = keys(root)
print("\n=== 5  a part-suffixed live file is invisible to the live set")
print(f"    manifest names the live part object: "
      f"{'indices/iuuid-idx/0/__a.part0' in named}")
if "indices/iuuid-idx/0/__a.part0" in named:
    failures.append(5)

print("\n" + "=" * 60)
print("counterexamples reproduced:", failures)
sys.exit(1 if failures else 0)
