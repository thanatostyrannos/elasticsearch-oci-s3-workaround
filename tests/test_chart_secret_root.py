#!/usr/bin/env python3
"""Every rig container sets GENCHAIN_SECRET_ROOT to the directory of secrets.

snapshot_churn_rig.py refuses a secret file outside the current directory or
GENCHAIN_SECRET_ROOT. The chart mounts every secret under /secrets while the
working directory is the source checkout, so a container that lacks the
variable exits 2 before it reaches the cluster.
"""
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHART = ROOT / "gitlab/kubernetes-test-rig/chart"
HELM = shutil.which("helm")

INVOCATION = re.compile(r"snapshot_churn_rig\.py\W+(run|teardown)\b")
IMAGE = re.compile(r"^\s*image:", re.MULTILINE)
SETTING = re.compile(
    r"name: GENCHAIN_SECRET_ROOT\n\s+value: /secrets\n")


def containers_without_the_root(rendered):
    """Subcommands of the rig invocations whose container lacks the setting.

    The container's env sits between its image line and its command, so the
    text from the last image line before an invocation is that container.
    """
    missing = []
    count = 0
    for hit in INVOCATION.finditer(rendered):
        images = list(IMAGE.finditer(rendered, 0, hit.start()))
        container = rendered[images[-1].start():hit.start()]
        count += 1
        if not SETTING.search(container):
            missing.append(hit.group(1))
    return count, missing


def render(*sets):
    cmd = [HELM, "template", "r", str(CHART),
           "--set", "teardown.standalone.enabled=true"]
    for value in sets:
        cmd += ["--set", value]
    return subprocess.run(cmd, text=True, capture_output=True)


@unittest.skipUnless(HELM, "helm is not installed")
class EveryRigContainerSetsTheSecretRoot(unittest.TestCase):
    def test_the_four_rig_containers_set_it(self):
        # Without it the churn Job, both teardown Jobs and the stale-state
        # init container exit 2 on their own password file, and a rig that
        # was meant to clean up leaves its load running.
        done = render()
        self.assertEqual(done.returncode, 0, done.stderr)
        count, missing = containers_without_the_root(done.stdout)
        # run, stale teardown, hook teardown, manual teardown
        self.assertGreaterEqual(count, 4)
        self.assertEqual(missing, [])

    def test_abuse_a_container_without_it_is_found(self):
        # Proves the check can fail: a container with no setting must be
        # reported, or the test above would pass vacuously.
        text = ("image: x\nenv:\n  - name: OTHER\n    value: /secrets\n"
                'command: ["python3", "snapshot_churn_rig.py", "run"]\n')
        self.assertEqual(containers_without_the_root(text), (1, ["run"]))

    def test_abuse_a_setting_in_an_earlier_container_does_not_count(self):
        # The setting must be in the container that runs the rig, not in a
        # sibling rendered before it.
        text = ("image: a\nenv:\n  - name: GENCHAIN_SECRET_ROOT\n"
                "    value: /secrets\nimage: b\n"
                'command: ["python3", "snapshot_churn_rig.py", "run"]\n')
        self.assertEqual(containers_without_the_root(text), (1, ["run"]))


if __name__ == "__main__":
    unittest.main()
