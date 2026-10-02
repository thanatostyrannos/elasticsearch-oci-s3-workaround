"""The loop runners must print their totals even when the protocol fails.

Both `scripts/run-test-cycle.sh` and the qualify job in the test-rig chart run
under `set -e`. A bare `python3 reclaim_test_protocol.py` followed by
`status=$?` exits at the failing command, so a failed run printed no done
line, no deleted/failed/unconfirmed totals and no cycle count. A failed run is
the one where an operator most needs to know what was deleted.

Filed as issue 41.
"""
import os
import pathlib
import re
import stat
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run-test-cycle.sh"
QUALIFY = (ROOT / "gitlab" / "kubernetes-test-rig" / "chart" / "templates"
           / "qualify-job.yaml")


def run_script(protocol_status, with_execute_file=True):
    """Run the script against a stub protocol that exits protocol_status."""
    with tempfile.TemporaryDirectory() as directory:
        work = pathlib.Path(directory)
        out = work / "out"
        out.mkdir()
        if with_execute_file:
            (out / "exec-1.txt").write_text(
                "deleted: 3\nfailed: 1\nunconfirmed: 2\n")
        creds = work / "creds.json"
        creds.write_text('{"s3": {}}')
        creds.chmod(0o600)
        conf = work / "run.conf"
        conf.write_text(
            'ENDPOINT="https://example.invalid"\nREGION=r\nBUCKET=b\n'
            f"PREFIX=p/\nCREDENTIALS={creds}\nREPOSITORY=x\nOUT={out}\n")
        conf.chmod(0o600)
        bindir = work / "bin"
        bindir.mkdir()
        real = subprocess.run(["bash", "-c", "command -v python3"],
                              capture_output=True, text=True).stdout.strip()
        stub = bindir / "python3"
        stub.write_text(
            "#!/bin/sh\n"
            f'if [ "$1" = reclaim_test_protocol.py ]; then exit {protocol_status}; fi\n'
            f'exec {real} "$@"\n')
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
        return subprocess.run(["bash", str(SCRIPT), str(conf)],
                              capture_output=True, text=True, timeout=120,
                              env=env, cwd=work)


class TotalsPrintWhenTheProtocolSucceeds(unittest.TestCase):

    def test_a_clean_run_prints_totals_and_exits_zero(self):
        # If the reporting block regresses on the happy path, operators lose
        # the only summary of what a passing run deleted.
        result = run_script(0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("done, exit 0", result.stdout)
        self.assertIn("deleted=3 failed=1 unconfirmed=2", result.stdout)


class TotalsPrintWhenTheProtocolFails(unittest.TestCase):

    def test_a_failed_run_still_prints_totals_and_keeps_its_status(self):
        # Models the protocol aborting mid-run, which is the run where the
        # operator most needs to know how much was deleted. If set -e ends the
        # script at the failing command again, the totals vanish and an
        # operator cannot tell what is gone from the bucket.
        result = run_script(7)
        self.assertEqual(result.returncode, 7, "the protocol status was lost")
        self.assertIn("done, exit 7", result.stdout)
        self.assertIn("deleted=3 failed=1 unconfirmed=2", result.stdout)

    def test_a_failure_before_any_cycle_file_still_reports(self):
        # Models the protocol dying before it writes cycles.tsv or any exec
        # file. If the reporting block aborts on the missing file, the script
        # exits with wc's status instead of the protocol's and prints no
        # totals line for an operator to read.
        result = run_script(5, with_execute_file=False)
        self.assertEqual(result.returncode, 5, "the protocol status was lost")
        self.assertIn("done, exit 5", result.stdout)
        self.assertIn("nothing was deleted", result.stdout)
        self.assertIn("cycles recorded: 0", result.stdout)


class TheQualifyJobReportsOnFailure(unittest.TestCase):

    def test_the_job_script_captures_status_without_aborting(self):
        # The chart runs under set -eu with no way to execute it offline, so
        # this pins the idiom. A bare command then `status=$?` makes the pod
        # exit before its totals print, and the Job log of a failed run ends
        # with no record of what was deleted.
        body = QUALIFY.read_text()
        self.assertRegex(
            body,
            r"status=0\n(?:\s*#[^\n]*\n)*\s*python3 reclaim_test_protocol\.py \"\$@\" \|\| status=\$\?")

    def test_no_bare_command_precedes_the_status_capture(self):
        # Abuse: a bare `python3 ... $args` directly above `status=$?` is the
        # defect itself, and a later edit could reintroduce it.
        body = QUALIFY.read_text()
        self.assertIsNone(re.search(
            r"\n\s*python3 reclaim_test_protocol\.py \"\$@\"\n\s*status=\$\?", body))


if __name__ == "__main__":
    unittest.main()
