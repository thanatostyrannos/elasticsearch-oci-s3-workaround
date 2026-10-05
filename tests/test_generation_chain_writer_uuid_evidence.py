"""A writer uuid that a parsed document claimed stays evidence for the whole run.

The writer-uuid collision check drops a directory whose believed documents
claim a Lucene writer identity another directory also claims. It used to see
only directories still standing at the end of the run, and only the documents
those directories had accepted. So a second fault that dropped the witness
directory, or got its documents rejected, removed the collision along with it,
and the forged directory's file list was believed. A forged current document
that omits a live segment then puts that segment in the manifest.

Every scenario here has the same shape. D is `indices/iuuid-i/0`, A is
`indices/iuuid-j/0`. s0 is deleted at generation 2, s1 at generation 3, and s2
is live and restores D from `__d1` and `__d2`. The document served at D's
current key names s2 as `__d0, __d2`, which is two of D's own blobs at the
same file count, so the declared byte size still matches. It omits the live
`__d1`, and it carries A's writer uuid.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import genchain_repo as repo
from generation_chain import run_audit
from generation_chain.derivation.identity import WRITER_UUID_COLLISION
from generation_chain.derivation.shards import EXTENT_SIZE_NOT_DECLARED

D = repo.directory_of("i", 0)
A = repo.directory_of("j", 0)
CURRENT = 3
SHARED_WRITER = "writer-j-0"

HISTORY = [
    {"s0": {"i": {0: ["__d0"]}, "j": {0: ["__a0"]}}},
    {"s0": {"i": {0: ["__d0"]}, "j": {0: ["__a0"]}},
     "s1": {"i": {0: ["__d1"]}, "j": {0: ["__a1"]}}},
    {"s1": {"i": {0: ["__d1"]}, "j": {0: ["__a1"]}},
     "s2": {"i": {0: ["__d1", "__d2"]}, "j": {0: ["__a1", "__a2"]}}},
    {"s2": {"i": {0: ["__d1", "__d2"]}, "j": {0: ["__a1", "__a2"]}}},
]
FORGED_HISTORY = [dict(generation) for generation in HISTORY]
FORGED_HISTORY[CURRENT] = {
    "s2": {"i": {0: ["__d0", "__d2"]}, "j": {0: ["__a1", "__a2"]}}}

# s2 declares two shards for index j and two in total, so the extent check
# drops A alone: j read one shard against two declared, and the total of two
# still matches what the run read across i and j.
A_FAILS_ITS_EXTENT = dict(declared_shard_count={("s2", "j"): 2},
                          declared_total_shards={"s2": 2})


def _shard_key(index: str, generation: int) -> str:
    return (f"{repo.directory_of(index, 0)}/index-"
            f"{repo.shard_generation_id(index, 0, generation)}")


class _Store:
    """One repository built from HISTORY, with the faults a scenario asks for."""

    def __init__(self, test: unittest.TestCase, defects: dict = None) -> None:
        self.dir = tempfile.mkdtemp(prefix="genchain-writer-evidence-")
        test.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "real")
        self.built = repo.build(self.root, HISTORY,
                                defects=repo.Defects(**(defects or {})))
        self.swapped = {}
        self.unreadable = []

    def forge_d(self) -> None:
        donor = os.path.join(self.dir, "forged")
        repo.build(donor, FORGED_HISTORY, defects=repo.Defects(
            writer_uuid_of={("i", 0): SHARED_WRITER}))
        self.swapped[_shard_key("i", CURRENT)] = repo.read(
            donor, _shard_key("i", CURRENT))

    def rewrite_a(self) -> None:
        # A's index was rewritten before the current generation, so its
        # current document carries a writer its earlier eras never did.
        # identity.writer_uuid_collisions records real Elasticsearch doing
        # exactly this.
        donor = os.path.join(self.dir, "rewritten")
        repo.build(donor, HISTORY, defects=repo.Defects(
            writer_uuid_of={("j", 0): "writer-j-new"}))
        self.swapped[_shard_key("j", CURRENT)] = repo.read(
            donor, _shard_key("j", CURRENT))

    def delete_a0(self) -> None:
        # s0 was deleted at generation 2 and `__a0` was only ever s0's, so
        # a delete that succeeded removed it. A's era documents for
        # generations 0 and 1 still name it, so they parse and then fail
        # the directory check.
        repo.remove(self.root, f"{A}/__a0")

    def audit(self, extra_unreadable=()):
        source = repo.FaultySource(self.root, [repo.Fault(
            swap_bytes=self.swapped,
            unreadable=list(self.unreadable) + list(extra_unreadable))])
        return run_audit(source)

    def live_named(self, result):
        return {c.key for c in result.condemned} & self.built.live_blob_keys


def _dropped(result):
    return {directory: doubt.code
            for directory, doubt in result.coverage.shards_dropped.items()}


class TheRejectAlwaysHadItsEvidence(unittest.TestCase):
    """The cases the check already caught, kept caught."""

    def test_a_healthy_store_drops_nothing(self):
        # The abuse case for the whole guard: a check that fired on healthy
        # data would drop every shard and the audit would condemn nothing.
        store = _Store(self)
        result = store.audit()
        self.assertEqual({}, _dropped(result))
        self.assertEqual(set(), store.live_named(result))

    def test_a_forgery_against_a_healthy_witness_drops_both(self):
        # D's current document carries A's writer and A's own current
        # document is believed. If this stopped holding, the forged file list
        # would be D's live set and `__d1` would be deleted.
        store = _Store(self)
        store.forge_d()
        result = store.audit()
        self.assertEqual(WRITER_UUID_COLLISION, _dropped(result).get(D))
        self.assertEqual(WRITER_UUID_COLLISION, _dropped(result).get(A))
        self.assertEqual(set(), store.live_named(result))


class AWitnessThatParsedIsNotLostToALaterCheck(unittest.TestCase):

    def test_a_witness_dropped_by_its_extent_still_drops_the_forgery(self):
        # A's current document parsed and carried the shared writer, and then
        # the extent check dropped A for an unrelated reason. Before the fix
        # that drop took the only witness with it, nothing was left to
        # contradict D, and the live `__d1` went into the manifest.
        store = _Store(self, A_FAILS_ITS_EXTENT)
        store.forge_d()
        result = store.audit()
        self.assertEqual(WRITER_UUID_COLLISION, _dropped(result).get(D))
        self.assertEqual(set(), store.live_named(result))

    def test_era_documents_of_a_directory_the_extent_check_drops_still_witness(
            self):
        # A was rewritten, so only its era documents carry the shared writer,
        # and the extent check drops A before the era pass. The era pass used
        # to read only directories still standing, so A's era documents were
        # never parsed, nothing contradicted D, and the live `__d1` went into
        # the manifest. Every new reason the extent check gains to drop a
        # directory widens this, so the witness has to survive the drop.
        # Neutered under "an-extent-drop-keeps-its-era-witnesses".
        store = _Store(self, A_FAILS_ITS_EXTENT)
        store.forge_d()
        store.rewrite_a()
        result = store.audit()
        self.assertEqual(WRITER_UUID_COLLISION, _dropped(result).get(D))
        self.assertEqual(set(), store.live_named(result))

    def test_a_witness_dropped_for_an_undeclared_size_still_witnesses(self):
        # The same witness, dropped by the newest reason the extent check
        # has: s2 declares no size for index j. A fix that made that drop
        # without keeping A's era writers would trade one live key for
        # another, `__d1` here.
        store = _Store(self, dict(index_detail_changes={
            ("s2", "j"): {"size_in_bytes": repo.REMOVE}}))
        store.forge_d()
        store.rewrite_a()
        result = store.audit()
        self.assertEqual(EXTENT_SIZE_NOT_DECLARED, _dropped(result).get(A))
        self.assertEqual(WRITER_UUID_COLLISION, _dropped(result).get(D))
        self.assertEqual(set(), store.live_named(result))

    def test_a_witness_in_rejected_era_documents_still_drops_the_forgery(self):
        # A was rewritten, so only its era documents carry the shared writer.
        # The newest era answers 503 and the two older ones parse and are then
        # rejected because they name a segment a successful delete removed.
        # Before the fix the rejection discarded the writer uuids along with
        # the file lists, and the live `__d1` went into the manifest.
        store = _Store(self)
        store.forge_d()
        store.rewrite_a()
        store.delete_a0()
        store.unreadable.append(_shard_key("j", 2))
        result = store.audit()
        self.assertEqual(WRITER_UUID_COLLISION, _dropped(result).get(D))
        self.assertEqual(set(), store.live_named(result))

    def test_the_rejected_documents_leave_the_witness_directory_standing(self):
        # The check drops a directory for what it BELIEVES, not for what a
        # rejected document under it claimed. A's believed writer is the new
        # one and nothing else claims it, so A keeps its coverage. Dropping A
        # too would cost a healthy directory for D's forgery.
        store = _Store(self)
        store.forge_d()
        store.rewrite_a()
        store.delete_a0()
        store.unreadable.append(_shard_key("j", 2))
        self.assertNotIn(A, _dropped(store.audit()))


class LegitimateHistoryIsNotACollision(unittest.TestCase):
    """What Elasticsearch really writes must survive the wider evidence."""

    def test_a_directory_whose_writer_changed_across_generations_is_kept(self):
        # A rewritten index carries different writers in different
        # generations of ONE directory. Counting that as a collision would
        # drop every shard of every index that was ever force-merged.
        store = _Store(self)
        store.rewrite_a()
        self.assertEqual({}, _dropped(store.audit()))

    def test_a_directory_s_own_rejected_era_documents_do_not_drop_it(self):
        # The abuse case for recording writers before the identity checks.
        # Era documents that name a since-deleted segment are routine in a
        # repository where some deletes succeeded. They now count as
        # evidence, and evidence under a directory's own name must never
        # count against that same directory.
        store = _Store(self)
        store.rewrite_a()
        store.delete_a0()
        result = store.audit()
        self.assertNotIn(WRITER_UUID_COLLISION, set(_dropped(result).values()))
        self.assertNotIn(A, _dropped(result))

    def test_reading_an_extent_dropped_directory_s_eras_costs_no_one_else(self):
        # The use case for reading the era documents of a directory the
        # extent check dropped. A's rewritten history is ordinary, and its
        # eras now count as evidence even though A itself is gone. If that
        # evidence ever counted against a directory with no forgery in it,
        # one snapshot document with a wrong count would cost D its coverage
        # as well as A.
        store = _Store(self, A_FAILS_ITS_EXTENT)
        store.rewrite_a()
        self.assertEqual({A}, set(_dropped(store.audit())))


class OneMoreReadFailureNeverGrowsTheManifest(unittest.TestCase):
    """The monotonicity the module docstring claims, for these scenarios.

    Each scenario is audited once as built and once more for every key in
    the store failing on its own on top of that. A failure may cost
    coverage. It may never add a key, because a key a failure adds is a key
    the healthy run had a reason to keep.
    """

    def _assert_no_failure_grows(self, store):
        healthy = store.audit()
        self.assertEqual(set(), store.live_named(healthy))
        baseline = {c.key for c in healthy.condemned}
        grew = {}
        for key in repo.read_keys(store.root):
            result = store.audit(extra_unreadable=[key])
            added = {c.key for c in result.condemned} - baseline
            if added or store.live_named(result):
                grew[key] = sorted(added | store.live_named(result))
        self.assertEqual({}, grew)

    def test_the_extent_scenario_is_monotone_in_one_more_failure(self):
        # The extent drop of A was where one fault turned "D dropped" into a
        # live key in the manifest. Any single additional failure here must
        # not reopen that.
        store = _Store(self, A_FAILS_ITS_EXTENT)
        store.forge_d()
        self._assert_no_failure_grows(store)

    def test_the_rejected_era_scenario_is_monotone_in_one_more_failure(self):
        # Same property for the era scenario, where two of A's documents
        # carry the shared writer. Losing either one alone must leave the
        # other as the witness.
        store = _Store(self)
        store.forge_d()
        store.rewrite_a()
        store.delete_a0()
        store.unreadable.append(_shard_key("j", 2))
        self._assert_no_failure_grows(store)

    def test_the_extent_dropped_era_scenario_is_monotone_in_one_more_failure(
            self):
        # A's three era documents each carry the shared writer after its
        # extent drop. Losing any one of them must leave the other two as
        # witnesses, and losing a document of D must not admit its forgery.
        store = _Store(self, A_FAILS_ITS_EXTENT)
        store.forge_d()
        store.rewrite_a()
        self._assert_no_failure_grows(store)


if __name__ == "__main__":
    unittest.main()
