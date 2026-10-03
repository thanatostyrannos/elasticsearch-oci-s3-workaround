"""The audit CronJob must put only its own output files under the run directory.

The template builds the flag list as positional parameters and hands it to the audit. These
checks read the template text, because rendering it needs helm and the suite
runs with the standard library alone.
"""

import pathlib
import re
import unittest

TEMPLATE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "gitlab" / "kubernetes-test-rig" / "chart" / "templates" / "audit-cronjob.yaml"
).read_text()

OUTPUT_FLAGS = {
    "--manifest": "orphans.tsv",
    "--classification": "classification.tsv",
    "--coverage-json": "coverage.json",
}


class AuditOutputPaths(unittest.TestCase):

    def test_output_flags_are_built_from_the_run_directory(self):
        # Each audit run writes into its own timestamped directory so a new run
        # cannot overwrite the last run's manifest. If a flag falls back to a
        # bare /output path, runs overwrite each other and the earlier
        # evidence is gone.
        for flag, name in OUTPUT_FLAGS.items():
            self.assertIn(f'set -- "$@" {flag} "$RUN_DIR/{name}"', TEMPLATE)

    def test_run_directory_is_set_before_the_first_output_flag(self):
        # The flags expand $RUN_DIR when the line runs. If the assignment moves
        # below them, the audit gets /orphans.tsv and tries to write at the
        # filesystem root.
        assign = TEMPLATE.index('RUN_DIR="/output/')
        first_use = TEMPLATE.index("$RUN_DIR/orphans.tsv")
        self.assertLess(assign, first_use)

    def test_no_rewrite_runs_over_the_whole_argument_string(self):
        # Abuse case: a repo path or prefix a user sets to something with
        # /output/ in it, such as a mirror mounted at /output/mirror. A sed
        # over the full string rewrites that value into the run directory and
        # the audit scans a directory that does not exist, or the wrong one.
        self.assertIsNone(re.search(r'\bsed\b', TEMPLATE))
        self.assertNotIn('args=$(echo "$args"', TEMPLATE)

    def test_only_the_run_directory_assignment_names_the_output_root(self):
        # Abuse case, same hazard from the other side: any other literal
        # /output/ in the command is a path that a later rewrite or edit can
        # treat as an output file.
        hits = [l for l in TEMPLATE.splitlines() if "/output/" in l]
        self.assertEqual(len(hits), 1)
        self.assertIn("RUN_DIR=", hits[0])


if __name__ == "__main__":
    unittest.main()
