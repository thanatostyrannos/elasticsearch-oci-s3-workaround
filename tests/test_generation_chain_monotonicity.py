"""Adding a fault must never make the manifest longer.

The reachability sweepers in this repository condemn on ABSENCE, so a read that
fails there manufactures an orphan. This derivation condemns on PRESENCE, and
the claim is the opposite one: no input can make it name a key it would not
have named from a healthy store. That is the property this file searches for a
counterexample to.

WHY THE SEARCH KEPT PASSING WITHOUT MEASURING ANYTHING. The generator this
replaces could not reach three failing regions, twice over. It applied one
source mutation at a time, so an arrangement needing a swap AND a listing
change together could not be built. Every fixture document named at least one
blob, so the empty-document cases had no donor anywhere in the corpus. And both
same-generation swaps used donors whose blob sets were NOT subsets of the
victim directory, so the containment test was exercised only where it succeeds.
A generator that produces only inputs the first check catches is a fixture
wearing a property test's clothes.

SO THE RULE THIS GENERATOR IS BUILT TO. For every check the derivation makes,
this generator must be able to produce an input that SATISFIES that check and
is still wrong. `TheGeneratorReachesEveryRegion` asserts that by construction,
one test per region: it shows the upstream checks PASSING on the input and
names which check catches it. A region with no such input is a check nothing
downstream of it is ever tested against.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import genchain_repo as repo
from generation_chain import run_audit
from generation_chain.derivation import identity, shards
from generation_chain.derivation.keys import KeyIndex
from generation_chain.formats.shard_snapshots import parse_shard_snapshots
from generation_chain.sources.local import LocalMirrorSource

# Three indices. `idx` has TWO shards, so a swap between two shards of one
# index is reachable, and that swap carries identical snapshot names to a swap
# between two indices, which is the region the name-set check cannot see into.
# `keep` survives to the end. `other` leaves the catalog.
#
# One Elasticsearch operation per step: create, create, create, delete, delete.
# A step that both adds and removes is not something Elasticsearch writes and
# the derivation refuses to interpret one, so a fixture built that way would
# exercise nothing.
_S1 = {"idx": {0: ["__a", "__shared"], 1: ["__p"]}, "other": {0: ["__o"]},
       "keep": {0: ["__k1"]}}
_S2 = {"idx": {0: ["__b", "__shared"], 1: ["__p"]}, "other": {0: ["__o"]},
       "keep": {0: ["__k1"]}}
_S3 = {"idx": {0: ["__c"], 1: ["__q"]}, "keep": {0: ["__k2"]}}
HISTORY = [
    {"s1": _S1},
    {"s1": _S1, "s2": _S2},
    {"s1": _S1, "s2": _S2, "s3": _S3},
    {"s2": _S2, "s3": _S3},
    {"s3": _S3},
]
ANCHOR = 4

IDX_0 = repo.directory_of("idx", 0)
IDX_1 = repo.directory_of("idx", 1)
KEEP_0 = repo.directory_of("keep", 0)

OTHER_0 = repo.directory_of("other", 0)

# Objects planted in `idx/0` under a name that also lives in `other/0`. Without
# them no donor from another directory can ever be CONTAINED in the victim, so
# the containment test is exercised only in the arrangement where it succeeds.
# That is the corpus gap that made the subset region unreachable twice.
#
# The name comes from `other`, the index that LEAVES the catalog, and that
# choice is load bearing. A decoy taken from a surviving index removes that
# index's unique witness, which drops its shard, which fails the declared
# extent of the snapshot covering it, which drops every other shard of that
# snapshot. The first cut of this file decoyed four names from two live
# indices and the healthy baseline came back with no segment in it at all, so
# every subset assertion in the search would have passed against nothing.
DECOY_BLOBS = ["__o"]
DECOYS = {IDX_0: list(DECOY_BLOBS)}

SHAPES = {"uuid-shard-generations": False, "numeric-shard-generations": True}


def build(root: str, numeric: bool, defects: repo.Defects = None) -> repo.Built:
    defects = defects or repo.Defects()
    defects.numeric_shard_generations = numeric
    defects.decoy_blobs = dict(DECOYS)
    return repo.build(root, HISTORY, defects=defects)


# -- faults a file edit can express ------------------------------------------

def _tolerant(action):
    """Mutations combine, so one may find the file another already ruined.

    A combination whose second mutation cannot apply is still the combination
    of the faults that did apply, so it stays in the search rather than
    aborting it.
    """
    def apply(root):
        try:
            action(root)
        except (OSError, ValueError, KeyError, IndexError):
            pass
    return apply


def corrupt(rel):
    def apply(root):
        data = bytearray(repo.read(root, rel))
        data[len(data) // 2] ^= 0xFF
        repo.overwrite(root, rel, bytes(data))
    return _tolerant(apply)


def truncate(rel, keep=12):
    return _tolerant(
        lambda root: repo.overwrite(root, rel, repo.read(root, rel)[:keep]))


def remove(rel):
    return _tolerant(lambda root: repo.remove(root, rel))


def rewrite_generation(generation, change):
    def apply(root):
        key = f"index-{generation}"
        document = json.loads(repo.read(root, key).decode("utf-8"))
        change(document)
        repo.overwrite(root, key,
                       json.dumps(document, sort_keys=True).encode("utf-8"))
    return _tolerant(apply)


def _shorten_lookup(document):
    for snapshot in document["snapshots"]:
        snapshot.get("index_metadata_lookup", {}).pop(repo.index_uuid("other"),
                                                      None)


def _retype_lookup(document):
    for snapshot in document["snapshots"]:
        for key in list(snapshot.get("index_metadata_lookup", {})):
            snapshot["index_metadata_lookup"][key] = 12345


def _empty_catalog(document):
    document["snapshots"] = []
    document["indices"] = {}


def _drop_shard_generations(document):
    for entry in document["indices"].values():
        entry["shard_generations"] = []


def _null_a_shard_generation(document):
    for entry in document["indices"].values():
        if entry["shard_generations"]:
            entry["shard_generations"][0] = None


def _old_min_version(document):
    document["min_version"] = "7.11.0"


def _wrong_latest(root):
    repo.overwrite(root, "index.latest", struct.pack(">q", 99))


def _lagging_latest(root):
    repo.overwrite(root, "index.latest", struct.pack(">q", ANCHOR - 1))


def _on_shard(index, shard, generation, action):
    def apply(root):
        action(repo.shard_document_key(root, index, shard, generation))(root)
    return _tolerant(apply)


FILE_FAULTS = [
    ("corrupt-latest", corrupt("index.latest")),
    ("latest-points-nowhere", _tolerant(_wrong_latest)),
    ("latest-lags-the-listing", _tolerant(_lagging_latest)),
    ("corrupt-gen1", corrupt("index-1")),
    ("truncate-gen2", truncate("index-2")),
    ("remove-gen2", remove("index-2")),
    ("corrupt-gen3", corrupt("index-3")),
    ("truncate-anchor", truncate(f"index-{ANCHOR}")),
    ("short-lookup-anchor", rewrite_generation(ANCHOR, _shorten_lookup)),
    ("retype-lookup-gen2", rewrite_generation(2, _retype_lookup)),
    ("empty-catalog-gen2", rewrite_generation(2, _empty_catalog)),
    ("no-shard-generations-gen1", rewrite_generation(1, _drop_shard_generations)),
    ("null-shard-generation-anchor",
     rewrite_generation(ANCHOR, _null_a_shard_generation)),
    ("min-version-below-the-floor-gen1",
     rewrite_generation(1, _old_min_version)),
    ("corrupt-shard-idx-0-gen0", _on_shard("idx", 0, 0, corrupt)),
    ("truncate-shard-idx-0-gen2", _on_shard("idx", 0, 2, truncate)),
    ("remove-shard-idx-0-gen3", _on_shard("idx", 0, 3, remove)),
    ("corrupt-shard-idx-1-gen1", _on_shard("idx", 1, 1, corrupt)),
    ("remove-a-blob", remove(f"{IDX_0}/__a")),
    ("remove-a-snapshot-document",
     remove(f"snap-{repo.snapshot_uuid('s1')}.dat")),
]


# -- faults only a store can express -----------------------------------------
#
# Each entry names the check its input SATISFIES. That is the rule the corpus
# is built to, written where the donors are so it cannot drift from them.

def _live_names(root):
    document = json.loads(repo.read(root, f"index-{ANCHOR}").decode("utf-8"))
    return [entry["name"] for entry in document["snapshots"]]


def _victim(root, numeric):
    return repo.shard_document_key(root, "idx", 0, ANCHOR, numeric)


def _stems_in(root, directory):
    from generation_chain.formats.shard_snapshots import segment_stem
    path = os.path.join(root, directory)
    if not os.path.isdir(path):
        return set()
    return {s for s in (segment_stem(n) for n in os.listdir(path)) if s}


STORE_FAULTS = [
    # SATISFIES containment, vacuously: the empty set is a subset of every
    # directory in the repository.
    ("donor-naming-nothing",
     lambda root, numeric: repo.Fault(
         swap_bytes={_victim(root, numeric): repo.forge_document(
             {n: [] for n in _live_names(root)})})),
    # SATISFIES containment and the name set. The shape Elasticsearch really
    # writes when it snapshots an empty index: the whole commit fits inline.
    ("donor-naming-only-inline-entries",
     lambda root, numeric: repo.Fault(
         swap_bytes={_victim(root, numeric): repo.forge_document(
             {n: ["v__commit"] for n in _live_names(root)})})),
    # SATISFIES containment and the name set. Its blobs really are objects in
    # the victim directory, because DECOYS put them there.
    ("donor-contained-in-the-victim",
     lambda root, numeric: repo.Fault(
         swap_bytes={_victim(root, numeric): repo.forge_document(
             {n: list(DECOY_BLOBS) + ["v__commit"]
              for n in _live_names(root)})})),
    # SATISFIES containment, the name set, and attributability AS THE LISTING
    # SEES IT. Only asking the store settles it.
    ("donor-with-a-witness-the-listing-invented",
     lambda root, numeric: repo.Fault(
         extra=[f"{IDX_0}/__invented"], denied=[f"{IDX_0}/__invented"],
         swap_bytes={_victim(root, numeric): repo.forge_document(
             {n: ["__invented", "v__commit"] for n in _live_names(root)})})),
    # SATISFIES containment, attributability and the writer uuid, genuinely.
    # The document really is this directory's, one generation out of date.
    ("donor-an-older-generation-of-this-shard",
     lambda root, numeric: repo.Fault(swap_keys={
         _victim(root, numeric): repo.shard_document_key(root, "idx", 0, 0,
                                                         numeric)})),
    # SATISFIES the name set. Two shards of ONE index carry identical snapshot
    # names at one generation, which is the region that check cannot enter.
    ("swap-two-shards-of-one-index",
     lambda root, numeric: repo.Fault(swap_keys={
         _victim(root, numeric): repo.shard_document_key(root, "idx", 1,
                                                         ANCHOR, numeric)})),
    ("swap-two-indices",
     lambda root, numeric: repo.Fault(swap_keys={
         _victim(root, numeric): repo.shard_document_key(root, "keep", 0,
                                                         ANCHOR, numeric)})),
    # SATISFIES every check on the bytes. Nothing inside a RepositoryData names
    # its own generation, so only the shape of the transition betrays it.
    ("swap-the-anchor-for-an-older-generation",
     lambda root, numeric: repo.Fault(swap_keys={f"index-{ANCHOR}": "index-0"})),
    ("swap-one-generation-for-another",
     lambda root, numeric: repo.Fault(swap_keys={"index-2": "index-0"})),
    # Listings that lag the store in either direction, which is what makes an
    # otherwise-caught swap look attributable.
    ("listing-over-reports-the-victim",
     lambda root, numeric: repo.Fault(
         extra=[f"{IDX_0}/{s}" for s in sorted(_stems_in(root, IDX_1))])),
    ("listing-hides-the-donor-directory",
     lambda root, numeric: repo.Fault(
         hidden=[f"{KEEP_0}/{s}" for s in sorted(_stems_in(root, KEEP_0))])),
    ("listing-holds-a-deleted-object",
     lambda root, numeric: repo.Fault(extra=[f"{IDX_0}/__gone"])),
    ("listing-lost-a-generation",
     lambda root, numeric: repo.Fault(hidden=["index-1"])),
    ("listing-lost-a-blob",
     lambda root, numeric: repo.Fault(hidden=[f"{IDX_0}/__a"])),
    ("listing-lost-a-live-snapshots-shard-document",
     lambda root, numeric: repo.Fault(hidden=[
         f"{KEEP_0}/snap-{repo.snapshot_uuid('s3')}.dat",
         f"{IDX_0}/snap-{repo.snapshot_uuid('s3')}.dat"])),
    ("listing-order-is-arbitrary",
     lambda root, numeric: repo.Fault(shuffle=True)),
    # The store denies or cannot answer for a key the listing gave. These are
    # different failures and only the second must never be recorded as a no.
    ("store-denies-a-listed-blob",
     lambda root, numeric: repo.Fault(denied=[f"{IDX_0}/__a"])),
    ("store-cannot-answer-for-a-listed-blob",
     lambda root, numeric: repo.Fault(unanswerable=[f"{IDX_0}/__a"])),
    ("store-will-not-hand-over-a-snapshot-document",
     lambda root, numeric: repo.Fault(
         unreadable=[f"snap-{repo.snapshot_uuid('s3')}.dat"])),
]


def _fault(name, root, numeric):
    """Build one store fault against a tree other faults may already have ruined.

    A fault that cannot be built is simply not applied, which leaves the
    combination smaller rather than aborting the search.
    """
    try:
        return dict(STORE_FAULTS)[name](root, numeric)
    except (OSError, ValueError, KeyError, IndexError):
        return repo.Fault()


class Monotonicity(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="genchain-monotone-")
        cls.healthy = {}
        cls.built = {}
        cls.baselines = {}
        for shape, numeric in SHAPES.items():
            root = os.path.join(cls.dir, shape)
            cls.built[shape] = build(root, numeric)
            cls.healthy[shape] = root
            cls.baselines[shape] = set(
                run_audit(LocalMirrorSource(root)).keys)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def run_with(self, file_names=(), store_names=(), shape=None):
        shape = shape or "uuid-shard-generations"
        root = os.path.join(self.dir, "case")
        shutil.rmtree(root, ignore_errors=True)
        shutil.copytree(self.healthy[shape], root)
        for name in file_names:
            dict(FILE_FAULTS)[name](root)
        if not store_names:
            return set(run_audit(LocalMirrorSource(root)).keys)
        faults = [_fault(n, root, SHAPES[shape]) for n in store_names]
        return set(run_audit(repo.FaultySource(root, faults)).keys)

    def test_the_healthy_repository_names_what_the_deletes_left(self):
        # The baseline every subset assertion is measured against. If it
        # shrinks to nothing those assertions all pass vacuously, which is the
        # way a property test quietly stops testing anything.
        for shape, baseline in self.baselines.items():
            self.assertIn(f"{IDX_0}/__a", baseline, shape)
            self.assertIn(f"{IDX_0}/__b", baseline, shape)
            self.assertGreaterEqual(len(baseline), 8, shape)
            # Live at the anchor, so never nameable.
            self.assertNotIn(f"{IDX_0}/__c", baseline, shape)
            self.assertNotIn(f"{IDX_1}/__q", baseline, shape)
            self.assertNotIn(f"{KEEP_0}/__k2", baseline, shape)

    def test_the_healthy_repository_names_no_live_key(self):
        # Stated against the fixture's own declaration of what is live rather
        # than against anything the tool derived, so the two are independent.
        for shape, built in self.built.items():
            self.assertEqual(set(),
                             built.live_blob_keys & self.baselines[shape], shape)

    def test_no_combination_of_up_to_three_file_faults_grows_the_manifest(self):
        names = [name for name, _ in FILE_FAULTS]
        checked = 0
        for shape, baseline in self.baselines.items():
            for size in range(0, 4):
                for combination in itertools.combinations(names, size):
                    got = self.run_with(combination, shape=shape)
                    checked += 1
                    self.assertTrue(
                        got.issubset(baseline),
                        f"{shape} {combination} added {sorted(got - baseline)}")
        # Twenty faults taken up to three at a time, over both id shapes. The
        # number is asserted so a fault list that silently shrank, or a loop
        # bound edited down, cannot leave the search claiming to cover more
        # than it ran.
        self.assertGreater(checked, 2700)

    def test_no_pair_of_store_faults_grows_the_manifest(self):
        # The failures a file edit cannot express: a listing that lags in
        # either direction, an arbitrary order, an existence check that denies
        # or cannot answer, and a store that hands over the wrong object with a
        # 200. The last is the nastiest, because a successful read of the wrong
        # bytes is not a failure any per-read check sees.
        names = [name for name, _ in FILE_FAULTS]
        stores = [name for name, _ in STORE_FAULTS]
        checked = 0
        for shape, baseline in self.baselines.items():
            for pair in itertools.chain(itertools.combinations(stores, 1),
                                        itertools.combinations(stores, 2)):
                for size in range(0, 2):
                    for combination in itertools.combinations(names, size):
                        got = self.run_with(combination, pair, shape)
                        checked += 1
                        self.assertTrue(
                            got.issubset(baseline),
                            f"{shape} {pair} + {combination} added "
                            f"{sorted(got - baseline)}")
        self.assertGreater(checked, 3000)

    def test_no_fault_of_any_kind_puts_a_live_key_in_a_manifest(self):
        # Subset of the baseline is the property. This is the consequence that
        # matters, asserted separately because a baseline that ever contained a
        # live key would make the subset assertions agree with each other while
        # both were wrong.
        stores = [name for name, _ in STORE_FAULTS]
        files = [name for name, _ in FILE_FAULTS]
        for shape, built in self.built.items():
            for pair in itertools.combinations(stores, 2):
                got = self.run_with((), pair, shape)
                self.assertEqual(set(), built.live_blob_keys & got,
                                 f"{shape} {pair}")
            for combination in itertools.combinations(files, 2):
                got = self.run_with(combination, (), shape)
                self.assertEqual(set(), built.live_blob_keys & got,
                                 f"{shape} {combination}")


class TheGeneratorReachesEveryRegion(unittest.TestCase):
    """For every check, an input that SATISFIES it and is still wrong.

    Proved by construction rather than hoped for. Each test builds the donor,
    shows the checks AHEAD of the named one returning a pass on it, and shows
    the named one firing. A region with no such input is a check that nothing
    downstream of it has ever been tested against, which is how this generator
    passed twice while measuring nothing.
    """

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="genchain-reach-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.built = build(self.dir, numeric=False)
        self.source = LocalMirrorSource(self.dir)
        self.keys = self.source.list_keys()

    def _context(self, extra=(), denied=()):
        source = repo.FaultySource(
            self.dir, [repo.Fault(extra=list(extra), denied=list(denied))])
        keys = source.list_keys()
        present = shards._blobs_present(keys)
        return (frozenset(present.get(IDX_0, set())), shards._owners(present),
                KeyIndex(keys, source))

    def _parse(self, data):
        return parse_shard_snapshots(data, "donor")

    def test_containment_is_satisfied_by_a_document_naming_nothing(self):
        # SATISFIED: containment, vacuously. CAUGHT BY: require_blob_names.
        document = self._parse(repo.forge_document({"s3": ["v__commit"]}))
        stems, owners, index = self._context()
        self.assertEqual(frozenset(), document.blob_names)
        self.assertEqual(frozenset(), document.blob_names - stems)
        with self.assertRaises(Exception):
            identity.require_blob_names(document, "donor")

    def test_containment_is_satisfied_by_a_donor_contained_in_the_victim(self):
        # SATISFIED: containment, for real, because DECOYS put objects under
        # those names in the victim directory. CAUGHT BY: attributability.
        document = self._parse(repo.forge_document(
            {"s3": list(DECOY_BLOBS) + ["v__commit"]}))
        stems, owners, index = self._context()
        self.assertEqual(frozenset(), document.blob_names - stems)
        doubt = identity.check_directory(document, "donor", IDX_0, stems,
                                         owners, index)
        self.assertEqual(identity.NO_UNIQUE_WITNESS, doubt.code)

    def test_attributability_by_the_listing_is_satisfied_by_an_invented_blob(self):
        # SATISFIED: containment AND attributability as the LISTING sees it,
        # which is everything the retired code checked.
        # CAUGHT BY: asking the store.
        invented = "__invented"
        document = self._parse(repo.forge_document(
            {"s3": [invented, "v__commit"]}))
        stems, owners, index = self._context(extra=[f"{IDX_0}/{invented}"],
                                             denied=[f"{IDX_0}/{invented}"])
        self.assertEqual(frozenset(), document.blob_names - stems)
        self.assertEqual({IDX_0}, owners[invented])
        doubt = identity.check_directory(document, "donor", IDX_0, stems,
                                         owners, index)
        self.assertEqual(identity.WITNESS_UNCONFIRMED, doubt.code)

    def test_attributability_is_satisfied_by_an_older_generation_of_the_shard(self):
        # SATISFIED: containment and attributability, genuinely. The witnesses
        # really are unique to this directory because the document really is
        # this directory's. CAUGHT BY: the snapshot-name set, and by nothing
        # else, which is why that check is stated rather than dressed up.
        document = self._parse(repo.read(
            self.dir, repo.shard_document_key(self.dir, "idx", 0, 0)))
        stems, owners, index = self._context()
        self.assertIsNone(identity.check_directory(document, "donor", IDX_0,
                                                   stems, owners, index))
        doubt = identity.check_snapshot_names(document, "donor", {"s3"})
        self.assertEqual(identity.SNAPSHOT_NAMES_DISAGREE, doubt.code)

    def test_the_name_set_is_satisfied_by_the_other_shard_of_the_same_index(self):
        # SATISFIED: the snapshot-name set, because two shards of ONE index
        # carry identical names at one generation. CAUGHT BY: containment,
        # unless the listing over-reports, which the next test covers.
        document = self._parse(repo.read(
            self.dir, repo.shard_document_key(self.dir, "idx", 1, ANCHOR)))
        stems, owners, index = self._context()
        self.assertIsNone(identity.check_snapshot_names(
            document, "donor", set(document.by_snapshot_name)))
        doubt = identity.check_directory(document, "donor", IDX_0, stems,
                                         owners, index)
        self.assertIn(doubt.code,
                      (identity.NAMES_BLOBS_NOT_HERE, identity.NO_UNIQUE_WITNESS))

    def test_containment_and_the_name_set_are_both_satisfied_at_once(self):
        # The arrangement that needs TWO faults composed: an over-reporting
        # listing to make the other shard's blobs look present here, AND the
        # swap. One source mutation at a time could not build this at all,
        # which is the gap that made the region unreachable.
        document = self._parse(repo.read(
            self.dir, repo.shard_document_key(self.dir, "idx", 1, ANCHOR)))
        stems, owners, index = self._context(
            extra=[f"{IDX_0}/{s}" for s in sorted(_stems_in(self.dir, IDX_1))])
        self.assertEqual(frozenset(), document.blob_names - stems)
        self.assertIsNone(identity.check_snapshot_names(
            document, "donor", set(document.by_snapshot_name)))
        doubt = identity.check_directory(document, "donor", IDX_0, stems,
                                         owners, index)
        self.assertEqual(identity.NO_UNIQUE_WITNESS, doubt.code)

    def test_every_donor_the_corpus_declares_is_actually_buildable(self):
        # The corpus claims each store fault SATISFIES some check. A donor that
        # silently fails to build reduces the search to a smaller one without
        # saying so, and that is exactly how the previous generator lost three
        # regions. This asserts every one of them produces a real fault.
        for name, _ in STORE_FAULTS:
            fault = _fault(name, self.dir, False)
            self.assertTrue(
                fault.swap_bytes or fault.swap_keys or fault.extra
                or fault.hidden or fault.denied or fault.unanswerable
                or fault.unreadable or fault.shuffle,
                f"{name} built an empty fault, so it searches nothing")

    def test_the_decoy_blobs_really_make_a_donor_contained(self):
        # The corpus gap, asserted directly. A decoy that failed to land would
        # leave every donor from another directory outside the victim, and the
        # containment test would again be exercised only where it succeeds.
        self.assertTrue(set(DECOY_BLOBS).issubset(_stems_in(self.dir, IDX_0)))
        self.assertTrue(set(DECOY_BLOBS).issubset(_stems_in(self.dir, OTHER_0)))

    def test_the_decoys_do_not_silently_empty_the_baseline(self):
        # The abuse case for the decoys themselves. They are an anomaly in the
        # listing, so they can drop shards, and a corpus that drops every shard
        # searches nothing while reporting thousands of cases checked.
        result = run_audit(LocalMirrorSource(self.dir))
        self.assertEqual({}, result.coverage.shards_dropped)
        self.assertIn(f"{IDX_0}/__a", result.keys)


if __name__ == "__main__":
    unittest.main()
