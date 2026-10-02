"""snapshot_sizes.py refuses a --batch that cannot fetch snapshot status."""

import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import snapshot_sizes as sizes


def check(*batch):
    """Run the tool's own parser and argument check, return the exit code."""
    parser = sizes.build_parser()
    args = parser.parse_args(
        ["--es", "https://es.example:9200", "--repo", "r", *batch])
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            sizes.check_arguments(parser, args)
        except SystemExit as exc:
            return exc.code
    return 0


class BatchMustFetchSomething(unittest.TestCase):

    def test_a_batch_of_one_is_accepted(self):
        # One snapshot per _status request is the smallest batch that still
        # fetches everything. An operator lowers --batch this far when a
        # cluster times out on bigger requests, so the floor must allow it.
        self.assertEqual(check("--batch", "1"), 0)

    def test_the_default_batch_is_accepted(self):
        # Every run that does not pass --batch goes through this path. A
        # check that rejected the default would break the tool for everyone.
        self.assertEqual(check(), 0)

    def test_a_batch_of_zero_is_refused(self):
        # Abuse: a wrapper that computes --batch from a snapshot count can
        # produce 0. The tool then crashed with a bare traceback partway
        # through the run instead of refusing before it started.
        self.assertEqual(check("--batch", "0"), 2)

    def test_a_negative_batch_is_refused(self):
        # Abuse: a negative batch fetched no snapshot status at all and the
        # run still exited 0, printing an empty sizing report that reads as
        # a repository with no snapshots. Capacity decisions get made from
        # that report.
        self.assertEqual(check("--batch", "-5"), 2)


if __name__ == "__main__":
    unittest.main()
