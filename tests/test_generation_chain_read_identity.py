"""Whether a read returned the object it asked for.

Nothing inside a BlobStoreIndexShardSnapshots names its own shard, its own
index or its own generation, so a store that answers one key with another
shard's document and a 200 has committed no error any per-read check can see.
Every counterexample in this file is that shape, and each one is a donor a
reviewer actually produced against the retired derivation.

The tests assert reason CODES rather than sentences. A guard pinned by its
wording is a guard a rewording frees, and the retired suite had 21 percent of
its assertions on literal strings.
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
from generation_chain.derivation import identity, shards
from generation_chain.errors import ShapeGateError
from generation_chain.formats.shard_snapshots import parse_shard_snapshots
from generation_chain.sources.local import LocalMirrorSource

# Two indices. `victim` is where a donor gets planted; `donor` supplies real
# documents from a directory that is not the victim's.
HISTORY = [
    {"s1": {"victim": {0: ["__v1", "__vshared"]},
            "donor": {0: ["__d1"]}}},
    {"s1": {"victim": {0: ["__v1", "__vshared"]},
            "donor": {0: ["__d1"]}},
     "s2": {"victim": {0: ["__vshared", "__v2"]},
            "donor": {0: ["__d1"]}}},
    {"s2": {"victim": {0: ["__vshared", "__v2"]},
            "donor": {0: ["__d1"]}}},
]

VICTIM = repo.directory_of("victim", 0)
DONOR = repo.directory_of("donor", 0)


class ADocumentThatNamesNoBlob(unittest.TestCase):
    """It parses, it declares nothing, and it fits every directory.

    The retired sweeper carried this gate at `s3_repo_sweeper.py:2743` and this
    package dropped it while keeping the docstring that described it. A
    document naming nothing is a SUBSET of every directory in the repository,
    so containment cannot separate it from the real one, and the empty live set
    it yields condemns the whole directory it lands in.
    """

    def test_a_document_with_no_files_and_no_snapshots_raises(self):
        # The gate itself, on the parsed record, so no caller can obtain such a
        # document by any route.
        document = parse_shard_snapshots(
            repo.forge_document({}), "indices/x/0/index-g")
        with self.assertRaises(ShapeGateError):
            identity.require_blob_names(document, "indices/x/0/index-g")

    def test_a_document_naming_only_inline_entries_raises(self):
        # The same hole with a donor Elasticsearch really writes. Snapshotting
        # an empty index produces a shard document whose whole commit fits
        # inline as a `v__` entry, so it parses, it names a Lucene commit, and
        # it yields no blob name at all.
        document = parse_shard_snapshots(
            repo.forge_document({"s2": ["v__commit"]}), "indices/x/0/index-g")
        self.assertEqual(frozenset(), document.blob_names)
        with self.assertRaises(ShapeGateError):
            identity.require_blob_names(document, "indices/x/0/index-g")

    def test_a_real_document_does_not_raise(self):
        # The abuse case for both. A gate that refuses everything protects
        # nothing, because the shard it drops contributes no manifest either.
        document = parse_shard_snapshots(
            repo.forge_document({"s2": ["__v1", "v__commit"]}),
            "indices/x/0/index-g")
        identity.require_blob_names(document, "indices/x/0/index-g")


class ADocumentPlantedInAnotherDirectory(unittest.TestCase):
    """The store answers the victim's key with somebody else's document.

    Each test names the check that the donor SATISFIES and the check that
    catches it, because a donor every check rejects proves nothing about the
    checks after the first.
    """

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="genchain-identity-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.built = repo.build(self.dir, HISTORY)
        self.current = repo.shard_document_key(self.dir, "victim", 0, 2)

    def _audit(self, *faults):
        return run_audit(repo.FaultySource(self.dir, faults))

    def _drop_code(self, result, directory=VICTIM):
        doubt = result.coverage.shards_dropped.get(directory)
        return doubt.code if doubt is not None else None

    def test_a_donor_naming_nothing_satisfies_containment_and_is_still_caught(self):
        # SATISFIES: containment, vacuously, because the empty set is a subset
        # of every directory. CAUGHT BY: the requirement that a document name a
        # blob at all, which turns the read into one that did not happen.
        result = self._audit(repo.Fault(
            swap_bytes={self.current: repo.forge_document(
                {"s2": ["v__commit"]})}))
        self.assertEqual(shards.CURRENT_DOCUMENT_UNREADABLE,
                         self._drop_code(result))
        self.assertEqual(set(), self.built.live_blob_keys & set(result.keys))

    def test_a_donor_from_another_directory_satisfies_the_name_set(self):
        # SATISFIES: the snapshot-name set, because at one generation every
        # shard of every index a snapshot covers carries identical names.
        # CAUGHT BY: containment, since the donor's blobs are not here.
        result = self._audit(repo.Fault(swap_keys={
            self.current: repo.shard_document_key(self.dir, "donor", 0, 2)}))
        self.assertEqual(identity.NAMES_BLOBS_NOT_HERE, self._drop_code(result))
        self.assertEqual(set(), self.built.live_blob_keys & set(result.keys))

    def test_a_contained_donor_satisfies_containment_and_is_still_caught(self):
        # SATISFIES: containment AND the snapshot-name set. The donor names
        # only blobs that really are objects in the victim directory, which is
        # the counterexample that traded a dead segment for a live one while
        # the manifest count did not move.
        # CAUGHT BY: attributability. The blobs it names are shared with the
        # directory it was really written for, so none of them is a witness
        # that belongs to the victim alone.
        result = self._audit(
            repo.Fault(extra=[f"{VICTIM}/__d1"]),
            repo.Fault(swap_bytes={self.current: repo.forge_document(
                {"s2": ["__d1", "v__commit"]})}))
        self.assertEqual(identity.NO_UNIQUE_WITNESS, self._drop_code(result))
        self.assertEqual(set(), self.built.live_blob_keys & set(result.keys))

    def test_a_witness_the_store_will_not_confirm_is_not_a_witness(self):
        # SATISFIES: containment, attributability by the LISTING, and the name
        # set. Everything the retired code checked.
        # CAUGHT BY: asking the store. The listing claimed a blob the store
        # does not hold, and the retired code trusted the listing here while
        # distrusting it where it could add a key to the manifest.
        forged = f"{VICTIM}/__forged-witness"
        result = self._audit(
            repo.Fault(extra=[forged], denied=[forged]),
            repo.Fault(swap_bytes={self.current: repo.forge_document(
                {"s2": ["__forged-witness", "v__commit"]})}))
        self.assertEqual(identity.WITNESS_UNCONFIRMED, self._drop_code(result))
        self.assertEqual(set(), self.built.live_blob_keys & set(result.keys))

    def test_an_older_generation_of_the_same_shard_satisfies_attributability(self):
        # SATISFIES: containment and attributability, genuinely, because the
        # document really is this directory's and its witnesses really are
        # unique to it. This is the region no per-directory check can see into.
        # CAUGHT BY: the snapshot-name set, which is what separates generations
        # and is stated as the only thing standing here.
        result = self._audit(repo.Fault(swap_keys={
            self.current: repo.shard_document_key(self.dir, "victim", 0, 0)}))
        self.assertEqual(identity.SNAPSHOT_NAMES_DISAGREE,
                         self._drop_code(result))
        self.assertEqual(set(), self.built.live_blob_keys & set(result.keys))

    def test_the_unfaulted_repository_reads_the_victim_shard(self):
        # The abuse case for every test above. All five assert that the shard
        # was DROPPED, and a run that dropped it unconditionally would satisfy
        # all five while measuring nothing.
        result = self._audit()
        self.assertEqual({}, result.coverage.shards_dropped)
        self.assertIn(f"{VICTIM}/__v1", result.keys)


class ALiveSnapshotsDocumentLyingInTheDirectory(unittest.TestCase):
    """The store says a live snapshot covers this shard and the file list does not.

    A `snap-<uuid>.dat` in a shard directory is usually leftovers, because this
    repository leaks. When the uuid is one the ANCHOR catalog still holds it is
    not leftovers: that snapshot covers this shard right now. If the current
    file list does not mention it, the two readings cannot both be true, and the
    file list is either not this shard's or not current.

    This is a second source for a fact the catalog also states, and it needs no
    `index_metadata_lookup`, so it reaches repositories the lookup cross-check
    cannot.
    """

    # Two snapshots live at the anchor, and `victim` is covered by only one of
    # them, so the other one's document has no business being in its directory.
    _S1 = {"victim": {0: ["__v1"]}, "wide": {0: ["__w1"]}}
    _S2 = {"victim": {0: ["__v1", "__v2"]}, "wide": {0: ["__w1"]}}
    _S3 = {"wide": {0: ["__w1", "__w2"]}}
    # One Elasticsearch operation per step: create, create, DELETE, create.
    # The delete is what puts anything in the manifest at all, without which
    # the leftovers assertion below would have nothing to find.
    HISTORY = [
        {"s1": _S1},
        {"s1": _S1, "s2": _S2},
        {"s2": _S2},
        {"s2": _S2, "s3": _S3},
    ]
    WHERE = repo.directory_of("victim", 0)

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="genchain-livedoc-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.built = repo.build(self.dir, self.HISTORY)

    def test_the_healthy_repository_reads_the_victim_shard(self):
        # The abuse case first, because the assertion below is satisfied by a
        # run that drops the shard for any reason at all.
        result = run_audit(LocalMirrorSource(self.dir))
        self.assertEqual({}, result.coverage.shards_dropped)

    def test_a_live_snapshot_document_the_file_list_omits_drops_the_shard(self):
        repo.overwrite(self.dir,
                       f"{self.WHERE}/snap-{repo.snapshot_uuid('s3')}.dat",
                       b"a live snapshot's shard document")
        result = run_audit(LocalMirrorSource(self.dir))
        doubt = result.coverage.shards_dropped.get(self.WHERE)
        self.assertEqual(shards.LIVE_SNAPSHOT_NOT_IN_FILE_LIST,
                         doubt.code if doubt else None)
        self.assertEqual(set(),
                         self.built.live_blob_keys & set(result.keys))

    def test_a_deleted_snapshots_document_lying_there_is_ordinary(self):
        # The other abuse case, and the important one. Every shard directory in
        # a leaking repository is full of documents belonging to snapshots that
        # are gone. A check that fired on those would drop every shard of every
        # repository this tool exists to audit.
        result = run_audit(LocalMirrorSource(self.dir))
        self.assertIn(f"{self.WHERE}/snap-{repo.snapshot_uuid('s1')}.dat",
                      result.keys)
        self.assertEqual({}, result.coverage.shards_dropped)


class ASnapshotTheAnchorStillHolds(unittest.TestCase):
    """It left the catalog at some step and it is here now. It is not garbage.

    Whether a snapshot left at some step and whether it is gone now are two
    different questions, and only the second decides what may be condemned.
    """

    _S1 = {"idx": {0: ["__a"]}}
    _KEEP = {"other": {0: ["__k"]}}
    _DOOMED = {"third": {0: ["__d"]}}
    # `s1` leaves at the first step and comes back at the second. `keep` is
    # there so the anchor is never an empty catalog, which refuses the run.
    # `doomed` leaves at the last step and stays gone, so this repository has a
    # real orphan in it: without one the healthy run produces an empty manifest
    # and cannot be told apart from a run that refused.
    HISTORY = [
        {"s1": _S1, "keep": _KEEP, "doomed": _DOOMED},
        {"keep": _KEEP, "doomed": _DOOMED},
        {"s1": _S1, "keep": _KEEP, "doomed": _DOOMED},
        {"s1": _S1, "keep": _KEEP},
    ]
    WHERE = repo.directory_of("idx", 0)

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="genchain-resurrected-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.built = repo.build(self.dir, self.HISTORY)
        self.result = run_audit(LocalMirrorSource(self.dir))

    def test_the_run_produces_a_manifest_rather_than_refusing(self):
        # The abuse case that matters here, and it took a neuter run to find.
        # Removing the rule does not make this tool name the live snapshot's
        # blobs: the contradiction tripwire notices that a key is both
        # condemned and live and refuses the whole run. So an empty manifest
        # satisfies every assertion below, and only checking that the run
        # SUCCEEDED tells the two apart.
        self.assertIsNone(self.result.coverage.refused)
        self.assertGreater(len(self.result.keys), 0)

    def test_the_run_saw_the_departure(self):
        # The other abuse case. If the chain never reports `s1` leaving, the
        # assertions below hold for a reason that has nothing to do with the
        # rule under test, and the guard is unmeasured.
        from generation_chain.derivation.chain import load_chain
        from generation_chain.derivation.garbage import delete_operations
        source = LocalMirrorSource(self.dir)
        chain = load_chain(source, source.list_keys())
        self.assertIn(repo.snapshot_uuid("s1"),
                      {o.snapshot_uuid for o in delete_operations(chain)})

    def test_nothing_it_names_is_condemned(self):
        self.assertNotIn(f"{self.WHERE}/__a", self.result.keys)

    def test_its_own_documents_are_not_condemned_either(self):
        # Named by uuid, so they need no file list, and that is exactly why the
        # rule has to be checked before any of those branches run.
        self.assertNotIn(f"snap-{repo.snapshot_uuid('s1')}.dat",
                         self.result.keys)
        self.assertNotIn(f"{self.WHERE}/snap-{repo.snapshot_uuid('s1')}.dat",
                         self.result.keys)

    def test_no_live_key_reaches_the_manifest(self):
        self.assertEqual(set(),
                         self.built.live_blob_keys & set(self.result.keys))


class ANameTwoSnapshotsHaveCarried(unittest.TestCase):
    """One name, two uuids, and no way to join the two views of it.

    A root catalog names a snapshot by uuid and a shard document names it by
    NAME, so a name reused after a delete leaves the derivation unable to say
    which of the two a file list belongs to. It attributes none of it.

    The cost is a real orphan left unnamed, and that is the direction this
    project prefers. The documents named by uuid are unambiguous and still
    appear, so the rule costs only what it has to.
    """

    # `s` is created, deleted, and created again. `keep` exists only so the
    # anchor catalog is never empty, which would refuse the run instead.
    HISTORY = [
        {"keep": {"other": {0: ["__k"]}},
         "s#first": {"idx": {0: ["__old", "__shared"]}}},
        {"keep": {"other": {0: ["__k"]}}},
        {"keep": {"other": {0: ["__k"]}},
         "s#second": {"idx": {0: ["__new", "__shared"]}}},
    ]
    WHERE = repo.directory_of("idx", 0)

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="genchain-reused-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.built = repo.build(self.dir, self.HISTORY)
        self.result = run_audit(LocalMirrorSource(self.dir))

    def test_the_shard_was_actually_read(self):
        # The abuse case, and it comes first because every assertion below is
        # satisfied by a run that dropped this shard and looked at nothing. The
        # retired test asserted only the absence, so the neuter sweep found the
        # guard unpinned: dropping the shard produced the same answer.
        self.assertEqual({}, self.result.coverage.shards_dropped)
        self.assertNotIn(self.WHERE, self.result.coverage.shards_retired)

    def test_no_file_list_is_attributed_to_the_reused_name(self):
        # `__old` really is garbage. It stays out anyway, because the only
        # route to it runs through a name a live snapshot also carries.
        self.assertNotIn(f"{self.WHERE}/__old", self.result.keys)

    def test_the_documents_named_by_uuid_are_still_condemned(self):
        # The guard costs only what it has to. A uuid in a blob name is
        # unambiguous however many snapshots have shared the NAME, so these
        # need no file list and stay in the manifest.
        self.assertIn(f"snap-{repo.snapshot_uuid('s#first')}.dat",
                      self.result.keys)

    def test_the_live_snapshots_own_blobs_are_untouched(self):
        self.assertEqual(set(),
                         self.built.live_blob_keys & set(self.result.keys))


class WriterUuidCollisions(unittest.TestCase):
    """A Lucene writer identity seen under two directories drops both.

    Measured on two captured Elasticsearch 9.5.2 repositories: overlap 0 across
    every cross-directory pairing, including two shards of ONE index. See
    `identity.writer_uuid_collisions` for the numbers and for the claim they
    refute.
    """

    def test_two_directories_claiming_one_writer_are_both_named(self):
        # There is no way to tell which of the two reads was the wrong one, so
        # neither directory is believed. Naming only one would be a guess.
        collisions = identity.writer_uuid_collisions({
            "indices/a/0": [_Doc({"w1", "w2"})],
            "indices/b/0": [_Doc({"w2", "w3"})],
            "indices/c/0": [_Doc({"w4"})]})
        self.assertEqual({"indices/a/0", "indices/b/0"}, set(collisions))

    def test_a_writer_uuid_in_one_directory_is_no_collision(self):
        # The abuse case. A check that fired on healthy data would drop every
        # shard of every repository, and coverage would go to zero.
        collisions = identity.writer_uuid_collisions({
            "indices/a/0": [_Doc({"w1"}), _Doc({"w1", "w2"})],
            "indices/b/0": [_Doc({"w3"})]})
        self.assertEqual({}, collisions)

    def test_documents_carrying_no_writer_uuid_collide_with_nothing(self):
        # An older segment carries no writer uuid, so an empty set is NO SIGNAL
        # rather than a claim of ownership. Reading absence as evidence here
        # would drop shards in a repository that pre-dates the field.
        collisions = identity.writer_uuid_collisions({
            "indices/a/0": [_Doc(set())], "indices/b/0": [_Doc(set())]})
        self.assertEqual({}, collisions)


class _Doc:
    def __init__(self, writers):
        self.writer_uuids = frozenset(writers)


if __name__ == "__main__":
    unittest.main()
