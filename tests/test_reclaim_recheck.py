"""The veto has to hold at the moment of deletion, not when the list was made.

The Elasticsearch veto protects blobs backing mounted searchable snapshots. It
ran when the manifest was derived and never again, and nothing bounded how old
a manifest could be when it was executed. So the sequence that removes live
data was: derive a manifest, mount a searchable snapshot, execute. Nothing in
`reclaim/` referenced Elasticsearch at all, and neither approval.py nor
manifest.py read the file's age.

That is a time-of-check gap. It is not the absence test, and it is the only
path left where this tool could remove a blob a running cluster still needs.
"""

import json
import os
import struct
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from generation_chain.reclaim import recheck
from generation_chain.reclaim.manifest import Derivation
from generation_chain.reclaim.transport import TransportError


class AStaleManifestIsRefused(unittest.TestCase):
    def test_a_manifest_older_than_the_bound_is_refused(self):
        problem = recheck.staleness_problem(age_seconds=7200, maximum=3600,
                                            path="m.tsv")
        self.assertIsNotNone(problem)
        self.assertIn("m.tsv", problem)

    def test_a_fresh_manifest_passes(self):
        self.assertIsNone(
            recheck.staleness_problem(age_seconds=60, maximum=3600,
                                      path="m.tsv"))

    def test_the_bound_can_be_lifted_deliberately(self):
        # Zero means "do not check", for an operator who has decided that
        # themselves. It has to be possible to say so, and it has to be an
        # explicit act rather than the default.
        self.assertIsNone(
            recheck.staleness_problem(age_seconds=999999, maximum=0,
                                      path="m.tsv"))

    def test_the_message_says_what_to_do(self):
        problem = recheck.staleness_problem(age_seconds=7200, maximum=3600,
                                            path="m.tsv")
        self.assertIn("derive", problem.lower())


class AManifestFromTheFutureIsRefused(unittest.TestCase):

    def test_a_derivation_time_well_ahead_of_this_clock_is_refused(self):
        # Abuse case: a record hours in the future gives a negative age,
        # which passes any limit forever. A wrong clock on the deriving host
        # or an edited record both look like this.
        problem = recheck.staleness_problem(
            age_seconds=-2 * recheck.CLOCK_SKEW_SECONDS, maximum=3600,
            path="m.tsv")
        self.assertIsNotNone(problem)
        self.assertIn("future", problem)

    def test_ordinary_skew_between_two_hosts_is_tolerated(self):
        # Use case: the audit runs in a pod and the delete on a jump host,
        # and their clocks differ by seconds. Refusing that would make every
        # fresh manifest need --max-manifest-age 0, which disables the check.
        self.assertIsNone(recheck.staleness_problem(
            age_seconds=-30, maximum=3600, path="m.tsv"))


def _catalog(uuid):
    document = {"min_version": "7.12.0", "snapshots": [], "indices": {},
                "index_metadata_identifiers": {}}
    if uuid is not None:
        document["uuid"] = uuid
    return json.dumps(document).encode("utf-8")


def _store(latest, catalogs):
    """A read callable over `index.latest` = `latest` and `catalogs`."""
    objects = {"index.latest": struct.pack(">q", latest)}
    objects.update({f"index-{n}": body for n, body in catalogs.items()})

    def read(key):
        if key not in objects:
            raise TransportError(f"404 for {key}")
        return objects[key]
    return read


class TheTargetMustBeTheRepositoryTheManifestNames(unittest.TestCase):

    derivation = Derivation(repository_uuid="repo-a", anchor_generation=5,
                            derived_at=0.0)

    def test_the_same_repository_at_the_anchor_or_later_passes(self):
        # Use case: the live repository moves on after a derivation, so its
        # generation is usually higher by execute time. Refusing that would
        # refuse almost every real run.
        for latest in (5, 9):
            self.assertIsNone(recheck.target_problem(
                self.derivation, _store(latest, {latest: _catalog("repo-a")})))

    def test_another_repository_is_refused(self):
        # Abuse case: the wrong --bucket or --prefix names a repository whose
        # keys match this manifest's layout. Neutered under
        # "the-target-must-carry-the-manifest-s-uuid".
        problem = recheck.target_problem(
            self.derivation, _store(7, {7: _catalog("repo-b")}))
        self.assertIsNotNone(problem)
        self.assertIn("repo-b", problem)

    def test_a_catalog_with_no_uuid_is_refused(self):
        # Abuse case: a catalog that states no uuid is no opinion, not a
        # match, the same rule the audit applies when it anchors.
        self.assertIsNotNone(recheck.target_problem(
            self.derivation, _store(7, {7: _catalog(None)})))

    def test_an_older_copy_of_the_same_repository_is_refused(self):
        # Abuse case: a copy taken before the deletes the manifest describes
        # still holds the snapshots those blobs belong to. Neutered under
        # "the-target-must-be-at-or-past-the-anchor".
        problem = recheck.target_problem(
            self.derivation, _store(4, {4: _catalog("repo-a")}))
        self.assertIsNotNone(problem)
        self.assertIn("generation 4", problem)

    def test_a_target_that_cannot_be_read_is_refused(self):
        # Abuse case: a 404 on index.latest, or on the catalog it names,
        # says nothing about which repository this is. Neutered under
        # "an-unreadable-target-is-refused".
        self.assertIsNotNone(recheck.target_problem(
            self.derivation, _store(6, {})))


class AKeyNowProtectedStopsTheRun(unittest.TestCase):
    """A mount that appeared after the manifest was written must stop it."""

    def _veto(self, index_uuids):
        return types.SimpleNamespace(index_uuids=frozenset(index_uuids),
                                     snapshot_uuids=frozenset())

    def test_a_key_under_a_newly_mounted_index_is_caught(self):
        keys = ("indices/AAAA/0/__seg1", "indices/BBBB/0/__seg2")
        now = recheck.newly_protected(keys, self._veto({"BBBB"}))
        self.assertEqual(now, ("indices/BBBB/0/__seg2",))

    def test_an_unrelated_mount_does_not_stop_the_run(self):
        keys = ("indices/AAAA/0/__seg1",)
        self.assertEqual(recheck.newly_protected(keys, self._veto({"ZZZZ"})),
                         ())

    def test_no_mounts_protects_nothing(self):
        keys = ("indices/AAAA/0/__seg1",)
        self.assertEqual(recheck.newly_protected(keys, self._veto(set())), ())

    def test_the_prefix_match_is_anchored_to_the_directory(self):
        # `indices/AAAA` must not protect `indices/AAAABBBB`. A loose prefix
        # here would silently widen protection, which is the safe direction,
        # but it would also make the refusal fire on runs it should not and
        # teach an operator to reach for the override.
        keys = ("indices/AAAABBBB/0/__seg1",)
        self.assertEqual(recheck.newly_protected(keys, self._veto({"AAAA"})),
                         ())


class TheOperatorMustChooseWhetherToRecheck(unittest.TestCase):
    def test_executing_without_either_flag_is_refused(self):
        problem = recheck.corroboration_choice_problem(
            elasticsearch=None, without=False)
        self.assertIsNotNone(problem)
        self.assertIn("--elasticsearch", problem)

    def test_naming_a_cluster_is_a_choice(self):
        self.assertIsNone(recheck.corroboration_choice_problem(
            elasticsearch="http://es:9200", without=False))

    def test_declining_deliberately_is_a_choice(self):
        # Someone reclaiming an orphaned repository has no cluster to ask.
        # Refusing them entirely would push them to a worse tool.
        self.assertIsNone(recheck.corroboration_choice_problem(
            elasticsearch=None, without=True))

    def test_asking_for_both_is_refused(self):
        problem = recheck.corroboration_choice_problem(
            elasticsearch="http://es:9200", without=True)
        self.assertIsNotNone(problem)


if __name__ == "__main__":
    unittest.main()
