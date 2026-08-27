"""The safety invariant: every failure mode produces a SMALLER list.

This tool condemns on presence. A generation blob it cannot read is a delete
operation it cannot explain, and a shard document it cannot parse is a file
list it cannot attribute, so in both cases the blobs behind them must vanish
from the manifest rather than appear in it. The reachability sweeper has the
opposite exposure, where a read failure shrinks the live set and manufactures
orphans, and that asymmetry is the only reason this tool is worth having.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import genchain_fixtures as fx
from generation_chain import run_audit
from generation_chain.sources.local import LocalMirrorSource


# One snapshot deleted, its segment leaked, one snapshot surviving.
HISTORY = [
    {"s1": {"idx": ["__a", "__shared"]}},
    {"s1": {"idx": ["__a", "__shared"]}, "s2": {"idx": ["__b", "__shared"]}},
    {"s2": {"idx": ["__b", "__shared"]}},
]


def condemned(root):
    return {c.key for c in run_audit(LocalMirrorSource(root)).condemned}


class SafetyInvariant(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        fx.build_repository(self.root, HISTORY)
        self.baseline = condemned(self.root)

    def test_a_deleted_snapshot_leaves_its_own_segment_condemned(self):
        # Use case for the whole derivation. If this stops passing the tool
        # explains nothing, and an operator comparing manifests would read an
        # empty list as agreement with the reachability sweeper.
        self.assertIn("indices/iuuid-idx/0/__a", self.baseline)
        self.assertNotIn("indices/iuuid-idx/0/__shared", self.baseline)
        self.assertNotIn("indices/iuuid-idx/0/__b", self.baseline)

    def test_an_unreadable_generation_blob_shrinks_the_manifest(self):
        # Abuse case, and the one the tool exists to survive. A generation
        # blob that has been truncated in transit, written by a different
        # Elasticsearch version, or half-overwritten must remove the delete
        # operations it witnessed from the output. If this ever grows the
        # list, a corrupt read becomes a deletion order.
        fx.corrupt(self.root, "index-2")
        after = condemned(self.root)
        self.assertLess(len(after), len(self.baseline))
        self.assertTrue(after.issubset(self.baseline))


if __name__ == "__main__":
    unittest.main()
