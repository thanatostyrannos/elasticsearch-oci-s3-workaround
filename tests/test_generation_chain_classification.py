"""Every key in the store gets a disposition, not just the condemned ones.

Elasticsearch's own delete removes the stale root generations and the stale
shard generation documents along with the segments. This tool will not name
either for deletion, because both are the evidence its derivation reads, and
a tool that condemned its own inputs would be usable exactly once.

That is a structural silence, so it has to be visible. A flat orphan manifest
would leave a large, fixed fraction of every real deletion sitting in the
"cannot explain" bucket that an operator uses to spot trouble, and the signal
would drown in it. Sorting every key into a disposition keeps that bucket
meaning one thing: a snapshot object this run could not place.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import genchain_fixtures as fx
from generation_chain import run_audit
from generation_chain.derivation.classification import (EVIDENCE, LIVE,
                                                        ORPHANED, OUTSIDE_MODEL,
                                                        UNEXPLAINED)
from generation_chain.sources.local import LocalMirrorSource

HISTORY = [
    {"s1": {"idx": ["__a", "__shared"]}},
    {"s1": {"idx": ["__a", "__shared"]}, "s2": {"idx": ["__b", "__shared"]}},
    {"s2": {"idx": ["__b", "__shared"]}},
]


class Dispositions(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-class-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        fx.build_repository(self.root, HISTORY)

    def audit(self):
        return run_audit(LocalMirrorSource(self.root))

    def placed(self):
        return {r.key: r.disposition for r in self.audit().classification}

    def test_every_key_in_the_store_gets_exactly_one_disposition(self):
        # An operator compares this manifest against the reachability
        # sweeper's by identity. A key the tool never mentions at all is
        # indistinguishable from a key it decided was live, and those two
        # lead to opposite actions.
        placed = self.placed()
        self.assertEqual(sorted(placed), fx.read_keys(self.root))

    def test_stale_generations_are_withheld_as_evidence_not_condemned(self):
        # Elasticsearch removed these; this tool will not name them, because
        # the derivation reads them to learn what a delete removed. If they
        # ever move to ORPHANED, a run that acted on the manifest would
        # destroy the chain and no later run could explain anything.
        placed = self.placed()
        self.assertEqual(placed["index-0"], EVIDENCE)
        self.assertEqual(placed["index-1"], EVIDENCE)
        self.assertEqual(placed["indices/iuuid-idx/0/index-sg-idx-0-0"], EVIDENCE)
        self.assertEqual(placed["indices/iuuid-idx/0/index-sg-idx-0-1"], EVIDENCE)

    def test_the_current_state_is_live_and_the_leaked_segment_is_orphaned(self):
        # The use case. Getting either half wrong turns the manifest into
        # either a list that misses the leak or a list that names live data.
        placed = self.placed()
        self.assertEqual(placed["index-2"], LIVE)
        self.assertEqual(placed["index.latest"], LIVE)
        self.assertEqual(placed["indices/iuuid-idx/0/index-sg-idx-0-2"], LIVE)
        self.assertEqual(placed["indices/iuuid-idx/0/__b"], LIVE)
        self.assertEqual(placed["indices/iuuid-idx/0/__shared"], LIVE)
        self.assertEqual(placed["indices/iuuid-idx/0/__a"], ORPHANED)
        self.assertEqual(placed["snap-uuid-s1.dat"], ORPHANED)

    def test_an_unreadable_shard_moves_its_keys_out_of_orphaned(self):
        # Abuse case for the invariant, stated in the language of the
        # dispositions. A read failure has to move keys from ORPHANED to
        # UNEXPLAINED and never the other way. This is what makes the
        # unexplained bucket the thing an operator watches: it grows exactly
        # when the tool saw less.
        before = self.placed()
        fx.corrupt(self.root, "indices/iuuid-idx/0/index-sg-idx-0-1")
        after = self.placed()
        self.assertEqual(before["indices/iuuid-idx/0/__a"], ORPHANED)
        self.assertEqual(after["indices/iuuid-idx/0/__a"], UNEXPLAINED)
        orphaned_before = {k for k, v in before.items() if v == ORPHANED}
        orphaned_after = {k for k, v in after.items() if v == ORPHANED}
        self.assertTrue(orphaned_after.issubset(orphaned_before))

    def test_objects_the_tool_does_not_model_are_named_as_such(self):
        # A repository holds blobs that are not part of the snapshot graph at
        # all, and the verification directory is the common one. Filing those
        # under "cannot explain" would put permanent noise in the bucket an
        # operator reads as a warning.
        with open(os.path.join(self.root, "incompatible-snapshots"), "wb") as fh:
            fh.write(b"{}")
        os.makedirs(os.path.join(self.root, "tests-abcd"), exist_ok=True)
        with open(os.path.join(self.root, "tests-abcd", "master.dat"), "wb") as fh:
            fh.write(b"x")
        placed = self.placed()
        self.assertEqual(placed["tests-abcd/master.dat"], OUTSIDE_MODEL)
        self.assertEqual(placed["incompatible-snapshots"], OUTSIDE_MODEL)


if __name__ == "__main__":
    unittest.main()
