#!/usr/bin/env python3
"""The test-rig chart may only pass snapshot_churn_rig.py flags it accepts.

The chart builds the churn run, the pre-install stale teardown, the
pre-delete teardown hook and the manual teardown Job from templates, and the
script's parser is the only judge of what is valid. These tests render the
chart with helm and feed every rendered argument list to that real parser.
"""
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import snapshot_churn_rig as rig

CHART = ROOT / "gitlab/kubernetes-test-rig/chart"
HELM = shutil.which("helm")

COMMAND_LINE = re.compile(
    r'command: \["python3", "snapshot_churn_rig\.py", "(run|teardown)"\]\n\s+args:\n')
STALE_TEARDOWN = re.compile(r'snapshot_churn_rig\.py teardown "\$@"\n\s+- --\n')
ITEM = re.compile(r"^\s+- (.*)$")


def helm_template(*sets):
    cmd = [HELM, "template", "r", str(CHART), "--set", "teardown.standalone.enabled=true"]
    for value in sets:
        cmd += ["--set", value]
    return subprocess.run(cmd, text=True, capture_output=True)


def argument_lists(rendered):
    """(subcommand, args) for every rig invocation in the rendered chart."""
    found = []
    spots = [(m.group(1), m.end()) for m in COMMAND_LINE.finditer(rendered)]
    spots += [("teardown", m.end()) for m in STALE_TEARDOWN.finditer(rendered)]
    for subcommand, start in spots:
        args = []
        for line in rendered[start:].splitlines():
            if line.lstrip().startswith("#"):
                continue
            item = ITEM.match(line)
            if not item:
                break
            args.append(json.loads(item.group(1)) if item.group(1).startswith('"')
                        else item.group(1))
        found.append((subcommand, args))
    return found


def parse_error(subcommand, args):
    """The parser's complaint about this argument list, or None."""
    parser = rig.build_parser()
    try:
        parser.parse_args([subcommand] + args)
    except SystemExit as stop:
        return stop.code
    return None


@unittest.skipUnless(HELM, "helm is not installed")
class RenderedRigArgsTests(unittest.TestCase):
    def assert_all_parse(self, rendered):
        invocations = argument_lists(rendered)
        # run, stale teardown, hook teardown, manual teardown
        self.assertGreaterEqual(len(invocations), 4)
        for subcommand, args in invocations:
            self.assertIsNone(parse_error(subcommand, args), (subcommand, args))

    def test_default_values_render_args_the_script_accepts(self):
        # An argument the script's parser rejects exits the churn Job and
        # every teardown Job with status 2 before they touch the cluster,
        # so a rig that was meant to clean up leaves its load running.
        done = helm_template()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_all_parse(done.stdout)

    def test_ca_cert_values_render_args_the_script_accepts(self):
        # The supported way to reach a self-signed lab cluster is a CA. If
        # the chart stopped passing --ca-cert correctly, that path would
        # fail the same way the removed --insecure did.
        done = helm_template("elasticsearch.caCert=-----BEGIN CERTIFICATE-----")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assert_all_parse(done.stdout)
        self.assertIn("--ca-cert", done.stdout)

    def test_insecure_tls_refuses_to_render(self):
        # The script verifies certificates and has no switch to stop. A
        # chart that rendered --insecure produced Jobs that exit 2 on
        # argparse; refusing at render time tells the operator before
        # anything is applied, and names the CA path instead.
        done = helm_template("elasticsearch.insecureTls=true")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("caCert", done.stderr)
        self.assertNotIn("--insecure", done.stdout)

    def test_abuse_parser_rejects_the_old_insecure_argument(self):
        # Proves the check can fail: the argument the chart used to emit
        # must be refused by the real parser, or the tests above would pass
        # vacuously after a parser change.
        self.assertEqual(parse_error("run", ["--es", "https://x:9200", "--insecure"]), 2)

    def test_abuse_extraction_finds_nothing_in_unrelated_text(self):
        # Guards the extractor: if it silently found zero invocations the
        # render tests would be checking nothing.
        self.assertEqual(argument_lists("kind: Job\n"), [])


if __name__ == "__main__":
    unittest.main()
