"""Guards on the current catalog, which everything else is measured against.

The generation `index.latest` names is the anchor of the whole run. It decides
which snapshots are alive, which shard documents hold the live file lists, and
therefore which blobs can be condemned at all. A misreading anywhere else in
the chain costs coverage; a misreading here costs data, so this is where the
refusals are strictest.
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
from generation_chain.sources.local import LocalMirrorSource

HISTORY = [
    {"s1": {"idx": ["__a", "__shared"]}},
    {"s1": {"idx": ["__a", "__shared"]}, "s2": {"idx": ["__b", "__shared"]}},
    {"s2": {"idx": ["__b", "__shared"]}},
]


def audit(root):
    return run_audit(LocalMirrorSource(root))


def condemned(root):
    return {c.key for c in audit(root).condemned}


def rewrite_generation(root, gen, mutate):
    path = os.path.join(root, f"index-{gen}")
    with open(path) as fh:
        doc = json.load(fh)
    mutate(doc)
    with open(path, "w") as fh:
        json.dump(doc, fh)


class CurrentCatalog(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        fx.build_repository(self.root, HISTORY)
        self.baseline = condemned(self.root)

    def test_an_empty_current_catalog_refuses_the_run(self):
        # A current generation naming zero live snapshots is the one input
        # that makes every blob in the repository condemnable at once, so it
        # is also the input where a single misread costs the most. The tool
        # explains nothing there rather than producing its largest possible
        # manifest off one document.
        rewrite_generation(self.root, 2, lambda d: (d.update(snapshots=[], indices={})))
        result = audit(self.root)
        self.assertEqual(result.condemned, [])
        self.assertIsNotNone(result.coverage.refused)

    def test_a_catalog_that_omits_an_index_its_snapshots_use_is_refused(self):
        # The snapshots array and the indices map are written from the same
        # state, so a live snapshot referencing an index the map does not list
        # means one of the two was decoded wrongly. Reading on would measure a
        # file list against a live set built from half a document.
        def drop_index(doc):
            doc["indices"] = {}
        rewrite_generation(self.root, 1, drop_index)
        result = audit(self.root)
        self.assertIn(1, result.coverage.generations_rejected)
        self.assertTrue(set(c.key for c in result.condemned).issubset(self.baseline))

    def test_a_current_document_that_is_not_this_shard_s_drops_the_shard(self):
        # The live set comes from the current shard document, and nothing in a
        # BlobStoreIndexShardSnapshots names its own shard or its own
        # generation, so a store that answers with another object succeeds and
        # hands back a live set that is far too small. The one thing that can
        # be checked is the catalog: the document has to account for every
        # snapshot the current generation says references this index. Without
        # this, one wrong object is enough to condemn live data.
        current = os.path.join(self.root, "indices/iuuid-idx/0/index-sg-idx-0-2")
        foreign = {"files": [{"name": "__q", "physical_name": "_q", "length": 1}],
                   "snapshots": {"stranger": {"files": ["__q"]}}}
        with open(current, "wb") as fh:
            fh.write(fx.codec_wrap(json.dumps(foreign).encode()))
        result = audit(self.root)
        self.assertIn("indices/iuuid-idx/0", result.coverage.shards_dropped)
        self.assertTrue({c.key for c in result.condemned}.issubset(self.baseline))


if __name__ == "__main__":
    unittest.main()
