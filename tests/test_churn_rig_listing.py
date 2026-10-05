"""The churn rig's own S3 listing, driven over loopback against tests/s3rig.py.

The rig lists a base path to count what failed deletes left behind, and
--purge-bucket deletes every key that listing returns. A listing that stops
early under-reports the leak, and a listing that returns a key outside the
base path hands another repository's object to the purge. These cases run
the rig's real signer and parser against a store that misbehaves the ways a
store does.
"""

import contextlib
import io
import pathlib
import sys
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import s3rig  # noqa: E402
import snapshot_churn_rig as rig  # noqa: E402

OURS = {"octest/index-%d" % i: b"x" for i in range(10)}
THEIRS = {"gcw/index-7": b"y", "scalerig/snap-z.dat": b"z"}


class PrefixIgnoringStore(s3rig.S3Rig):
    """Answers every listing as if no prefix had been asked for."""

    def _list(self, h, req, head_only):
        req.params["prefix"] = [""]
        return super()._list(h, req, head_only)


def client(store):
    return rig.S3(store.endpoint, s3rig.TEST_REGION, s3rig.TEST_ACCESS_KEY,
                  s3rig.TEST_SECRET_KEY, s3rig.TEST_BUCKET)


def list_with_deadline(s3, prefix, seconds=10):
    """Run s3.list in a thread so a listing that never ends fails the test."""
    outcome = {}

    def target():
        try:
            outcome["keys"] = s3.list(prefix)
        except Exception as error:  # noqa: BLE001 - the test inspects it
            outcome["error"] = error

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    outcome["finished"] = not worker.is_alive()
    return outcome


class RigListing(unittest.TestCase):

    def store(self, cls=s3rig.S3Rig, **faults):
        return cls("", page_size=3, objects=dict(OURS, **THEIRS), **faults)

    def test_an_honest_store_lists_every_page_under_the_base_path(self):
        # The leak count and the purge both read this list. If paging broke
        # on an honest store, every report past the first page would count
        # too few leaked blobs and a purge would leave them behind.
        with self.store() as store:
            keys = [k for k, _ in client(store).list("octest/")]
        self.assertEqual(sorted(keys), sorted(OURS))

    def test_a_page_without_is_truncated_is_refused_not_read_as_the_end(self):
        # A store that leaves IsTruncated out reads as "last page" to a
        # trusting client. The rig would report three objects where ten
        # remain, and the operator would believe a purge had finished.
        with self.store(omit_is_truncated=True) as store:
            with self.assertRaises(rig.EsError):
                client(store).list("octest/")

    def test_a_truncated_page_without_a_token_is_refused(self):
        # IsTruncated true with no token ends the listing early. The rest
        # of the base path would not exist as far as the leak count goes.
        with self.store(drop_token_after=1) as store:
            with self.assertRaises(rig.EsError):
                client(store).list("octest/")

    def test_a_store_that_ignores_list_type_2_is_refused(self):
        # A V1 store never reads the V2 continuation token. Without a
        # refusal the rig either crashes on the missing token or walks
        # page one forever.
        with self.store(answer_v1=True) as store:
            outcome = list_with_deadline(client(store), "octest/")
        self.assertTrue(outcome["finished"])
        self.assertIsInstance(outcome.get("error"), rig.EsError)

    def test_a_repeated_continuation_token_ends_in_a_refusal(self):
        # A store that hands back the same token walks a trusting client in
        # a loop that never ends, and the teardown Job holding the rig's
        # state file never finishes.
        with self.store(repeat_token_after=2) as store:
            outcome = list_with_deadline(client(store), "octest/")
        self.assertTrue(outcome["finished"])
        self.assertIsInstance(outcome.get("error"), rig.EsError)

    def test_a_key_outside_the_base_path_stops_the_purge_before_any_delete(
            self):
        # The bucket is shared with live repositories. A store that answers
        # a listing for octest/ with gcw/index-7 would hand that key to the
        # purge, and an object deleted from this store does not come back.
        with self.store(cls=PrefixIgnoringStore) as store:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(rig.EsError):
                    rig.clear_bucket(client(store), "octest", True)
            self.assertEqual(store.deleted, [])


if __name__ == "__main__":
    unittest.main()
