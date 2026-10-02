"""A listing that cannot prove it is complete, and a generation that cannot prove it is foreign.

Each case below is a way the run could shrink its own input and carry on:
a directory it could not read, a page that never said whether more followed,
a generation above `index.latest` that names no repository, and a repository
uuid that is a placeholder. All four must end in a refusal with nothing
condemned, because a smaller input makes the live set look smaller.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import genchain_repo as repo
import s3rig
from generation_chain import run_audit
from generation_chain.credentials import Secret
from generation_chain.errors import RunRefused, SourceReadError
from generation_chain.sources import s3 as s3_module
from generation_chain.sources.local import LocalMirrorSource
from generation_chain.sources.s3 import S3CompatibleSource, S3Credentials

HISTORY = [
    {"s1": {"i": {0: ["__i1"], 1: ["__j1"]}}},
    {"s1": {"i": {0: ["__i1"], 1: ["__j1"]}},
     "s2": {"i": {0: ["__i2"], 1: ["__j2"]}}},
    {"s2": {"i": {0: ["__i2"], 1: ["__j2"]}}},
]
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def _page(truncated_text):
    inner = "" if truncated_text is None else f"<IsTruncated>{truncated_text}</IsTruncated>"
    return ET.fromstring(
        f'<ListBucketResult xmlns="{NS[1:-1]}">{inner}</ListBucketResult>')


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="genchain-partial-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.built = repo.build(self.dir, HISTORY)

    def s3_source(self, rig):
        return S3CompatibleSource(
            endpoint=rig.endpoint, region=s3rig.TEST_REGION,
            bucket=s3rig.TEST_BUCKET,
            credentials=S3Credentials(s3rig.TEST_ACCESS_KEY,
                                      Secret(s3rig.TEST_SECRET_KEY)))

    def assertRefusedWithNothingCondemned(self, result):
        self.assertIsNotNone(result.coverage.refused)
        self.assertEqual([], result.condemned)


class UnreadableDirectory(_Fixture):

    def test_a_readable_mirror_lists_every_key(self):
        # If this stopped passing, the stricter walk would reject healthy
        # mirrors and no audit of a local copy would ever finish.
        self.assertEqual(sorted(repo.read_keys(self.dir)),
                         LocalMirrorSource(self.dir).list_keys())

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode 000 directory")
    def test_a_directory_the_walk_cannot_open_is_a_read_failure(self):
        # A silently skipped shard directory drops that directory's blobs from
        # the present set, so the run reports a repository smaller than it is
        # and an operator reads the shortfall as a clean result.
        sub = os.path.join(self.dir, "indices", repo.index_uuid("i"), "1")
        os.chmod(sub, 0)
        self.addCleanup(os.chmod, sub, 0o755)
        with self.assertRaises(SourceReadError):
            LocalMirrorSource(self.dir).list_keys()
        self.assertRefusedWithNothingCondemned(
            run_audit(LocalMirrorSource(self.dir)))


class TruncationFlag(_Fixture):

    def test_a_paged_listing_with_the_flag_is_read_to_the_end(self):
        # If this stopped passing, the strict flag check would break paging
        # against every store that answers correctly.
        with s3rig.S3Rig(self.dir, page_size=7) as rig:
            keys = self.s3_source(rig).list_keys()
        self.assertEqual(sorted(repo.read_keys(self.dir)), keys)

    def test_a_page_with_no_flag_refuses_the_run(self):
        # A store that leaves IsTruncated out would otherwise end the listing
        # at page one and hide most of the repository from the run.
        with s3rig.S3Rig(self.dir, page_size=7, omit_is_truncated=True) as rig:
            source = self.s3_source(rig)
            with self.assertRaises(RunRefused) as raised:
                source.list_keys()
            self.assertFalse(raised.exception.transient)
            self.assertRefusedWithNothingCondemned(run_audit(source))

    def test_the_flag_is_read_exactly_true_or_false(self):
        # Anything else is a store this code does not understand. Guessing
        # "last page" for it is the silent partial listing again.
        self.assertIsNone(s3_module._continuation_token(_page("false")))
        for value in ("TRUE", "True", "yes", "", "1", " true", "false "):
            with self.subTest(value=value):
                with self.assertRaises(RunRefused):
                    s3_module._continuation_token(_page(value))


class GenerationAboveLatest(_Fixture):

    def _write_generation_nine(self, document):
        base = {"min_version": "7.12.0", "snapshots": [], "indices": {},
                "index_metadata_identifiers": {}}
        base.update(document)
        with open(os.path.join(self.dir, "index-9"), "wb") as handle:
            handle.write(json.dumps(base).encode("utf-8"))

    def test_a_foreign_generation_above_latest_is_still_set_aside(self):
        # If this stopped passing, a co-tenant's higher numbered blob would
        # refuse every run in a shared bucket instead of being ignored.
        self._write_generation_nine({"uuid": "somebody-elses-repository"})
        result = run_audit(LocalMirrorSource(self.dir))
        self.assertIsNone(result.coverage.refused)
        self.assertEqual(2, result.coverage.current_generation)
        self.assertIn(9, result.coverage.generations_rejected)

    def test_a_generation_above_latest_with_no_uuid_refuses(self):
        # With no uuid it cannot be proven foreign, and if it is ours it is the
        # current generation. Anchoring below it condemns blobs only it names.
        self._write_generation_nine({})
        self.assertRefusedWithNothingCondemned(
            run_audit(LocalMirrorSource(self.dir)))
        self.assertFalse(
            run_audit(LocalMirrorSource(self.dir)).coverage.refusal_is_transient)


class UnassignedRepositoryUuid(_Fixture):

    def test_an_assigned_uuid_anchors_normally(self):
        # If this stopped passing, the new refusal would catch every ordinary
        # repository.
        self.assertIsNone(run_audit(LocalMirrorSource(self.dir)).coverage.refused)

    def test_the_placeholder_uuid_refuses_and_says_why(self):
        # A repository that predates uuids has no identity to tell its own
        # generation blobs from a co-tenant's, so no anchor is safe.
        repo.build(self.dir, HISTORY, defects=repo.Defects(
            foreign_uuid_at={g: "_na_" for g in range(3)}))
        result = run_audit(LocalMirrorSource(self.dir))
        self.assertRefusedWithNothingCondemned(result)
        self.assertIn("predates", result.coverage.refused)
        self.assertFalse(result.coverage.refusal_is_transient)


if __name__ == "__main__":
    unittest.main()
