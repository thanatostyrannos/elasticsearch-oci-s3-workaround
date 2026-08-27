"""Whether this run read all of what it claims to have read.

The live set for a shard is one object, so a live set that came up SHORT is
invisible from the inside: a truncated file list, a catalog missing an index
and a `shard_generations` array one entry short all parse and all look like a
smaller repository. Elasticsearch writes a second statement of the same fact,
at snapshot time, in a different part of the code, into a different object.
`snap-<uuid>.dat` declares which indices the snapshot holds, how many shards in
total, how many shards each index has and how many bytes each came to.

The rule these tests pin: if the traversal does not account for what a snapshot
declares, that snapshot's shards contribute nothing.

The boundary, stated because it is easy to oversell: the declaration lives in
the same bucket. It does not defend against a tamper that adjusts the catalog
and the snapshot document together. It defends against every failure this
package has actually had, which is a short list, a missing entry, a silently
dropped value and a partial read.
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
from generation_chain.derivation import shards
from generation_chain.derivation.classification import UNEXPLAINED
from generation_chain.sources.local import LocalMirrorSource

# One index with two shards, so a per-index shard count is a number that can be
# wrong in both directions, and one index that leaves the catalog entirely.
HISTORY = [
    {"s1": {"wide": {0: ["__w0a"], 1: ["__w1a"]}, "gone": {0: ["__g1"]}}},
    {"s1": {"wide": {0: ["__w0a"], 1: ["__w1a"]}, "gone": {0: ["__g1"]}},
     "s2": {"wide": {0: ["__w0a", "__w0b"], 1: ["__w1a"]}}},
    {"s2": {"wide": {0: ["__w0a", "__w0b"], 1: ["__w1a"]}}},
]
# `s2` declares more shards of `wide` than this run can read, which is what a
# shard failing part way through a snapshot leaves behind: a declared extent
# the file lists do not cover. The index has ONE shard on disk so that the
# shortfall is the only thing wrong; giving it two and letting `s2` touch one
# of them trips the per-shard snapshot-name check first, and the test would
# then pass for a reason that has nothing to do with partial snapshots.
PARTIAL_HISTORY = [
    {"s1": {"wide": {0: ["__w0a"]}}},
    {"s1": {"wide": {0: ["__w0a"]}}, "s2": {"wide": {0: ["__w0a", "__w0b"]}}},
]
WIDE_0 = repo.directory_of("wide", 0)
WIDE_1 = repo.directory_of("wide", 1)
GONE_0 = repo.directory_of("gone", 0)


class Healthy(unittest.TestCase):

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="genchain-extent-ok-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.built = repo.build(self.dir, HISTORY)
        self.result = run_audit(LocalMirrorSource(self.dir))

    def test_both_shards_of_the_wide_index_are_read(self):
        # The baseline every drop test below is measured against. If this stops
        # holding, those tests pass by describing a tool that reads nothing.
        self.assertNotIn(WIDE_0, self.result.coverage.shards_dropped)
        self.assertNotIn(WIDE_1, self.result.coverage.shards_dropped)

    def test_the_orphan_of_the_deleted_snapshot_is_named(self):
        self.assertIn(f"{GONE_0}/__g1", _unexplained(self.result))
        self.assertEqual(set(),
                         self.built.live_blob_keys & set(self.result.keys))


class ADeclarationTheTraversalDoesNotMeet(unittest.TestCase):

    def _audit(self, defects):
        self.dir = tempfile.mkdtemp(prefix="genchain-extent-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.built = repo.build(self.dir, HISTORY, defects=defects)
        return run_audit(LocalMirrorSource(self.dir))

    def _code(self, result, directory):
        doubt = result.coverage.shards_dropped.get(directory)
        return doubt.code if doubt is not None else None

    def test_a_declared_shard_count_above_what_was_read_drops_the_index(self):
        # A catalog whose `shard_generations` array is one entry short reads as
        # an index with fewer shards, and the shard it forgot is never
        # traversed. The snapshot's own document still says how many there are.
        result = self._audit(repo.Defects(
            declared_shard_count={("s2", "wide"): 3}))
        self.assertEqual(shards.EXTENT_SHARD_COUNT, self._code(result, WIDE_0))
        self.assertEqual(shards.EXTENT_SHARD_COUNT, self._code(result, WIDE_1))

    def test_a_declared_shard_count_below_what_was_read_drops_the_index(self):
        # The other direction, and it matters as much. A count that is too low
        # means this run read a directory the snapshot says is not part of the
        # index, so one of the two readings is about a different repository.
        result = self._audit(repo.Defects(
            declared_shard_count={("s2", "wide"): 1}))
        self.assertEqual(shards.EXTENT_SHARD_COUNT, self._code(result, WIDE_0))

    def test_a_declared_total_that_disagrees_drops_the_snapshots_shards(self):
        # `total_shards` is an aggregate the per-index counts cannot express: a
        # snapshot spanning indices the catalog does not connect to it still
        # has to add up.
        result = self._audit(repo.Defects(declared_total_shards={"s2": 5}))
        self.assertEqual(shards.EXTENT_TOTAL_SHARDS, self._code(result, WIDE_0))

    def test_a_snapshot_document_nobody_could_read_drops_its_shards(self):
        # Unreadable means this run verified NOTHING about that snapshot's
        # extent. Carrying on would measure the traversal against a declaration
        # nobody read, which is an absence standing in for a check.
        result = self._audit(repo.Defects(missing_snapshot_documents=["s2"]))
        self.assertEqual(shards.EXTENT_UNREADABLE, self._code(result, WIDE_0))

    def test_a_snapshot_declaring_no_shard_count_drops_that_index(self):
        # An absent declaration is not a statement that the traversal was
        # complete. This is the same rule as everywhere else in the package,
        # applied to the one input whose whole job is to be a second opinion.
        self.dir = tempfile.mkdtemp(prefix="genchain-extent-nodetail-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.built = repo.build(self.dir, HISTORY)
        _strip_index_details(self.dir, repo.snapshot_uuid("s2"))
        result = run_audit(LocalMirrorSource(self.dir))
        self.assertEqual(shards.EXTENT_NOT_DECLARED, self._code(result, WIDE_0))

    def test_a_declared_index_the_run_never_traversed_drops_the_shards(self):
        # The catalog is provably short of what the snapshot recorded. Every
        # shard of every index that snapshot touches contributes nothing,
        # because a live set assembled from a catalog missing an entry reads
        # that index as having nothing alive in it.
        self.dir = tempfile.mkdtemp(prefix="genchain-extent-index-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.built = repo.build(self.dir, HISTORY)
        _declare_an_extra_index(self.dir, repo.snapshot_uuid("s2"), "vanished")
        result = run_audit(LocalMirrorSource(self.dir))
        self.assertEqual(shards.EXTENT_INDEX_NOT_TRAVERSED,
                         self._code(result, WIDE_0))
        self.assertEqual(set(),
                         self.built.live_blob_keys & set(result.keys))

    def test_a_partial_snapshot_is_not_measured_against_its_own_extent(self):
        # A snapshot Elasticsearch itself reports as partial legitimately does
        # not cover what it set out to, so the shortfall says nothing about
        # this run's reading. Without this the tool would drop every shard of
        # every repository that ever had a shard fail during a snapshot.
        #
        # The shortfall has to be REAL for this to measure anything. An earlier
        # version of this test used a partial snapshot whose extent added up
        # anyway, so removing the check changed nothing and the neuter sweep
        # reported it unpinned. Here `s2` declares two shards of `wide` and
        # only ever took one, which is exactly what a shard failing mid-
        # snapshot leaves behind.
        self.dir = tempfile.mkdtemp(prefix="genchain-partial-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.built = repo.build(self.dir, PARTIAL_HISTORY, defects=repo.Defects(
            partial_snapshots=["s2"],
            declared_shard_count={("s2", "wide"): 2}))
        result = run_audit(LocalMirrorSource(self.dir))
        self.assertEqual({}, result.coverage.shards_dropped)

    def test_the_same_shortfall_on_a_COMPLETE_snapshot_does_drop_it(self):
        # The abuse case, and the one that proves the test above is measuring
        # the `successful_shards` reading rather than the shard count. Same
        # shortfall, same fixture, only the partial flag removed.
        self.dir = tempfile.mkdtemp(prefix="genchain-not-partial-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.built = repo.build(self.dir, PARTIAL_HISTORY, defects=repo.Defects(
            declared_shard_count={("s2", "wide"): 2}))
        result = run_audit(LocalMirrorSource(self.dir))
        self.assertEqual(shards.EXTENT_SHARD_COUNT, self._code(result, WIDE_0))

    def test_no_drop_ever_puts_a_live_key_in_the_manifest(self):
        # The property that matters across all of the above. A drop is supposed
        # to make the answer shorter, and a drop that made it longer would be
        # the failure mode the retired derivation had, where what the run
        # needed to read depended on what it had managed to read.
        for defects in (repo.Defects(declared_shard_count={("s2", "wide"): 3}),
                        repo.Defects(declared_total_shards={"s2": 5}),
                        repo.Defects(missing_snapshot_documents=["s2"]),
                        repo.Defects(partial_snapshots=["s2"])):
            result = self._audit(defects)
            self.assertEqual(set(),
                             self.built.live_blob_keys & set(result.keys),
                             defects)


class ADroppedShardContributesNoIndexMetadata(unittest.TestCase):
    """An index this run could not read through contributes nothing at all.

    This one is CONSERVATISM rather than a proof, and saying so matters. A
    metadata blob's liveness is decided by the anchor catalog's two maps, not
    by anything in a shard directory, so a dropped shard is not evidence that
    the metadata reading is wrong.

    It is kept because the metadata path is the one that has actually produced
    counterexamples in this package, three of them, and because what it costs
    is a leaked metadata blob rather than a deleted one. What it buys is that
    an index this run could not read through contributes NOTHING, which is one
    rule instead of two.
    """

    # `s1` and `s2` write different metadata for `wide`, so `s1` leaving really
    # does orphan a blob `s2` does not use.
    HISTORY = [
        {"s1": {"wide": {0: ["__w0a"], 1: ["__w1a"]}}},
        {"s1": {"wide": {0: ["__w0a"], 1: ["__w1a"]}},
         "s2": {"wide": {0: ["__w0a", "__w0b"], 1: ["__w1a"]}}},
        {"s2": {"wide": {0: ["__w0a", "__w0b"], 1: ["__w1a"]}}},
    ]

    def _audit(self, **extra):
        self.dir = tempfile.mkdtemp(prefix="genchain-md-drop-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.built = repo.build(self.dir, self.HISTORY, defects=repo.Defects(
            per_snapshot_index_metadata=True, **extra))
        return run_audit(LocalMirrorSource(self.dir))

    def test_a_read_index_gives_up_its_orphaned_metadata(self):
        # The abuse case, and it has to come first. If the deleted snapshot's
        # metadata blob is never condemned even when everything reads, the test
        # below passes without measuring the rule.
        result = self._audit()
        self.assertEqual({}, result.coverage.shards_dropped)
        self.assertIn(repo.metadata_key("wide", "s1", per_snapshot=True),
                      result.keys)

    def test_an_index_with_a_dropped_shard_gives_up_nothing(self):
        result = self._audit(declared_shard_count={("s2", "wide"): 3})
        self.assertIn(WIDE_0, result.coverage.shards_dropped)
        self.assertNotIn(repo.metadata_key("wide", "s1", per_snapshot=True),
                         result.keys)


class AbsenceIsNeverEvidence(unittest.TestCase):

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="genchain-absence-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.built = repo.build(self.dir, HISTORY)

    def test_an_index_the_anchor_no_longer_lists_yields_no_condemned_segment(self):
        # `gone` left the catalog, so no live snapshot references it and its
        # whole directory really is garbage. This run still names none of its
        # segments, because it established no live set there to measure them
        # against, and an empty live set condemns everything in a directory.
        result = run_audit(LocalMirrorSource(self.dir))
        self.assertEqual(UNEXPLAINED, _disposition(result, f"{GONE_0}/__g1"))
        self.assertNotIn(f"{GONE_0}/__g1", result.keys)

    def test_a_shard_the_catalog_names_no_generation_for_is_dropped(self):
        # A null in `shard_generations`, or a shard index past the end of the
        # array, is the catalog stating NO OPINION. Reading it as an empty live
        # set condemns the directory.
        _blank_shard_generation(self.dir, 2, "wide", 1)
        result = run_audit(LocalMirrorSource(self.dir))
        doubt = result.coverage.shards_dropped.get(WIDE_1)
        self.assertEqual(shards.NO_SHARD_GENERATION,
                         doubt.code if doubt else None)
        self.assertEqual(set(),
                         self.built.live_blob_keys & set(result.keys))

    def test_a_store_that_cannot_answer_is_not_a_store_that_said_no(self):
        # The one measured place where this tool's report was WRONG rather than
        # conservative. Folding "could not answer" into "does not hold" made
        # about 31 of 30,938 keys vanish from a manifest while coverage still
        # claimed 100%. The key still leaves the manifest, and the report now
        # says it left.
        key = f"snap-{repo.snapshot_uuid('s1')}.dat"
        clean = run_audit(LocalMirrorSource(self.dir))
        self.assertIn(key, clean.keys)

        result = run_audit(repo.FaultySource(
            self.dir, [repo.Fault(unanswerable=[key])]))
        self.assertIn(key, result.coverage.existence_unanswered)
        self.assertNotIn(key, result.keys)

    def test_a_healthy_run_reports_nothing_unanswered(self):
        # The abuse case for the test above. A field that is always populated
        # tells an operator nothing, and one that is never populated hides the
        # failure it exists to surface.
        result = run_audit(LocalMirrorSource(self.dir))
        self.assertEqual((), result.coverage.existence_unanswered)


class TheLiveSetForIndexMetadata(unittest.TestCase):
    """It is complete or it does not exist. There is no partial answer.

    Index metadata edges are assembled from TWO maps written in different parts
    of the catalog, `index_metadata_lookup` on each snapshot and
    `index_metadata_identifiers` at the root. Assembling anything from a
    partial input is how a live set comes up short, and this is the one place
    in the package where the live set is assembled rather than read.
    """

    def _generation(self, lookup, identifiers):
        from generation_chain.model import IndexEntry, RootGeneration, SnapshotRef
        return RootGeneration(
            generation=1, repository_uuid="u",
            snapshots={"uuid-live": SnapshotRef(
                uuid="uuid-live", name="live", metadata_lookup=lookup)},
            indices={"iuuid-a": IndexEntry(
                name="a", uuid="iuuid-a", snapshot_uuids=("uuid-live",),
                shard_generations=("g",))},
            index_metadata_identifiers=identifiers)

    def test_a_lookup_that_all_resolves_gives_the_live_set(self):
        # The abuse case first. A function that returned None for everything
        # would satisfy the test below while making the tool blind to every
        # metadata blob in every repository.
        from generation_chain.derivation.garbage import live_metadata_blobs
        live = live_metadata_blobs(
            self._generation({"iuuid-a": "lookup-a"}, {"lookup-a": "md-a"}), [])
        self.assertEqual({"iuuid-a": {"md-a"}}, live)

    def test_one_unresolvable_lookup_voids_the_whole_live_set(self):
        # Not a best effort over the part that resolved. A live snapshot whose
        # metadata this run cannot place is a reason to claim nothing about any
        # metadata, because the blob it could not place may be the one another
        # snapshot's delete would otherwise have condemned.
        from generation_chain.derivation.garbage import live_metadata_blobs
        self.assertIsNone(live_metadata_blobs(
            self._generation({"iuuid-a": "lookup-a"}, {}), []))

    def test_a_repository_whose_metadata_live_set_is_void_condemns_no_metadata(self):
        # The wiring, through `run_audit`, so the rule above is not merely true
        # of a function nobody calls. With the live set void, not one index
        # metadata blob reaches the manifest, including ones that really are
        # orphaned. That lost coverage is the price of the rule.
        self.dir = tempfile.mkdtemp(prefix="genchain-md-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        repo.build(self.dir, HISTORY)
        clean = run_audit(LocalMirrorSource(self.dir))
        self.assertTrue([k for k in clean.keys if "/meta-" in k])

        _unmap_a_lookup(self.dir, 2)
        result = run_audit(LocalMirrorSource(self.dir))
        self.assertEqual([], [k for k in result.keys if "/meta-" in k])


def _unmap_a_lookup(root: str, generation: int) -> None:
    """Leave a live snapshot's lookup value with nothing to resolve to."""
    import json
    key = f"index-{generation}"
    document = json.loads(repo.read(root, key).decode("utf-8"))
    document["index_metadata_identifiers"] = {}
    repo.overwrite(root, key,
                   json.dumps(document, sort_keys=True).encode("utf-8"))


def _disposition(result, key: str) -> str:
    for placement in result.classification:
        if placement.key == key:
            return placement.disposition
    raise AssertionError(f"{key} was given no disposition at all")


def _unexplained(result):
    return {p.key for p in result.classification
            if p.disposition == UNEXPLAINED}


def _strip_index_details(root: str, uuid: str) -> None:
    """Rewrite one snapshot document without its per-index detail map."""
    import json
    from generation_chain.formats.codec import unwrap
    key = f"snap-{uuid}.dat"
    body = unwrap(repo.read(root, key))["snapshot"]
    body.pop("index_details", None)
    repo.overwrite(root, key, repo.codec_wrap(
        json.dumps({"snapshot": body}, sort_keys=True).encode("utf-8"),
        codec_name="snapshot"))


def _declare_an_extra_index(root: str, uuid: str, index_name: str) -> None:
    """Add one index to a snapshot's declared extent that the catalog omits."""
    import json
    from generation_chain.formats.codec import unwrap
    key = f"snap-{uuid}.dat"
    body = unwrap(repo.read(root, key))["snapshot"]
    body["indices"] = sorted(list(body["indices"]) + [index_name])
    body.setdefault("index_details", {})[index_name] = {
        "shard_count": 1, "size_in_bytes": 42, "max_segments_per_shard": 1}
    body["total_shards"] = body["total_shards"] + 1
    body["successful_shards"] = body["total_shards"]
    repo.overwrite(root, key, repo.codec_wrap(
        json.dumps({"snapshot": body}, sort_keys=True).encode("utf-8"),
        codec_name="snapshot"))


def _blank_shard_generation(root: str, generation: int, index: str,
                            shard: int) -> None:
    """Set one shard's generation id to null, which is Elasticsearch's own
    way of saying it has no opinion about that shard."""
    import json
    key = f"index-{generation}"
    document = json.loads(repo.read(root, key).decode("utf-8"))
    for entry in document["indices"].values():
        if entry["id"] == repo.index_uuid(index):
            entry["shard_generations"][shard] = None
    repo.overwrite(root, key,
                   json.dumps(document, sort_keys=True).encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
