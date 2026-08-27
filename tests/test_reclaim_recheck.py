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

import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from generation_chain.reclaim import recheck


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
