"""The derivation against a repository a real Elasticsearch actually wrote.

Fixtures agree with whoever wrote them. This file is the check that the reader
agrees with Elasticsearch: the captured 9.5.2 repository in
tests/fixtures/real-es952-repo.tar.gz carries three root generations, two
snapshots, and the leak this project exists for, and it is written in SMILE
rather than the JSON a hand-built fixture reaches for first.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import genchain_repo as repo
from generation_chain import run_audit
from generation_chain.formats.shard_snapshots import parse_shard_snapshots
from generation_chain.sources.local import LocalMirrorSource

GUARDS_SHARD = "indices/KMsiARacSXSgCnGLMZ191w/0"
GENERATION_1_DOCUMENT = f"{GUARDS_SHARD}/index-z0fHPDfwTjOuUvRF5b0mTA"


class RealRepository(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-real-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        repo.extract_fixture("real-es952-repo.tar.gz", self.dir)

    def read(self, key):
        with open(os.path.join(self.dir, key), "rb") as fh:
            return fh.read()

    def test_a_real_shard_document_yields_its_snapshots_and_their_blobs(self):
        # Elasticsearch writes these documents in SMILE inside a Lucene codec
        # frame. A reader that only handles JSON parses nothing here, every
        # shard gets dropped, and the tool reports an empty manifest against a
        # real repository while looking healthy. This is the test that says
        # the format work is real rather than fixture-shaped.
        document = parse_shard_snapshots(
            self.read(GENERATION_1_DOCUMENT), GENERATION_1_DOCUMENT)
        self.assertEqual(set(document.by_snapshot_name), {"v9-snap-1", "v9-snap-2"})
        self.assertEqual(document.by_snapshot_name["v9-snap-1"],
                         frozenset({"__eNrx2s4fSuCkM28PCPz2jw",
                                    "__YIojeK5gTw2xhDNjnycQNA"}))
        # The `v__` entries in the same document hold their content inline and
        # have no object behind them. Naming one in a manifest sends an
        # operator hunting for a key the store never had.
        self.assertTrue(all(not name.startswith("v__")
                            for name in document.blob_names))

    def test_the_deleted_snapshot_of_the_captured_repository_is_reconstructed(self):
        # The capture holds generations 0, 1 and 2, and v9-snap-1 was deleted
        # between 1 and 2 while its blobs stayed. If this stops passing, the
        # derivation no longer reproduces an operation that really happened on
        # a real cluster, whatever the synthetic fixtures say.
        result = run_audit(LocalMirrorSource(self.dir))
        self.assertIsNone(result.coverage.refused)
        self.assertEqual(result.coverage.current_generation, 2)
        self.assertEqual(result.coverage.generations_usable, (0, 1, 2))
        self.assertEqual(result.coverage.transitions_explained, 2)
        self.assertIn("snap-0BL_pm0STciV-fQF_5PZAg.dat", result.keys)
        self.assertIn(f"{GUARDS_SHARD}/__eNrx2s4fSuCkM28PCPz2jw", result.keys)
        for key in result.keys:
            self.assertTrue(os.path.exists(os.path.join(self.dir, key)), key)

    def test_the_surviving_snapshot_keeps_every_blob_it_names(self):
        # Abuse case for the derivation as a whole: v9-snap-2 is still live in
        # generation 2, so nothing it references may appear. A manifest naming
        # a live snapshot's segment is the data-loss direction, and it is the
        # direction nothing in this project measures by name today.
        result = run_audit(LocalMirrorSource(self.dir))
        live = parse_shard_snapshots(
            self.read(f"{GUARDS_SHARD}/index-8jXfoe0aRo-iYI4WaAryJA"),
            "current")
        for blob in live.blob_names:
            self.assertNotIn(f"{GUARDS_SHARD}/{blob}", result.keys)


class ATwoShardIndexARealClusterWrote(unittest.TestCase):
    """The measurement the writer-uuid guard rests on, pinned.

    `identity.writer_uuid_collisions` treats a Lucene writer identity seen
    under two shard directories as a positive contradiction. That is only sound
    while Elasticsearch keeps writer uuids disjoint across directories, and the
    case that mattered most was untested until this capture: two shards of ONE
    index, where the snapshot-name set is identical and the two directories are
    as similar as they ever get.

    This is not a test of Elasticsearch. It is the evidence the guard cites. A
    later capture that contradicts it has to fail loudly here rather than leave
    a guard standing on a measurement that stopped being true.
    """

    INDEX = "indices/DVAX-VlHR-mL2VgBNTUIRw"
    SHARD_0 = f"{INDEX}/0/index-B5TbIl1ITMeHB1ydDtab8A"
    SHARD_1 = f"{INDEX}/1/index-akxkEK50RUeiOdb58WAk4A"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-twoshard-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        repo.extract_fixture("real-es952-twoshard-repo.tar.gz", self.dir)

    def _document(self, key):
        with open(os.path.join(self.dir, key), "rb") as handle:
            return parse_shard_snapshots(handle.read(), key)

    def test_two_shards_of_one_index_share_no_writer_uuid(self):
        self.assertEqual(frozenset(),
                         self._document(self.SHARD_0).writer_uuids
                         & self._document(self.SHARD_1).writer_uuids)

    def test_both_shards_actually_carry_writer_uuids(self):
        # The abuse case, and it is not hypothetical. Two empty sets are
        # disjoint, so the test above passes for free against any capture whose
        # documents carry no writer uuid at all, which is what a pre-7.x
        # segment or a decoder that dropped the field looks like.
        self.assertEqual(8, len(self._document(self.SHARD_0).writer_uuids))
        self.assertEqual(8, len(self._document(self.SHARD_1).writer_uuids))

    def test_the_two_shards_carry_identical_snapshot_names(self):
        # Why the writer uuid is needed at all. These two directories are
        # covered by the same two snapshots, so the name set separates neither
        # from the other, and every per-directory check has to lean on the
        # blobs and the writer identity instead.
        first = set(self._document(self.SHARD_0).by_snapshot_name)
        self.assertEqual(first, set(self._document(self.SHARD_1).by_snapshot_name))
        self.assertEqual({"s1", "s2"}, first)


if __name__ == "__main__":
    unittest.main()
