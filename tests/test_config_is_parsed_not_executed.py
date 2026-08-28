"""The loop runner may not execute its own configuration file.

`scripts/run-test-cycle.sh` used to read its config with `. "$CONF"`, which
executes it. Anything in the file that looked like a command ran as the
operator, before a single check had happened.

The mode check limits that: the script refuses a config other users can write.
What it does not cover is the case that actually happens, which is a config
copied out of a runbook or a ticket and chmodded on arrival. Nobody reads a
settings file the way they read a script, and a `.conf` extension invites
exactly that assumption.

It parses now. This test holds it there, because the difference between
sourcing and parsing is invisible in a diff that touches the same line.

Filed as issue 14.
"""
import pathlib
import re
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run-test-cycle.sh"


class TheConfigFileIsNeverExecuted(unittest.TestCase):

    def test_the_script_does_not_source_its_config(self):
        # `. "$CONF"` and `source "$CONF"` both execute it. Neither may appear.
        body = SCRIPT.read_text()
        offenders = [
            line.strip() for line in body.split("\n")
            if re.match(r'^\s*(\.|source)\s+"?\$\{?CONF\}?"?\s*$', line)
        ]
        self.assertEqual(offenders, [], "the config is executed, not parsed")

    def test_a_command_in_the_config_does_not_run(self):
        # The whole point, exercised rather than asserted. If this ever passes
        # by running the command, the marker file gives it away.
        with tempfile.TemporaryDirectory() as directory:
            work = pathlib.Path(directory)
            marker = work / "the-config-was-executed"
            config = work / "hostile.conf"
            config.write_text(
                'ENDPOINT="https://example.invalid"\n'
                "REGION=r\nBUCKET=b\nPREFIX=p/\n"
                "CREDENTIALS=./creds.json\nREPOSITORY=x\nOUT=./out\n"
                f"$(touch {marker})\n")
            config.chmod(0o600)
            result = subprocess.run(
                ["bash", str(SCRIPT), str(config)],
                capture_output=True, text=True, timeout=120, cwd=work)
            self.assertFalse(
                marker.exists(),
                "the config file executed a command, so it is being sourced")
            self.assertIn("not KEY=value", result.stdout + result.stderr,
                          "the run should say which line it refused")

    def test_an_ordinary_config_still_reads(self):
        # Parsing is only an improvement if it still accepts what people write:
        # quoted values, bare values, comments and blank lines.
        with tempfile.TemporaryDirectory() as directory:
            work = pathlib.Path(directory)
            config = work / "good.conf"
            config.write_text(
                "# a comment\n\n"
                'ENDPOINT="https://example.invalid"\n'
                "REGION=us-ashburn-1\n"
                'BUCKET="my-bucket"\n'
                "PREFIX=base/\n"
                "CREDENTIALS=./creds.json\nREPOSITORY=repo\nOUT=./out\n")
            config.chmod(0o600)
            result = subprocess.run(
                ["bash", str(SCRIPT), str(config)],
                capture_output=True, text=True, timeout=120, cwd=work)
            combined = result.stdout + result.stderr
            self.assertNotIn("not KEY=value", combined,
                             "a normal config was refused by the parser")
            # It gets past parsing and fails later, on something real.
            self.assertIn("preflight", combined)


if __name__ == "__main__":
    unittest.main()
