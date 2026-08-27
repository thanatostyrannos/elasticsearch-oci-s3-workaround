"""The shard reads are the expensive ones, so they are the ones to overlap.

The package has had read-ahead, a bounded thread pool and a --concurrency flag
since before this test existed. Exactly one caller used them: chain.py warmed
the root generations. The shard documents, which are read once per directory
PER GENERATION and are therefore the dominant cost of any real run, were
fetched one at a time with the pool sitting idle.

Measured on a live Oracle repository before this: cycle time went from 2.0
minutes at 20 generations to 7.1 minutes at 250, tracking generation count
almost linearly, while eight workers waited.

The property that makes warming safe is that it cannot change an answer. It is
a hint: the same keys are read, in the same order, and `fetch` returns exactly
what that key produced. The equivalence test below is what holds that, and it
matters more than the speed.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import genchain_repo as repo
from generation_chain import run_audit
from generation_chain.sources import prepared
from generation_chain.sources.local import LocalMirrorSource

SHARED, ONLY_FIRST, ONLY_SECOND = "__shared", "__onlyfirst", "__onlysecond"
HISTORY = [
    {"s1": {"idx": {0: [SHARED, ONLY_FIRST]}}},
    {"s1": {"idx": {0: [SHARED, ONLY_FIRST]}},
     "s2": {"idx": {0: [SHARED, ONLY_SECOND]}}},
    {"s2": {"idx": {0: [SHARED, ONLY_SECOND]}}},
]


class TheShardReadsAreWarmed(unittest.TestCase):
    """Recorded at the seam: what shards.py asks to warm, before it reads."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="readahead-shards-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        repo.build(self.dir, HISTORY)

    def _run_recording_hints(self):
        from generation_chain.derivation import shards
        warmed = []
        original = shards.hint

        def recording(source, keys):
            keys = list(keys)
            warmed.append(keys)
            return original(source, keys)

        shards.hint = recording
        try:
            result = run_audit(prepared(LocalMirrorSource(self.dir)))
        finally:
            shards.hint = original
        return warmed, result

    def test_the_shard_documents_are_warmed(self):
        warmed, _ = self._run_recording_hints()
        keys = {k for batch in warmed for k in batch}
        shard_docs = {k for k in keys
                      if k.startswith("indices/") and "/index-" in k}
        self.assertTrue(shard_docs,
                        "no shard document was warmed; the expensive reads "
                        "are still cold. warmed=%r" % (warmed,))

    def test_the_snapshot_documents_are_warmed(self):
        warmed, _ = self._run_recording_hints()
        keys = {k for batch in warmed for k in batch}
        self.assertTrue({k for k in keys if k.startswith("snap-")},
                        "warmed=%r" % (warmed,))

    def test_nothing_is_warmed_that_is_not_a_key(self):
        # A hint wired to the wrong expression would warm empty strings or
        # None and satisfy a looser test by doing nothing useful.
        warmed, _ = self._run_recording_hints()
        for batch in warmed:
            for key in batch:
                self.assertIsInstance(key, str)
                self.assertTrue(key)

    def test_warming_does_not_change_the_answer(self):
        # The property everything rests on. A hint changes latency, not
        # answers: the same keys are read and fetch returns what that key
        # produced.
        _, warm = self._run_recording_hints()
        cold = run_audit(LocalMirrorSource(self.dir))
        self.assertEqual(sorted(warm.keys), sorted(cold.keys))


if __name__ == "__main__":
    unittest.main()
