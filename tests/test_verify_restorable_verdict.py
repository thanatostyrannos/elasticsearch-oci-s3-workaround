"""verify_restorable.py's verdict, driven against the loopback stand-in.

Exit 0 tells the caller the repository restores and it may carry on
deleting. Every case here is a cluster answer that has to turn into exit 1
instead, plus the answers that legitimately mean there is nothing to check
yet.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_verify_restorable_cleanup import (  # noqa: E402
    FakeElasticsearch, clean_restore, run_script)

LISTING = "/_snapshot/repo/_all"


def snapshot(name, state="SUCCESS", indices=("data-1",), start=1):
    return {"snapshot": name, "state": state, "indices": list(indices),
            "start_time_in_millis": start}


def restored_from(server):
    """The snapshot path segment each restore request named."""
    return [path.split("/")[3] for method, path in server.targets
            if "/_restore" in path]


class TheVerdictComesFromTheRestore(unittest.TestCase):

    def test_zero_restored_documents_fail(self):
        # Eight deleted data blobs once restored an index that answered 200
        # and held nothing. Reading zero as success is that failure.
        server = FakeElasticsearch(clean_restore(), count={"count": 0})
        code, _ = run_script(server)
        self.assertEqual(code, 1)

    def test_a_partial_snapshot_is_never_the_one_restored(self):
        # A PARTIAL snapshot has failed shards and restores less than it
        # names. Restoring it instead of the newest SUCCESS proves nothing
        # about the repository.
        server = FakeElasticsearch(clean_restore())
        server.answers[LISTING] = (200, {"snapshots": [
            snapshot("good", start=1), snapshot("half", "PARTIAL", start=2)]})
        run_script(server)
        self.assertEqual(restored_from(server), ["good"])

    def test_a_frozen_mount_is_never_the_index_restored(self):
        # A partial- index keeps its data in the object store, so restoring
        # it counts nothing local and cannot show that bytes survived.
        server = FakeElasticsearch(clean_restore())
        server.answers[LISTING] = (200, {"snapshots": [
            snapshot("mounts-only", indices=("partial-x",), start=2),
            snapshot("real", indices=("data-1",), start=1)]})
        run_script(server)
        self.assertEqual(restored_from(server), ["real"])

    def test_a_red_cluster_whose_broken_index_is_in_the_repository_fails(
            self):
        # An unassigned shard of an index this repository holds is damage
        # the deletes may have caused. Continuing to the restore of some
        # other index would report INTACT over it.
        server = FakeElasticsearch(clean_restore())
        server.answers["/_cluster/health"] = (200, {"status": "red"})
        server.answers["/_cat/shards"] = (200, [
            {"index": "data-1", "state": "UNASSIGNED"}])
        code, _ = run_script(server)
        self.assertEqual(code, 1)

    def test_a_snapshot_in_an_impossible_state_fails(self):
        # FAILED or INCOMPATIBLE in the listing says the repository is not
        # what Elasticsearch wrote. Skipping it would let the run pass.
        server = FakeElasticsearch(clean_restore())
        server.answers[LISTING] = (200, {"snapshots": [
            snapshot("good"), snapshot("bad", "FAILED", start=2)]})
        code, _ = run_script(server)
        self.assertEqual(code, 1)


class AnAnswerItCannotUseIsAFailure(unittest.TestCase):
    """An error answer is not an empty repository."""

    def test_a_cluster_that_refuses_the_credential_fails(self):
        # An empty or stale password draws 401 on every call. The listing
        # then had no snapshots in it, and the script said "not a failure"
        # and exited 0 without checking anything.
        server = FakeElasticsearch(clean_restore())
        for path in ("/_cluster/health", LISTING):
            server.answers[path] = (401, {"status": 401})
        code, _ = run_script(server)
        self.assertEqual(code, 1)

    def test_a_missing_repository_fails(self):
        # A repository the deletes broke badly enough to unregister answers
        # 404. That is the worst outcome, not an empty one.
        server = FakeElasticsearch(clean_restore())
        server.answers[LISTING] = (404, {"error": {
            "type": "repository_missing_exception"}, "status": 404})
        code, _ = run_script(server)
        self.assertEqual(code, 1)

    def test_an_unavailable_listing_fails(self):
        server = FakeElasticsearch(clean_restore())
        server.answers[LISTING] = (503, {"status": 503})
        code, _ = run_script(server)
        self.assertEqual(code, 1)

    def test_a_listing_without_its_snapshot_list_fails(self):
        # A 200 that carries no list is not a repository with no snapshots.
        server = FakeElasticsearch(clean_restore())
        server.answers[LISTING] = (200, {})
        code, _ = run_script(server)
        self.assertEqual(code, 1)

    def test_an_explicitly_empty_repository_is_not_a_failure(self):
        # The counterpart. A repository nothing has been written to yet is a
        # normal state on a new rig, and failing it would stop every first
        # pass.
        server = FakeElasticsearch(clean_restore())
        server.answers[LISTING] = (200, {"snapshots": []})
        code, _ = run_script(server)
        self.assertEqual(code, 0)


class AnUnreadableSnapshotIsNotSkipped(unittest.TestCase):

    def test_a_snapshot_elasticsearch_cannot_load_fails_the_check(self):
        # ignore_unavailable=true drops a snapshot whose metadata a reclaim
        # deleted from the listing, and the check then restored an older,
        # healthy one and printed INTACT. The stand-in answers the way
        # Elasticsearch does: only a listing that ignores unavailable
        # snapshots succeeds.
        def listing(target):
            if "ignore_unavailable=true" in target:
                return 200, {"snapshots": [snapshot("older")]}
            return 500, {"error": {"type": "snapshot_missing_exception"},
                         "status": 500}

        server = FakeElasticsearch(clean_restore())
        server.answers[LISTING] = listing
        code, _ = run_script(server)
        self.assertEqual(code, 1)

if __name__ == "__main__":
    unittest.main()
