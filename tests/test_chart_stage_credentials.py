"""stage-credentials copies each credential as the pod's own user, not root.

The init container used to run as root with every capability dropped and
chown each copy; without CAP_CHOWN that call fails with EPERM, so no pod that
stages credentials could start. These checks pin the replacement: the step
runs as the pod user, reads the Secret through the pod's fsGroup, and never
calls chown.
"""

import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

CHART = (
    pathlib.Path(__file__).resolve().parent.parent
    / "gitlab" / "kubernetes-test-rig" / "chart"
)
HELPERS = (CHART / "templates" / "_helpers.tpl").read_text()
HELM = shutil.which("helm")


def staging_block():
    start = HELPERS.index('{{- define "rig.credentialStagingInit" -}}')
    end = HELPERS.index("{{- end -}}", HELPERS.index("volumeMounts:", start))
    return HELPERS[start:end]


def staging_script():
    """The Python the init container runs, template directives removed."""
    block = staging_block()
    body = block[block.index("    - |\n") + len("    - |\n"):block.index("  volumeMounts:")]
    lines = [l[6:] if l.startswith("      ") else l.strip() for l in body.splitlines()]
    return "\n".join(l for l in lines if not l.lstrip().startswith("{{"))


def volume_block():
    start = HELPERS.index('{{- define "rig.credentialVolumes" -}}')
    return HELPERS[start:HELPERS.index("{{- end -}}\n", HELPERS.index("eck-elastic-user", start))]


class TheStagingStepNeedsNoRoot(unittest.TestCase):

    def test_the_step_does_not_run_as_root(self):
        # A root step needs CAP_CHOWN to hand files over, and the chart drops
        # every capability; that combination stopped every credential-using
        # pod at its init containers.
        block = staging_block()
        self.assertNotRegex(block, r"runAsUser:\s*0\b")
        self.assertIn("runAsNonRoot: true", block)

    def test_the_script_never_changes_ownership(self):
        # Abuse case: a chown left in the script fails with EPERM for a
        # non-root user just as it did for capability-less root.
        self.assertNotIn("chown", staging_script())

    def test_the_secret_is_readable_by_the_pod_group(self):
        # The step now reads the Secret as uid 1001 through the pod's
        # fsGroup. Mode 0600 leaves only root able to read it, and the copy
        # fails with EACCES instead.
        volumes = volume_block()
        modes = re.findall(r"defaultMode:\s*(\d+)", volumes)
        self.assertTrue(modes)
        self.assertEqual(set(modes), {"0440"})


class TheStagingScriptCopiesPrivately(unittest.TestCase):
    """Run the script itself as the current user against temp directories."""

    def run_script(self, eck=True):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        raw, out, eckdir = tmp / "raw", tmp / "out", tmp / "eck"
        for d in (raw, out, eckdir):
            d.mkdir()
        (raw / "creds.json").write_text('{"s3": {}}')
        (raw / "es-password").write_text("from-values")
        for f in raw.iterdir():
            os.chmod(f, 0o440)
        if eck:
            (eckdir / "elastic").write_text("eck-generated\n")
        script = (staging_script()
                  .replace("/secrets-raw", str(raw))
                  .replace("/eck-elastic-user", str(eckdir))
                  .replace('"/secrets"', f'"{out}"'))
        run = subprocess.run(
            ["python3", "-c", script],
            env={**os.environ, "PASSWORD_KEY": "es-password"},
            capture_output=True, text=True)
        return run, out

    def test_every_key_is_copied_owner_only(self):
        # The tools refuse a credential file group or others can read; a
        # copy left at 0440 would stop the audit and the loop at start.
        run, out = self.run_script()
        self.assertEqual(run.returncode, 0, run.stderr)
        for name in ("creds.json", "es-password"):
            mode = stat.S_IMODE((out / name).stat().st_mode)
            self.assertEqual(mode, 0o600, name)
            self.assertEqual((out / name).stat().st_uid, os.getuid(), name)

    def test_the_harness_password_comes_from_eck_and_creds_json_does_not(self):
        # The harness must log in with the password ECK generated, and the
        # read-only veto in creds.json must never become the superuser.
        run, out = self.run_script()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual((out / "es-password").read_text(), "eck-generated")
        self.assertEqual((out / "creds.json").read_text(), '{"s3": {}}')


@unittest.skipUnless(HELM, "helm is not installed")
class TheRenderedPodRunsTheStepAsItsUser(unittest.TestCase):

    def test_rendered_qualify_job_has_no_root_container(self):
        # The rendered manifest is what the cluster runs; a template change
        # that left a root override elsewhere would bring the EPERM back.
        out = subprocess.run(
            [HELM, "template", "r", str(CHART),
             "--set", "elasticsearch.external=false",
             "--set", "elasticsearch.eck.disableTls=false",
             "--set-string", "credentials.elasticsearch.apiKey=k",
             "-s", "templates/qualify-job.yaml"],
            capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotRegex(out.stdout, r"runAsUser:\s*0\b")


if __name__ == "__main__":
    unittest.main()
