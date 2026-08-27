"""Two faults at once, where each one alone is already handled.

Every defect in `genchain_repo.Defects` is applied ONE AT A TIME by the rest of
this suite, and that is where its counterexamples came from. A second
implementation of this same design, built independently from the same starting
point, lost data to a pair of them instead.

Its shape: a snapshot that declares failed shards cannot be demanded of any
shard document, so the completeness gate waived it. A genuine earlier document,
missing only that snapshot's entry, then passed every identity gate. The
partial snapshot's segments fell out of the live set and one it still restores
from was condemned. Nothing in that construction is a forged object. Every byte
was written by Elasticsearch; the store served an old one for a current key.

This package survives it, and these tests are here so that stays true. It does
not survive it by waiving less. It survives because two separate gates each
demand the partial snapshot independently, and the neutering below shows both.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import genchain_repo as repo
from generation_chain import run_audit
from generation_chain.sources.local import LocalMirrorSource

# `__shared` is the segment that matters: the deleted snapshot `d` named it, so
# it is a CANDIDATE, and the live snapshot `p` restores from it, so naming it
# is a loss. A segment only `p` names could never be condemned at all, because
# this package condemns on PRESENCE in a deleted snapshot's file list and never
# on absence from a live one. Getting that wrong is how a composed-fault test
# passes while measuring nothing.
#
# Generation 0 predates `p`, so its file list is the one that loses `__shared`
# when the store serves it in place of the current one. One shard, because a
# second would trip a different gate first.
HISTORY = [
    {"a": {"wide": {0: ["__w0a"]}}},
    {"a": {"wide": {0: ["__w0a"]}},
     "p": {"wide": {0: ["__w0a", "__shared"]}},
     "d": {"wide": {0: ["__w0a", "__shared"]}}},
    {"a": {"wide": {0: ["__w0a"]}},
     "p": {"wide": {0: ["__w0a", "__shared"]}}},
]
WIDE_0 = repo.directory_of("wide", 0)


class APartialSnapshotAndAStaleDocument(unittest.TestCase):

    def _audit(self, stale: bool, partial: bool):
        directory = tempfile.mkdtemp(prefix="genchain-composed-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        defects = repo.Defects(
            partial_snapshots=["p"] if partial else [],
            declared_shard_count={("p", "wide"): 2} if partial else {})
        self.built = repo.build(directory, HISTORY, defects=defects)
        if stale:
            self._serve_the_earlier_document(directory)
        return run_audit(LocalMirrorSource(directory))

    @staticmethod
    def _serve_the_earlier_document(root: str) -> None:
        """Put generation 0's real document under generation 1's key.

        A store serving a stale object, which is the failure this models. Both
        documents are genuine and neither is edited.
        """
        where = os.path.join(root, repo.directory_of("wide", 0))
        earlier = os.path.join(
            where, "index-" + repo.shard_generation_id("wide", 0, 0, False))
        current = os.path.join(
            where, "index-" + repo.shard_generation_id("wide", 0, 2, False))
        shutil.copyfile(earlier, current)

    def _live_named(self, result):
        return self.built.live_blob_keys & set(result.keys)

    def test_the_pair_together_names_nothing_a_live_snapshot_restores_from(self):
        # The whole point. If this ever fails, an operator acting on the
        # manifest deletes a segment the live snapshot `p` needs, and this
        # store has no recovery path for a deleted object.
        result = self._audit(stale=True, partial=True)
        self.assertEqual(set(), self._live_named(result))

    def test_the_pair_together_drops_the_shard_rather_than_reading_it(self):
        # Naming nothing is not enough on its own, because a run that read the
        # shard and found nothing to condemn would also name nothing. The
        # shard has to be refused.
        result = self._audit(stale=True, partial=True)
        self.assertIn(WIDE_0, result.coverage.shards_dropped)

    def test_a_partial_snapshot_on_its_own_is_read_normally(self):
        # The control. Without this, the test above could pass because partial
        # snapshots are refused outright, which would cost coverage on every
        # repository that has ever had a shard fail mid-snapshot.
        result = self._audit(stale=False, partial=True)
        self.assertEqual({}, result.coverage.shards_dropped)
        self.assertEqual(set(), self._live_named(result))

    def test_a_stale_document_on_its_own_is_already_refused(self):
        # The other control. The pair is only interesting if neither half is
        # doing all the work, and this half is not new.
        result = self._audit(stale=True, partial=False)
        self.assertIn(WIDE_0, result.coverage.shards_dropped)
        self.assertEqual(set(), self._live_named(result))
