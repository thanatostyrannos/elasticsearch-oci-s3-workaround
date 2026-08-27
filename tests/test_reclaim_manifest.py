"""Reading a manifest: exactly the keys it names, and a refusal for a file
that cannot prove it is whole.

`reporting/manifest.py` writes a header and one row per key, then (issue
#61) a `# derivation complete` line once every row is written and only when
the run was not refused. These tests build manifests by hand, in the exact
header/row/marker shape the audit tool writes, and check that this package's
reader hands back precisely what a complete, marked manifest names and
refuses precisely the shapes an interrupted, refused, or foreign write
actually leaves.
"""

import hashlib
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generation_chain.reclaim.manifest import (EXPECTED_HEADER, ManifestError,
                                               load_manifest)
from generation_chain.reporting.manifest import COMPLETION_MARKER


def write(path: str, *rows: str, complete: bool = True) -> None:
    """A manifest in the exact shape the audit CLI produces.

    `complete=True`, the default, appends COMPLETION_MARKER after the rows,
    the same as a successful run written through `--manifest FILE`.
    `complete=False` stops after the rows, the same as a refused run's file,
    or one written to stdout and redirected by hand.
    """
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(EXPECTED_HEADER + "\n")
        for row in rows:
            handle.write(row + "\n")
        if complete:
            handle.write(COMPLETION_MARKER)


ROW = "indices/iuuid/0/__blob\treason text\tsegment blob\tsuuid\tsname\t1\t2"


class LoadManifest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="reclaim-manifest-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "manifest.tsv")

    def test_reads_exactly_the_keys_the_manifest_names_in_order(self):
        # Use case, and it pins order and duplicates too. If a refactor ever
        # sorted, deduplicated, or otherwise touched this list, it would stop
        # being "exactly what the manifest names" and start being a derived
        # set, which is the one thing this reader must never produce.
        write(self.path, ROW, ROW.replace("iuuid/0", "iuuid/1"),
             ROW)  # a duplicate row, on purpose
        data = load_manifest(self.path)
        self.assertEqual(data.keys, (
            "indices/iuuid/0/__blob", "indices/iuuid/1/__blob",
            "indices/iuuid/0/__blob"))

    def test_an_empty_manifest_after_the_header_names_zero_keys(self):
        # Use case for a clean, successful repository: a header with no rows
        # but a completion marker is a valid manifest naming nothing, not a
        # refusal.
        write(self.path)
        data = load_manifest(self.path)
        self.assertEqual(data.keys, ())

    def test_a_manifest_missing_the_completion_marker_is_refused(self):
        # THE central guard now that issue #61 gives the derivation a real
        # way to say "finished": a file with a real header and well-formed
        # rows but no marker as its last line is exactly what a refused run,
        # a stdout redirect, or a kill just before the marker line all leave
        # behind. Neutered under
        # "a-manifest-without-the-completion-marker-is-refused".
        write(self.path, ROW, complete=False)
        with self.assertRaises(ManifestError):
            load_manifest(self.path)

    def test_a_header_only_file_with_no_marker_is_refused(self):
        # The specific real-world shape a refused run's own manifest file
        # takes: a header, zero rows, and (before issue #61, and again if the
        # CLI's own marker step were ever skipped) nothing else. A reader
        # that treated "header, no rows" as "clean repository" would read a
        # refusal as a manifest naming nothing to delete, which is not the
        # same claim at all.
        write(self.path, complete=False)
        with self.assertRaises(ManifestError):
            load_manifest(self.path)

    def test_a_marker_that_is_not_the_last_line_does_not_count(self):
        # Abuse case for the marker check itself: something appended after
        # the marker, whether a hand edit or a second run's output pasted on
        # underneath, means the file's last line is no longer the marker,
        # and a marker earlier in the file proves nothing about what
        # follows it.
        write(self.path, ROW)  # writes header, ROW, marker
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(ROW.replace("iuuid/0", "iuuid/9") + "\n")
        with self.assertRaises(ManifestError):
            load_manifest(self.path)

    def test_a_file_missing_its_trailing_newline_is_refused(self):
        # Abuse case: a process killed mid-write stops after some number of
        # complete bytes, and the ordinary way that looks is a file with no
        # newline after its last row. Neutered in tests/genchain_neuter.py
        # under "a-manifest-without-a-trailing-newline-is-refused".
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(EXPECTED_HEADER + "\n" + ROW)  # no final "\n"
        with self.assertRaises(ManifestError):
            load_manifest(self.path)

    def test_a_row_with_too_few_fields_is_refused(self):
        # Abuse case: a write stopped in the middle of a row rather than
        # exactly at its end. Neutered under
        # "a-manifest-row-with-the-wrong-field-count-is-refused".
        write(self.path, "indices/iuuid/0/__blob\treason text\tsegment blob")
        with self.assertRaises(ManifestError):
            load_manifest(self.path)

    def test_a_row_with_too_many_fields_is_refused(self):
        # Abuse case in the other direction: a stray tab, from a key or a
        # reason string that should never have reached this format, must not
        # be silently absorbed into the wrong column.
        write(self.path, ROW + "\textra")
        with self.assertRaises(ManifestError):
            load_manifest(self.path)

    def test_a_foreign_header_is_refused(self):
        # Abuse case: a file from a different tool, or a listing rather than
        # a manifest, must not be read as though its first column were `key`.
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("not\tthe\tright\theader\n")
        with self.assertRaises(ManifestError):
            load_manifest(self.path)

    def test_an_empty_file_is_refused_rather_than_read_as_zero_keys(self):
        # Abuse case distinguishing "zero rows after a real header" (valid)
        # from "nothing was ever written" (not a manifest at all).
        open(self.path, "w").close()
        with self.assertRaises(ManifestError):
            load_manifest(self.path)

    def test_the_digest_is_the_sha256_of_the_exact_bytes_on_disk(self):
        # Pins the tie between the manifest reader and approval.py: the
        # digest an operator approves must be computable by anyone from the
        # file alone, with a standard tool, not derived from this package's
        # own parsed representation of it.
        write(self.path, ROW)
        with open(self.path, "rb") as handle:
            raw = handle.read()
        data = load_manifest(self.path)
        self.assertEqual(data.digest, hashlib.sha256(raw).hexdigest())

    def test_a_missing_file_is_refused_with_no_traceback(self):
        with self.assertRaises(ManifestError):
            load_manifest(os.path.join(self.dir, "does-not-exist.tsv"))


if __name__ == "__main__":
    unittest.main()
