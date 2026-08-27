"""The canary: does this tool ever name a blob a live snapshot still uses.

The suite this replaces had no such test. A reviewer removed BOTH of the
derivation's live-set protections and the tool named six live shared blobs
with every one of its 136 tests still green. A suite that cannot go red when
the tool condemns live data is not evidence about the only property that
matters, so this file is the first one written and the first one run.

Everything here is arranged so that a green result cannot be reached by the
tool going quiet. Each test asserts BOTH that the live blobs stayed out of the
manifest AND that a blob that really is garbage stayed in it. A run that
dropped the shard would satisfy the first and fail the second, which is what
stops the canary from passing vacuously the day a guard starts refusing
everything.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import genchain_repo as repo
from generation_chain import run_audit
from generation_chain.derivation.classification import LIVE
from generation_chain.sources.local import LocalMirrorSource

SHARED = "__shared"
ONLY_FIRST = "__only-in-the-deleted-one"
ONLY_SECOND = "__only-in-the-surviving-one"

# Two snapshots of one shard, the second sharing a segment with the first, and
# then the first is deleted. This is the ordinary state of any repository that
# takes incremental snapshots, which is every repository.
SHARING_HISTORY = [
    {"s1": {"idx": {0: [SHARED, ONLY_FIRST]}}},
    {"s1": {"idx": {0: [SHARED, ONLY_FIRST]}},
     "s2": {"idx": {0: [SHARED, ONLY_SECOND]}}},
    {"s2": {"idx": {0: [SHARED, ONLY_SECOND]}}},
]


class SharedSegmentsSurviveADelete(unittest.TestCase):
    """A segment two snapshots share must outlive the deletion of one of them.

    If this file goes red, the tool is naming keys that a snapshot still in the
    catalog references, and running its manifest through a delete would destroy
    a live snapshot. There is no worse failure available to this project.
    """

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="genchain-canary-")
        self.addCleanup(_rmtree, self.dir)
        self.built = repo.build(self.dir, SHARING_HISTORY)
        self.result = run_audit(LocalMirrorSource(self.dir))
        self.where = repo.directory_of("idx", 0)

    def test_the_shared_segment_is_not_in_the_manifest(self):
        # The whole safety model in one assertion. `__shared` belongs to the
        # deleted snapshot AND to the surviving one, so the delete that removed
        # the first must not have orphaned it. A tool that names this key hands
        # an operator a command that destroys the surviving snapshot.
        self.assertNotIn(f"{self.where}/{SHARED}", self.result.keys)

    def test_the_shared_segment_is_classified_live(self):
        # The manifest and the dispositions are two views of one answer. A key
        # absent from the manifest but filed as anything other than live means
        # the two views disagree, and an operator who trusts the dispositions
        # over the manifest would still delete it.
        self.assertEqual(LIVE, _disposition(self.result,
                                            f"{self.where}/{SHARED}"))

    def test_the_segment_only_the_deleted_snapshot_named_is_in_the_manifest(self):
        # The abuse case for both tests above. Without this, a run that dropped
        # the shard for any reason would satisfy them by naming nothing, and the
        # canary would report green while measuring nothing at all.
        self.assertIn(f"{self.where}/{ONLY_FIRST}", self.result.keys)

    def test_the_surviving_snapshots_own_segment_is_not_in_the_manifest(self):
        # A blob the deleted snapshot never named must not be reachable through
        # the operation that deleted it. This catches an attribution that walks
        # the union of a shard document instead of one snapshot's file list.
        self.assertNotIn(f"{self.where}/{ONLY_SECOND}", self.result.keys)

    def test_no_key_the_fixture_declares_live_reaches_the_manifest(self):
        # The general form, stated against the fixture's own declaration rather
        # than against anything the tool derived. The two statements are
        # independent, so agreement between them is evidence.
        self.assertEqual(set(),
                         self.built.live_blob_keys & set(self.result.keys))

    def test_the_shard_was_not_dropped(self):
        # Every assertion above is satisfiable by refusing to look. This one
        # says the run actually read the shard, so the others measured the
        # derivation rather than its silence.
        self.assertEqual({}, self.result.coverage.shards_dropped)


class TheManifestIsASubsetOfElasticsearchsOwnAnswer(unittest.TestCase):
    """The algorithm restated as an assertion.

    Elasticsearch's own package documentation says a delete collects the `__`
    blobs in a shard directory that the current BlobStoreIndexShardSnapshots
    does not reference. That set difference is computed here from the fixture,
    independently of the tool, and the tool's segment rows must fall inside it.

    A manifest row outside that set is a blob Elasticsearch itself would have
    kept, which is the definition of naming live data.
    """

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="genchain-setdiff-")
        self.addCleanup(_rmtree, self.dir)
        self.built = repo.build(self.dir, SHARING_HISTORY)
        self.result = run_audit(LocalMirrorSource(self.dir))

    def test_every_condemned_segment_is_one_elasticsearch_would_collect(self):
        where = repo.directory_of("idx", 0)
        present = {key for key in self.built.keys
                   if key.startswith(where + "/")
                   and key.rpartition("/")[2].startswith("__")}
        live = {f"{where}/{SHARED}", f"{where}/{ONLY_SECOND}"}
        collectable = present - live
        named = {key for key in self.result.keys
                 if key.rpartition("/")[2].startswith("__")}
        self.assertTrue(named.issubset(collectable),
                        sorted(named - collectable))

    def test_the_tool_names_fewer_than_elasticsearch_would_or_the_same(self):
        # The tool condemns on PRESENCE: it names a blob only when it can point
        # at the delete operation that orphaned it. That makes its answer a
        # subset of the set difference rather than equal to it, and the report
        # has to be able to say so honestly rather than claim parity.
        where = repo.directory_of("idx", 0)
        named = {key for key in self.result.keys
                 if key.rpartition("/")[2].startswith("__")}
        self.assertEqual({f"{where}/{ONLY_FIRST}"}, named)


def _disposition(result, key: str) -> str:
    for placement in result.classification:
        if placement.key == key:
            return placement.disposition
    raise AssertionError(f"{key} was given no disposition at all")


def _rmtree(path: str) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
