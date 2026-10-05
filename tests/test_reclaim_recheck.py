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

import io
import json
import os
import struct
import sys
import time
import types
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import s3rig
from generation_chain.corroboration import ElasticsearchVeto
from generation_chain.reclaim import cli, recheck
from generation_chain.reclaim.manifest import Derivation, load_manifest
from generation_chain.reclaim.transport import TransportError
from test_reclaim_cli import (ReclaimCase, repository_keys, store,
                              write_manifest)


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

    def test_a_negative_bound_does_not_lift_the_limit(self):
        # Abuse case: only zero is the stated way to switch the check off. A
        # negative bound used to switch it off too, so a typo such as -3600
        # let a manifest derived years ago through, after a searchable
        # snapshot could have been mounted over its blobs. Neutered under
        # "only-zero-lifts-the-age-limit".
        for age in (31 * 365 * 86400, 60, -30):
            self.assertIsNotNone(recheck.staleness_problem(
                age_seconds=age, maximum=-1, path="m.tsv"))

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


class _ClusterAnswer:
    def __init__(self, body):
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self, size=-1):
        return self.body


def _cluster(mounted_index_uuid=None, unreachable=False):
    """An opener answering the veto's three reads the way 9.5.2 does.

    Shaped like the answers in test_generation_chain_corroboration.py: the
    repository's snapshot list, the mount settings, the in-flight status.
    """
    mounted = {}
    if mounted_index_uuid is not None:
        mounted["frozen-idx"] = {"settings": {
            "index.store.snapshot.snapshot_uuid": "uuid-s1",
            "index.store.snapshot.index_uuid": mounted_index_uuid,
            "index.store.snapshot.repository_name": "repo"}}
    answers = iter([{"snapshots": [{"snapshot": "s2", "uuid": "uuid-s2"}]},
                    mounted, {"snapshots": []}])

    def open_it(request, timeout=None, **kwargs):
        if unreachable:
            raise urllib.error.URLError("connection refused")
        return _ClusterAnswer(next(answers))
    return open_it


class ExecuteReChecksTheClusterBeforeDeleting(ReclaimCase):
    """The re-check wired through the command line, not the module alone.

    The tests above call `newly_protected` directly, and nothing proved that
    `--execute` hands it the manifest's keys or acts on what comes back. This
    is the last gate before a delete, so it is checked end to end against
    the same rig the other reclaim tests delete from.
    """

    PROTECTED = "indices/iuuid-mounted/0/__seg1"
    OTHER = "indices/iuuid-other/0/__seg2"

    def setUp(self):
        super().setUp()
        with open(self.credentials_path, "w", encoding="utf-8") as handle:
            json.dump({"s3": {"access_key_id": s3rig.TEST_ACCESS_KEY,
                              "secret_access_key": s3rig.TEST_SECRET_KEY},
                       "elasticsearch": {"username": "u", "password": "p"}},
                      handle)
        os.chmod(self.credentials_path, 0o600)
        write_manifest(self.manifest_path, [self.PROTECTED, self.OTHER])

    def execute(self, rig, opener, *cluster_args):
        # run_cli always adds --without-elasticsearch, and these tests name
        # a cluster instead, so the command line is built here.
        def veto_with_fake_cluster(**kwargs):
            return ElasticsearchVeto(opener=opener, **kwargs)
        manifest = load_manifest(self.manifest_path)
        args = ["--manifest", self.manifest_path, "--endpoint", rig.endpoint,
                "--region", s3rig.TEST_REGION, "--bucket", rig.bucket,
                "--credentials", self.credentials_path, "--execute",
                "--approve-digest", manifest.digest,
                "--approve-rows", str(len(manifest.keys)), *cluster_args]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli, "ElasticsearchVeto", veto_with_fake_cluster):
            code = cli.main(args, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_a_key_mounted_since_derivation_stops_the_run(self):
        # Abuse case: a searchable snapshot mounted over a manifest key
        # between deriving and executing. If the re-check result stopped
        # reaching the decision, this run would delete a blob the mounted
        # index reads from, and the index would fail later with nothing
        # tying the failure to the sweep. Neutered under
        # "a-re-checked-protection-refuses-the-run" and
        # "execute-hands-the-manifest-to-the-re-check".
        with store({self.PROTECTED: b"x", self.OTHER: b"y"}) as rig:
            code, _stdout, _stderr = self.execute(
                rig, _cluster(mounted_index_uuid="iuuid-mounted"),
                "--elasticsearch", "http://127.0.0.1:9200",
                "--es-repository", "repo")
            attempts = list(rig.batch_delete_attempts)
            remaining = repository_keys(rig)
        self.assertEqual(code, cli.EXIT_APPROVAL_REFUSED)
        self.assertEqual(attempts, [])
        self.assertEqual(remaining, {self.PROTECTED, self.OTHER})

    def test_an_unrelated_mount_lets_the_run_delete(self):
        # Use case: a cluster with mounts elsewhere must not block a manifest
        # none of them touch. A check that refused on any mount at all would
        # teach operators to reach for --without-elasticsearch.
        with store({self.PROTECTED: b"x", self.OTHER: b"y"}) as rig:
            code, _stdout, _stderr = self.execute(
                rig, _cluster(mounted_index_uuid="iuuid-elsewhere"),
                "--elasticsearch", "http://127.0.0.1:9200",
                "--es-repository", "repo")
            remaining = repository_keys(rig)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(remaining, set())

    def test_a_cluster_that_cannot_be_asked_stops_the_run(self):
        # Abuse case: a veto that could not be fetched is not a veto that
        # said yes. If the failure fell through to the delete, a network
        # blip at the wrong moment would skip the one re-check the operator
        # asked for.
        with store({self.PROTECTED: b"x", self.OTHER: b"y"}) as rig:
            code, _stdout, _stderr = self.execute(
                rig, _cluster(unreachable=True),
                "--elasticsearch", "http://127.0.0.1:9200",
                "--es-repository", "repo")
            attempts = list(rig.batch_delete_attempts)
        self.assertEqual(code, cli.EXIT_APPROVAL_REFUSED)
        self.assertEqual(attempts, [])

    def test_a_cluster_named_without_its_repository_is_a_usage_error(self):
        # Abuse case: without --es-repository the re-check has no snapshot
        # list to read, so it cannot have run. Treating that as a pass would
        # delete on a re-check that never happened.
        with store({self.PROTECTED: b"x", self.OTHER: b"y"}) as rig:
            code, _stdout, _stderr = self.execute(
                rig, _cluster(), "--elasticsearch", "http://127.0.0.1:9200")
            attempts = list(rig.batch_delete_attempts)
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(attempts, [])


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
