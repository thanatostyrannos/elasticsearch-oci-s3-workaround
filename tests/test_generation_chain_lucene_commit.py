"""The Lucene commit point checked as an independent second opinion.

Issue #1: Elasticsearch's own corroboration is common-mode with the file
list it is supposed to corroborate, because both are read out of the same
object store. A tamper (or a genuine upstream format change) that drops the
same live segment from both `index-<gen>` and `snap-<uuid>.dat`, keeping
`segments_N` and patching the counts to match, satisfies every check that
compares those two copies to each other. `segments_N` is written by a
different layer for a different reason, so comparing what it requires
against the file list this tool was handed catches that drift without asking
Elasticsearch anything.

These tests build the parsed document directly rather than through the byte-
level codec framing the rest of this suite uses (`shard_blob` /
`codec_wrap`), because the field under test, `meta_hash`, is only ever bytes
when Elasticsearch writes the document as SMILE. Round-tripping it through a
JSON fixture would either drop the point of the test or make the test build
a SMILE encoder nobody else needs. `unwrap` is mocked to hand back the
document directly instead, which keeps the shape-gate logic under test on
the real code path while sidestepping an encoding concern this defect has
nothing to do with. The commit bytes themselves come from
`test_lucene_segments.lucene_commit`, which is exercised on its own in that
file; here it is only a fixture.
"""

import dataclasses
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import genchain_repo as repo
from generation_chain import run_audit
from generation_chain.errors import ShapeGateError
from generation_chain.formats import shard_snapshots
from generation_chain.formats.shard_snapshots import (
    parse_shard_snapshots as real_parse_shard_snapshots)
from generation_chain.sources.local import LocalMirrorSource
from test_lucene_segments import lucene_commit


def _file_entry(name, physical_name, content=None):
    entry = {"name": name, "physical_name": physical_name, "length": 1}
    if content is not None:
        entry["meta_hash"] = content
    return entry


def _shard_document(snapshot_files, commit_name, commit_physical,
                    commit_content, inline_si=()):
    """One snapshot's worth of a shard document, commit included.

    `snapshot_files` is {declared name: physical Lucene name} for the
    ordinary segment blobs. The commit entry is added separately because its
    content, when present, is what this module's new check reads.
    `inline_si` names segments whose `.si` file is listed as an inline
    `v__` entry, which is how Elasticsearch stores every `.si`.
    """
    inline = {f"v__si{segment}": f"{segment}.si" for segment in inline_si}
    files = [_file_entry(name, physical)
             for name, physical in {**snapshot_files, **inline}.items()]
    files.append(_file_entry(commit_name, commit_physical, commit_content))
    return {
        "files": files,
        "snapshots": {
            "s1": {"files": list(snapshot_files) + list(inline)
                   + [commit_name]},
        },
    }


def _parse(document):
    with patch.object(shard_snapshots, "unwrap", return_value=document):
        return shard_snapshots.parse_shard_snapshots(b"ignored", "where")


class LuceneCommitCrossCheck(unittest.TestCase):

    def test_a_file_list_that_covers_every_required_segment_parses(self):
        # The use case. The commit needs _0 and _1, and the snapshot's file
        # list carries a blob for each, so the independent oracle agrees
        # with the file list and the document reads normally.
        document = _shard_document(
            {"__a": "_0.cfs", "__b": "_1.cfs"},
            "v__c", "segments_1", lucene_commit(["_0", "_1"]))
        parsed = _parse(document)
        self.assertEqual(parsed.by_snapshot_name["s1"], frozenset({"__a", "__b"}))

    def test_a_segment_the_commit_needs_and_the_file_list_drops_is_refused(self):
        # This is issue #1's reproduction, translated into the shape this
        # reader sees. Elasticsearch's real attack drops a live segment from
        # both `index-<gen>` and `snap-<uuid>.dat` at once and patches the
        # counts to match, which is invisible to every check that compares
        # the two ES-owned copies to each other because they still agree.
        # Here the file list has dropped segment _1's blob and kept
        # `segments_N` unchanged, so the commit still requires a segment the
        # file list no longer references. Before this reader existed, that
        # was accepted: the presence gate only asked whether SOME
        # `segments_N` name was there, never what it required. If this stops
        # raising, the fix this issue asked for is gone and a restore from
        # this snapshot silently comes back short one segment.
        document = _shard_document(
            {"__a": "_0.cfs"},
            "v__c", "segments_1", lucene_commit(["_0", "_1"]))
        with self.assertRaises(ShapeGateError):
            _parse(document)

    def test_the_real_shape_with_every_segment_s_blob_parses(self):
        # Use case in the shape Elasticsearch writes: each segment has an
        # inline `.si` beside its blobs. If the stricter rule below refused
        # this, every real shard document would be dropped.
        document = _shard_document(
            {"__a": "_0.cfs", "__b": "_1.cfs"},
            "v__c", "segments_1", lucene_commit(["_0", "_1"]),
            inline_si=["_0", "_1"])
        parsed = _parse(document)
        self.assertEqual(parsed.by_snapshot_name["s1"], frozenset({"__a", "__b"}))

    def test_an_inline_si_alone_does_not_represent_a_segment(self):
        # Issue #1's tamper in the shape Elasticsearch actually writes. Every
        # `.si` is stored inline, so a file list that drops segment _1's
        # blob keeps `_1.si`, and the oracle used to count that inline entry
        # as _1 being present. The check then passed on every real document
        # and `__b`'s blob could be named while the report said the oracle
        # ran. Neutered under "an-inline-entry-does-not-represent-a-segment".
        document = _shard_document(
            {"__a": "_0.cfs"},
            "v__c", "segments_1", lucene_commit(["_0", "_1"]),
            inline_si=["_0", "_1"])
        with self.assertRaises(ShapeGateError):
            _parse(document)

    def test_a_commit_this_reader_cannot_decode_is_refused(self):
        # Abuse case for the fail-closed direction. A commit that is present
        # but corrupt, truncated, or deliberately rewritten to evade the
        # check above must not be treated as "no opinion" and waved through;
        # every uncertainty here has to resolve toward naming fewer keys, so
        # an unreadable commit refuses the whole shard rather than skip the
        # comparison it cannot make.
        document = _shard_document(
            {"__a": "_0.cfs"}, "v__c", "segments_1", b"not a lucene commit")
        with self.assertRaises(ShapeGateError):
            _parse(document)

    def test_a_commit_with_no_inline_content_still_parses(self):
        # Regression guard for every other fixture in this suite, none of
        # which attaches real commit bytes to its synthetic `segments_N`
        # entries. Real Elasticsearch 9.5.2 always inlines this commit for a
        # shard of realistic size (verified against the captured fixtures in
        # `lucene_segments.py`'s docstring), so an absent `meta_hash` is
        # either a hand-built test fixture or a shard large enough to store
        # the commit out of line. Either way this reader has no bytes to
        # check, and it falls back to the name-only presence gate that
        # existed before this fix, exactly as it did for every shard document
        # this suite already built before this file existed.
        document = _shard_document(
            {"__a": "_0.cfs"}, "v__c", "segments_1", None)
        parsed = _parse(document)
        self.assertEqual(parsed.by_snapshot_name["s1"], frozenset({"__a"}))


# One shard, one snapshot, present across three generations, so the survey
# reads three distinct shard-document keys: the current one plus two eras.
# `genchain_repo.build` never attaches a real inline commit to its synthetic
# `segments_N` entries (see `test_a_commit_with_no_inline_content_still_parses`
# above), so every one of the three parses reports commit_oracle_skipped=1
# and commit_oracle_checked=0 on its own. The count this class pins is not
# that number; it is that `Coverage` ends up holding the SUM of whatever each
# parsed document reported, across every document the survey actually read.
COVERAGE_HISTORY = [
    {"s1": {"idx": {0: ["__a"]}}},
    {"s1": {"idx": {0: ["__a"]}}},
    {"s1": {"idx": {0: ["__a"]}}},
]


class CommitOracleCoverage(unittest.TestCase):
    """`Coverage.commit_oracle_checked` / `_skipped` reflect every document read.

    Before this pair existed, a run where the Lucene cross-check fired on
    every entry and a run where it fired on none produced an identical
    manifest and an identical `Coverage`. `KeyIndex.unanswered` was kept
    separate from a plain denial for the same reason: this project's own
    history names folding a check that did not run into a check that ran and
    passed as the one measured place its report was wrong rather than
    conservative.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-commit-oracle-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        repo.build(self.root, COVERAGE_HISTORY)

    def test_the_coverage_totals_are_the_sum_of_every_document_parsed(self):
        calls = []

        def counting_parse(data, where):
            document = real_parse_shard_snapshots(data, where)
            calls.append(where)
            # The real parser never reports both on one document (a commit is
            # either inline and compared, or absent and skipped), but a fixed
            # 1-and-1 per call is what makes the sum below unambiguous: it
            # can only match if every one of `calls` actually landed in
            # Coverage, not just the last one or the first.
            return dataclasses.replace(
                document, commit_oracle_checked=1, commit_oracle_skipped=1)

        with patch("generation_chain.derivation.shards.parse_shard_snapshots",
                  side_effect=counting_parse):
            result = run_audit(LocalMirrorSource(self.root))

        # Sanity: this test is only evidence about SUMMING if more than one
        # document was actually read. A survey that reads one document and a
        # survey that reads three would look identical against a weaker
        # assertion than the equality below.
        # A key parsed under two caches is counted once, so the expected
        # total is the distinct keys, not the parses.
        self.assertGreater(len(set(calls)), 1)
        self.assertEqual(result.coverage.commit_oracle_checked, len(set(calls)))
        self.assertEqual(result.coverage.commit_oracle_skipped, len(set(calls)))


# A repository with a current generation and one earlier era per shard, the
# shape the double count needed: the current document is read in pass one and
# again as the last era.
TALLY_HISTORY = [
    {"s1": {"i": {0: ["__i1"], 1: ["__j1"]}}},
    {"s1": {"i": {0: ["__i1"], 1: ["__j1"]}},
     "s2": {"i": {0: ["__i2"], 1: ["__j2"]}}},
    {"s2": {"i": {0: ["__i2"], 1: ["__j2"]}}},
]


class CommitOracleTallyCountsEachDocumentOnce(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-oracle-tally-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        repo.build(self.root, TALLY_HISTORY)

    def _audit_recording_fetches(self):
        fetched = set()

        class Recording(LocalMirrorSource):
            def fetch(self, key):
                if "/index-" in key:
                    fetched.add(key)
                return super().fetch(key)

        return run_audit(Recording(self.root)), fetched

    def test_each_directory_and_generation_is_counted_once(self):
        # If this failed, the report would say the Lucene cross-check looked
        # at more (snapshot, commit) pairs than exist, and an operator would trust
        # a check that covered less of the repository than printed.
        result, fetched = self._audit_recording_fetches()
        coverage = result.coverage
        pairs = 0
        for key in fetched:
            document = real_parse_shard_snapshots(
                repo.read(self.root, key), key)
            pairs += (document.commit_oracle_checked
                      + document.commit_oracle_skipped)
        self.assertEqual(pairs, 8)
        self.assertEqual(
            coverage.commit_oracle_checked + coverage.commit_oracle_skipped,
            pairs)

    def test_the_tally_never_changes_what_is_condemned(self):
        # If the report-only tally started steering classification, a change
        # to how documents are counted would silently change which blobs the
        # manifest lets an operator delete.
        counted, _ = self._audit_recording_fetches()
        with patch("generation_chain.derivation.shards."
                   "CommitOracleTally.record"):
            uncounted = run_audit(LocalMirrorSource(self.root))
        self.assertEqual(counted.condemned, uncounted.condemned)
        self.assertTrue(counted.condemned)


if __name__ == "__main__":
    unittest.main()
