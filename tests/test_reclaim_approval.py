"""Approval is about one exact manifest, never about a category of them.

`verify_approval` is the gate the issue asks for by name: "approving one
manifest cannot approve a different one." These tests build a manifest, and
check that only the digest and row count computed from that exact file pass,
that either value alone is not enough, and that neither being asked for is
skipped when the caller forgets to pass them.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generation_chain.reclaim.approval import ApprovalError, verify_approval
from generation_chain.reclaim.manifest import EXPECTED_HEADER, load_manifest
from generation_chain.reporting.manifest import COMPLETION_MARKER

ROW = "indices/iuuid/0/__blob\treason text\tsegment blob\tsuuid\tsname\t1\t2"


def manifest_at(path: str, *rows: str):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(EXPECTED_HEADER + "\n")
        for row in rows:
            handle.write(row + "\n")
        handle.write(COMPLETION_MARKER)
    return load_manifest(path)


class VerifyApproval(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="reclaim-approval-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.manifest = manifest_at(
            os.path.join(self.dir, "a.tsv"), ROW, ROW.replace("iuuid/0", "iuuid/1"))

    def test_the_exact_digest_and_row_count_pass(self):
        # Use case. If this ever raised, no manifest could ever be executed
        # against, which is the failure mode opposite the one this whole
        # module exists to prevent.
        verify_approval(self.manifest, self.manifest.digest, 2)

    def test_a_digest_from_a_different_manifest_is_refused(self):
        # Abuse case, and the guard the issue names: an edited or regenerated
        # manifest must fail approval rather than be silently accepted.
        # Neutered under "approval-checks-the-manifest-digest".
        other = manifest_at(os.path.join(self.dir, "b.tsv"), ROW)
        with self.assertRaises(ApprovalError):
            verify_approval(self.manifest, other.digest, 2)

    def test_a_mistyped_digest_is_refused(self):
        with self.assertRaises(ApprovalError):
            verify_approval(self.manifest, "0" * 64, 2)

    def test_the_right_digest_with_the_wrong_row_count_is_refused(self):
        # Abuse case: an operator who pastes the correct hash but a stale row
        # count, copied from an earlier run's coverage report, must still be
        # stopped. Neutered under "approval-checks-the-row-count".
        with self.assertRaises(ApprovalError):
            verify_approval(self.manifest, self.manifest.digest, 45000)

    def test_a_manifest_growing_between_runs_changes_both_values(self):
        # Realistic abuse case: the audit re-runs and condemns one more key.
        # An approval computed against the earlier, smaller file must not
        # pass against the new one, on either axis.
        grown = manifest_at(
            os.path.join(self.dir, "c.tsv"), ROW, ROW.replace("iuuid/0", "iuuid/1"),
            ROW.replace("iuuid/0", "iuuid/2"))
        with self.assertRaises(ApprovalError):
            verify_approval(grown, self.manifest.digest, len(self.manifest.keys))


if __name__ == "__main__":
    unittest.main()
