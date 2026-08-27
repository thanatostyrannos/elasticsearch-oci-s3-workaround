"""Which generation this run treats as current, and what that costs when wrong.

Elasticsearch's blobstore package documentation gives the order plainly: find
the most recent RepositoryData by LISTING every `index-` blob and selecting the
highest N, and read `index.latest` only if the listing fails. The retired
derivation had that backwards. A repository left by an ordinary crash between
writing `index-N+1` and updating `index.latest` made it name two live keys on
the rig, with no store misbehaving and nothing tampered with.

So the crash case is the first test in this file, and it asserts on live keys
rather than on which number the run picked. A tool that anchors correctly for
the wrong reason still has to not destroy data.
"""

from __future__ import annotations

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
from generation_chain.derivation.chain import BY_INDEX_LATEST, BY_LISTING
from generation_chain.sources.local import LocalMirrorSource

# Three generations. The third adds a snapshot that brings a NEW segment, which
# is the segment a run anchored on generation 1 would call garbage.
NEW_IN_THE_LAST_GENERATION = "__arrived-with-the-newest-snapshot"
HISTORY = [
    {"s1": {"idx": {0: ["__a", "__shared"]}}},
    {"s1": {"idx": {0: ["__a", "__shared"]}},
     "s2": {"idx": {0: ["__shared", "__b"]}}},
    {"s1": {"idx": {0: ["__a", "__shared"]}},
     "s2": {"idx": {0: ["__shared", "__b"]}},
     "s3": {"idx": {0: ["__shared", NEW_IN_THE_LAST_GENERATION]}}},
]


class Crashed(unittest.TestCase):
    """index.latest lags the listing, which is what a crash leaves behind.

    Elasticsearch writes `index-N+1` and then updates `index.latest`. A process
    killed between the two leaves a repository where the listing is ahead. This
    is not corruption and not tampering; it is the ordinary result of a restart
    at the wrong moment, and any operator can produce it.
    """

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="genchain-crash-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.built = repo.build(self.dir, HISTORY,
                                defects=repo.Defects(latest_lags_by=1))
        self.result = run_audit(LocalMirrorSource(self.dir))
        self.where = repo.directory_of("idx", 0)

    def test_the_segment_only_the_newest_snapshot_brought_is_not_condemned(self):
        # The whole point. Anchored on generation 1 this blob belongs to no
        # snapshot the catalog knows, so it reads as garbage. It belongs to a
        # snapshot that exists and would be destroyed with it.
        self.assertNotIn(f"{self.where}/{NEW_IN_THE_LAST_GENERATION}",
                         self.result.keys)

    def test_no_live_key_at_all_is_condemned(self):
        # The general form against the fixture's own declaration of what is
        # live, so the assertion above cannot pass by naming a different live
        # key instead.
        self.assertEqual(set(),
                         self.built.live_blob_keys & set(self.result.keys))

    def test_the_run_anchors_on_the_generation_the_listing_shows(self):
        self.assertEqual(2, self.result.coverage.current_generation)

    def test_the_run_records_which_input_it_anchored_on(self):
        # An operator reading the report has to be able to tell an ordinary run
        # from one that overrode the repository's own pointer.
        self.assertEqual(BY_LISTING, self.result.coverage.anchored_by)

    def test_the_run_reports_what_index_latest_said(self):
        # The disagreement is a fact the report carries rather than one an
        # operator has to infer. It is also the signature of the crash, so a
        # site seeing it repeatedly has a real problem to chase.
        self.assertEqual(1, self.result.coverage.latest_generation)

    def test_the_run_still_produces_a_manifest(self):
        # The abuse case. Every assertion above is satisfied by a run that
        # refused, and a guard that turns a routine crash into a refusal is a
        # guard operators learn to bypass.
        self.assertIsNone(self.result.coverage.refused)


class AStaleAnchorNamesLiveData(unittest.TestCase):
    """The reproduction, not the argument.

    Anchoring on `index.latest` instead of on the listing is wrong because
    Elasticsearch's own documentation says so, and that alone would be reason
    enough. This class is the measurement behind it: two histories the fixture
    language can express in which a run anchored one generation low names a
    blob that a snapshot in the true current catalog still references.

    Both were found by searching the shapes rather than by reasoning about
    them, after a first attempt at this test passed against the retired
    derivation because its history had no delete below the anchor and so could
    not reach the failing region at all.

    The mechanism is the same in both. A blob a deleted snapshot named is still
    on disk, because this repository leaks. A later snapshot references it
    again. Anchored low, the run measures that blob against a live set written
    before the later snapshot existed, and the blob is missing from it.
    """

    # A snapshot deleted, then a later snapshot that references a blob it had
    # named. The blob never left the store, because the delete leaked.
    DELETE_THEN_REFERENCE_AGAIN = [
        {"s1": {"i": {0: ["__a", "__sh"]}}},
        {"s1": {"i": {0: ["__a", "__sh"]}}, "s2": {"i": {0: ["__sh"]}}},
        {"s2": {"i": {0: ["__sh"]}}},
        {"s2": {"i": {0: ["__sh"]}}, "s3": {"i": {0: ["__sh", "__a"]}}},
    ]
    # An index that leaves the catalog and comes back in a later snapshot.
    AN_INDEX_THAT_RETURNS = [
        {"s1": {"i": {0: ["__a"]}}, "s0": {"j": {0: ["__j"]}}},
        {"s1": {"i": {0: ["__a"]}}, "s0": {"j": {0: ["__j"]}},
         "s2": {"j": {0: ["__j"]}}},
        {"s2": {"j": {0: ["__j"]}}},
        {"s2": {"j": {0: ["__j"]}}, "s3": {"i": {0: ["__a"]}, "j": {0: ["__j"]}}},
    ]

    def _run(self, history, lag):
        directory = tempfile.mkdtemp(prefix="genchain-stale-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        built = repo.build(directory, history,
                           defects=repo.Defects(latest_lags_by=lag))
        result = run_audit(LocalMirrorSource(directory))
        return built, result

    def test_a_lagging_pointer_costs_no_live_data_when_a_blob_returns(self):
        # Measured against the retired derivation, which named
        # `indices/iuuid-i/0/__a` here. That blob belongs to a snapshot in the
        # true current catalog, so its manifest destroyed live data.
        built, result = self._run(self.DELETE_THEN_REFERENCE_AGAIN, 1)
        self.assertEqual(set(), built.live_blob_keys & set(result.keys))

    def test_a_lagging_pointer_costs_no_live_data_when_an_index_returns(self):
        # The second shape the search found, same key, different cause.
        built, result = self._run(self.AN_INDEX_THAT_RETURNS, 1)
        self.assertEqual(set(), built.live_blob_keys & set(result.keys))

    def test_the_lag_does_not_change_the_answer_at_all(self):
        # The stronger statement, and the one that says the anchoring is right
        # rather than merely safe. `index.latest` is a pointer that can lag by
        # any amount without changing what is true about the repository, so a
        # run that reads the listing must produce the SAME manifest at every
        # lag. The retired derivation produced three different manifests here.
        for history in (self.DELETE_THEN_REFERENCE_AGAIN,
                        self.AN_INDEX_THAT_RETURNS):
            answers = {lag: set(self._run(history, lag)[1].keys)
                       for lag in (0, 1, 2)}
            self.assertEqual(answers[0], answers[1], history[0])
            self.assertEqual(answers[0], answers[2], history[0])
            # The abuse case. Three identical EMPTY manifests would satisfy the
            # two assertions above while measuring nothing.
            self.assertGreater(len(answers[0]), 0)


class HealthyRepository(unittest.TestCase):

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="genchain-anchor-ok-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        repo.build(self.dir, HISTORY)

    def test_agreement_is_reported_as_agreement(self):
        # When the two inputs agree there is nothing to override, and the
        # report must not suggest the tool second-guessed the repository.
        coverage = run_audit(LocalMirrorSource(self.dir)).coverage
        self.assertEqual(BY_INDEX_LATEST, coverage.anchored_by)
        self.assertEqual(2, coverage.current_generation)
        self.assertEqual(2, coverage.latest_generation)


class ACoTenantsGenerationBlob(unittest.TestCase):
    """A higher-numbered blob belonging to somebody else must not be the anchor.

    Anchoring on the highest is Elasticsearch's rule inside ONE repository. A
    bucket shared with another repository can hold higher numbers that say
    nothing about ours, so the anchor is the highest generation carrying OUR
    uuid, and the uuid comes from the generation `index.latest` names.
    """

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="genchain-cotenant-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        repo.build(self.dir, HISTORY)

    def _write_generation_nine(self, uuid: str, snapshots=()) -> None:
        with open(os.path.join(self.dir, "index-9"), "wb") as handle:
            handle.write(json.dumps({
                "min_version": "7.12.0", "uuid": uuid,
                "snapshots": list(snapshots), "indices": {},
                "index_metadata_identifiers": {}}).encode("utf-8"))

    def test_a_foreign_generation_above_the_anchor_is_rejected(self):
        self._write_generation_nine("somebody-elses-repository")
        coverage = run_audit(LocalMirrorSource(self.dir)).coverage
        self.assertEqual(2, coverage.current_generation)
        self.assertIn(9, coverage.generations_rejected)
        self.assertNotIn(9, coverage.generations_usable)

    def test_our_own_generation_above_the_anchor_with_an_empty_catalog_refuses(self):
        # The abuse case for the test above, and it models the state where
        # anchoring on the highest is at its most dangerous. A catalog naming no
        # live snapshots says every blob in the repository is garbage, which is
        # the largest manifest this tool could produce off one document. It
        # produces none instead.
        self._write_generation_nine("repo-uuid-aaaa")
        result = run_audit(LocalMirrorSource(self.dir))
        self.assertEqual([], result.condemned)
        self.assertIsNotNone(result.coverage.refused)

    def test_an_unreadable_generation_above_the_anchor_refuses(self):
        # Unreadable means the uuid is unknown, so the blob might be ours, and
        # if it is ours it is the current generation. Anchoring below a
        # generation that might be current is exactly the crash defect, so the
        # run explains nothing rather than quietly using the lower number.
        with open(os.path.join(self.dir, "index-9"), "wb") as handle:
            handle.write(b"not a RepositoryData document at all")
        result = run_audit(LocalMirrorSource(self.dir))
        self.assertEqual([], result.condemned)
        self.assertIsNotNone(result.coverage.refused)
        self.assertFalse(result.coverage.refusal_is_transient)


class TheIndexLatestPointerItself(unittest.TestCase):

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="genchain-latest-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        repo.build(self.dir, HISTORY)

    def test_a_pointer_to_a_generation_no_blob_backs_refuses(self):
        # `index.latest` is where the repository uuid comes from, so a pointer
        # nothing backs leaves the run unable to say which generation blobs are
        # even ours.
        with open(os.path.join(self.dir, "index.latest"), "wb") as handle:
            handle.write(struct.pack(">q", 99))
        result = run_audit(LocalMirrorSource(self.dir))
        self.assertEqual([], result.condemned)
        self.assertIsNotNone(result.coverage.refused)

    def test_a_read_failure_is_marked_retryable_and_a_format_failure_is_not(self):
        # A scheduled job derives success from the exit code and retries on it.
        # A store that answered 503 and a repository whose pointer is malformed
        # must not look alike: retrying the first is right, and retrying the
        # second burns the backoff to reach the same answer.
        os.remove(os.path.join(self.dir, "index.latest"))
        self.assertTrue(
            run_audit(LocalMirrorSource(self.dir)).coverage.refusal_is_transient)

        repo.overwrite(self.dir, "index.latest", b"\x00\x01\x02")
        self.assertFalse(
            run_audit(LocalMirrorSource(self.dir)).coverage.refusal_is_transient)


if __name__ == "__main__":
    unittest.main()
