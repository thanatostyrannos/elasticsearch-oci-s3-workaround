"""The qualify Job passes --insecure-http to the loop only when asked.

The chart's MinIO serves plain http inside the cluster, and the audit and
reclaim the loop starts refuse a non-loopback http store unless told it is a
lab store. These checks run `helm template` and skip when helm is absent.
"""

import pathlib
import shutil
import subprocess
import unittest

CHART = (
    pathlib.Path(__file__).resolve().parent.parent
    / "gitlab" / "kubernetes-test-rig" / "chart"
)
HELM = shutil.which("helm")


def qualify_script(*flags):
    out = subprocess.run(
        [HELM, "template", "r", str(CHART), *flags,
         "-s", "templates/qualify-job.yaml"],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return out.stdout


@unittest.skipUnless(HELM, "helm is not installed")
class TheQualifyJobOptsIntoAPlainHttpStoreOnlyWhenAsked(unittest.TestCase):

    def test_the_value_puts_the_flag_on_the_loop(self):
        # Without the flag every cycle against the chart's MinIO fails at the
        # audit, and a whole qualification run reports nothing found.
        rendered = qualify_script("--set", "qualify.insecureHttp=true",
                                  "--set", "qualify.endpoint=http://m:9000")
        self.assertIn('set -- "$@" --insecure-http', rendered)

    def test_abuse_the_default_never_passes_the_flag(self):
        # Abuse: a default that opted in would let the loop send manifests
        # and signed requests to a real store in the clear without anyone
        # choosing that.
        self.assertNotIn("--insecure-http", qualify_script())


if __name__ == "__main__":
    unittest.main()
