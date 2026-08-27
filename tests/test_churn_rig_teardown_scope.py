"""Teardown must delete only the indices this harness created.

Teardown resolves ``*<prefix>*`` and deletes what comes back. That selection is
the last thing standing between a prefix typed at a shell and an index nobody
meant to touch. ``check_prefix_free`` guards the run path, but teardown accepts
a prefix, proceeds with no state file after a warning, and has no preflight of
its own, so nothing else is in front of it.
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import snapshot_churn_rig as rig


def resolved(*names):
    return {"indices": [{"name": n} for n in names]}


class TeardownIndexScope(unittest.TestCase):

    def test_takes_the_backing_indices_of_its_own_data_stream(self):
        # The data stream's backing indices are the only indices the harness
        # creates. If the predicate stops matching them, teardown leaves behind
        # the indices it made, and the next run is refused by check_prefix_free
        # with nothing to do but clean up by hand.
        got = rig.teardown_index_scope(
            resolved(".ds-churnrig-stream-2026.08.26-000001",
                     ".ds-churnrig-stream-2026.08.26-000002"),
            "churnrig-stream")
        self.assertEqual(len(got), 2)

    def test_takes_the_ilm_frozen_mounts_of_those_indices(self):
        # ILM prefixes partial- or restored- onto the backing index name when
        # it mounts a searchable snapshot, and leaves the rest intact. Those
        # are ours. Missing them leaves mounted indices pinning snapshots the
        # operator believes are gone.
        got = rig.teardown_index_scope(
            resolved("partial-.ds-churnrig-stream-2026.08.26-000001",
                     "restored-.ds-churnrig-stream-2026.08.26-000001"),
            "churnrig-stream")
        self.assertEqual(len(got), 2)

    def test_leaves_indices_that_only_share_the_prefix(self):
        # This is the one that matters. Every name below is real and lives in
        # the lab this harness runs against. --prefix metrics resolves
        # *metrics* and returns metrics-sys, which the harness did not create.
        # Deleting any of these destroys data whose recovery path runs through
        # the store this project exists because that store loses deletes.
        for prefix, foreign in (("metrics", "metrics-sys"),
                                ("logs", "logs-app"),
                                ("frozen", "frozen-metrics"),
                                ("restored", "restored-logs-app"),
                                ("restored", "restored-metrics-sys")):
            with self.subTest(prefix=prefix, foreign=foreign):
                got = rig.teardown_index_scope(resolved(foreign),
                                               prefix + "-stream")
                self.assertEqual(got, [])


class TeardownPreconditions(unittest.TestCase):

    def test_refuses_to_derive_every_name_from_the_prefix(self):
        # Without a state file, teardown's repository is <prefix>-repo and its
        # policies are <prefix>-slm and <prefix>-ilm. Each of these prefixes
        # passes the --prefix validator and names a live repository in the
        # lab: gcw-repo holds 2 snapshots, s3c3-repo 2, scalerig-repo 11.
        # Teardown would delete those snapshots and unregister the repository.
        # Deleting snapshots is the operation that strands objects against a
        # store that drops batch deletes, which is why this project exists.
        for prefix in ("gcw", "s3c3", "scalerig"):
            with self.subTest(prefix=prefix):
                reason = rig.derive_refusal(None, False, rig.names(prefix))
                self.assertIsNotNone(reason)
                # The operator has to be able to check the names before
                # deciding, so the refusal has to show them.
                self.assertIn(prefix + "-repo", reason)

    def test_allows_a_teardown_the_run_recorded(self):
        # A state file is the harness's own record of what it created. That is
        # the case teardown is for.
        self.assertIsNone(
            rig.derive_refusal({"names": {}}, False, rig.names("churnrig")))

    def test_allows_derivation_once_the_operator_opts_in(self):
        # A lost state file is a real situation. The opt-in exists so it stays
        # recoverable, after the operator has read the names.
        self.assertIsNone(rig.derive_refusal(None, True, rig.names("churnrig")))


class PurgeScope(unittest.TestCase):

    def test_refuses_a_bucket_path_guessed_from_the_prefix(self):
        # These are base paths of live repositories in the lab and all are
        # valid prefixes. A purge scoped by a guess deletes their objects, and
        # objects deleted out of this store do not come back.
        for guessed in ("gcw", "scalerig", "s3c3", "rv1based", "rv2stale"):
            with self.subTest(base_path=guessed):
                self.assertIsNotNone(rig.purge_refusal(None, False, guessed))

    def test_allows_a_path_the_run_recorded(self):
        self.assertIsNone(
            rig.purge_refusal({"base_path": "churnrig"}, False, "churnrig"))

    def test_allows_a_path_the_operator_stated(self):
        # Passing --base-path is the operator naming the path out loud, which
        # is the difference between a stated scope and an inherited one.
        self.assertIsNone(rig.purge_refusal(None, True, "churnrig"))


class TheDataStreamNameIsConfigurable(unittest.TestCase):
    """A rig has to be able to write into a name the operator chose.

    Deriving every name from one prefix is fine for a lab and wrong for a
    cluster where the index name is already decided by a convention the rig does
    not get to pick. The override changes the name and nothing else: the
    teardown scope is computed from the resolved name, so it narrows with the
    name rather than widening.
    """

    def test_the_default_is_unchanged(self):
        # Every existing invocation has to keep meaning what it meant.
        self.assertEqual(rig.names("octest")["data_stream"], "octest-stream")

    def test_an_explicit_name_is_used_verbatim(self):
        n = rig.names("octest", data_stream="team-metrics-test")
        self.assertEqual(n["data_stream"], "team-metrics-test")

    def test_everything_else_still_comes_from_the_prefix(self):
        # The override renames one thing. A repository or policy that quietly
        # followed the data stream name would collide with a second rig using
        # the same stream name in a different bucket.
        n = rig.names("octest", data_stream="team-metrics-test")
        self.assertEqual(n["repo"], "octest-repo")
        self.assertEqual(n["ilm"], "octest-ilm")
        self.assertEqual(n["slm"], "octest-slm")
        self.assertEqual(n["template"], "octest-template")
        self.assertEqual(n["snap_prefix"], "octest-snap-")

    def test_teardown_scope_follows_the_chosen_name(self):
        # The guard keys off the resolved data stream, so a custom name must
        # take its own backing indices and nothing else.
        resolved = {"indices": [
            {"name": ".ds-team-metrics-test-2026.08.26-000001"},
            {"name": ".ds-octest-stream-2026.08.26-000001"},
            {"name": "team-metrics-test-lookalike"},
        ]}
        taken = rig.teardown_index_scope(resolved, "team-metrics-test")
        self.assertEqual(taken, [".ds-team-metrics-test-2026.08.26-000001"])

    def test_a_lookalike_is_still_left_alone(self):
        # The rule that an index merely starting with the name belongs to
        # someone else has to survive the override, or the override becomes a
        # way to widen the blast radius.
        resolved = {"indices": [{"name": "team-metrics-test"},
                                {"name": "team-metrics-test-prod-000001"}]}
        self.assertEqual(rig.teardown_index_scope(resolved, "team-metrics-test"), [])
