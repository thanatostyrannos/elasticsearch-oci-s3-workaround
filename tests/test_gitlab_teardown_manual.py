#!/usr/bin/env python3
"""Static checks on the teardown:manual job in the GitLab test rig pipeline.

The job is the safety net for a rig whose automatic teardown did not finish.
A safety net nobody ever ran is where defects hide, so these tests read the
pipeline file as text and pin the two decisions that keep it working.
"""
import re
import unittest
from pathlib import Path

CI_FILE = Path(__file__).resolve().parent.parent / "gitlab/kubernetes-test-rig/.gitlab-ci.yml"


def manual_job_script():
    text = CI_FILE.read_text()
    match = re.search(r"^teardown:manual:\n(.*?)(?=^\S)", text + "\nend:\n", re.S | re.M)
    assert match, "teardown:manual job not found"
    return match.group(1)


def hardcodes_rendered_name(script):
    return "test-rig-teardown-manual" in script or "rig-component=teardown-manual" not in script


def creates_before_uninstall(script):
    return script.index("create job") < script.index("helm uninstall")


def missing_template_exits_nonzero(script):
    guard = script.index('[ -z "$template_job" ]')
    return "exit 1" in script[guard:script.index("fi", guard)]


def rig_after_script():
    text = CI_FILE.read_text()
    match = re.search(r"^teardown:rig:\n(.*?)(?=^\S)", text + "\nend:\n", re.S | re.M)
    assert match, "teardown:rig job not found"
    body = match.group(1)
    return body[body.index("after_script:"):body.rindex("rules:")]


def absent_template_exits_with_remedy(script):
    guard = script.index('[ -z "$template_job" ]')
    branch = script[guard:script.index("\n        fi", guard)]
    return ("exit 1" in branch and "Nothing was torn down" in branch
            and "teardown.standalone.enabled=true" in branch
            and "snapshot_churn_rig.py teardown" in branch)


def retry_result_is_reported(script):
    create_lines = [line for line in script.splitlines() if "create job" in line
                    or "--from=" in line]
    return (bool(create_lines) and "kubectl -n \"$KUBE_NAMESPACE\" wait" in script
            and not any("|| true" in line for line in create_lines))


class TeardownRigAfterScriptTests(unittest.TestCase):
    def test_absent_template_fails_and_names_the_remedy(self):
        # With the template Job gone the retry silently did nothing, and
        # the operator believed the rig was cleaned up while the load
        # generator kept writing to a live bucket.
        self.assertTrue(absent_template_exits_with_remedy(rig_after_script()))

    def test_abuse_silent_skip_is_detected(self):
        # Models the original `if [ -n "$template_job" ]` shape, which
        # prints nothing when the Job is absent.
        bad = ('if [ -z "$template_job" ]; then\n  echo x\n'
               '        fi')
        self.assertFalse(absent_template_exits_with_remedy(bad))

    def test_retry_job_is_waited_on_and_not_swallowed(self):
        # `create job || true` reported a retry that might still be
        # running, or might have failed, as if teardown had finished.
        self.assertTrue(retry_result_is_reported(rig_after_script()))

    def test_abuse_fire_and_forget_retry_is_detected(self):
        # Models the original create-and-ignore line.
        bad = 'kubectl create job --from=job/x y || true\necho later'
        self.assertFalse(retry_result_is_reported(bad))


class TeardownManualTests(unittest.TestCase):
    def test_finds_template_by_label_not_rendered_name(self):
        # The template Job's name is <release>-<nameOverride>-teardown-manual
        # and nameOverride is a values.yaml knob. A hardcoded name was wrong
        # on day one and made every manual teardown fail at the copy step,
        # leaving the load generator running against a live bucket.
        self.assertFalse(hardcodes_rendered_name(manual_job_script()))

    def test_abuse_hardcoded_name_is_detected(self):
        # Models the original defect: a rendered chart name pasted into
        # --from. Proves the check can fail, so it cannot pass vacuously
        # after a refactor.
        bad = 'kubectl create job --from="job/${HELM_RELEASE}-elasticsearch-oci-s3-workaround-test-rig-teardown-manual" x'
        self.assertTrue(hardcodes_rendered_name(bad))

    def test_teardown_job_runs_before_helm_uninstall(self):
        # helm uninstall deletes the suspended template Job, the state
        # volume and the credentials the teardown Job reads. Uninstalling
        # first leaves nothing to copy and nothing to run.
        self.assertTrue(creates_before_uninstall(manual_job_script()))

    def test_abuse_uninstall_first_order_is_detected(self):
        # Models the original order, uninstall then create. The ordering
        # check must report it as wrong.
        bad = "helm uninstall rig || true\nkubectl create job --from=job/x y"
        self.assertFalse(creates_before_uninstall(bad))

    def test_missing_template_exits_nonzero(self):
        # With the template gone, reporting success would tell an operator
        # the rig was torn down when nothing ran.
        self.assertTrue(missing_template_exits_nonzero(manual_job_script()))

    def test_abuse_guard_without_exit_is_detected(self):
        # Models a refactor that keeps the message but drops the exit.
        bad = 'if [ -z "$template_job" ]; then\n  echo gone\nfi'
        self.assertFalse(missing_template_exits_nonzero(bad))


if __name__ == "__main__":
    unittest.main()
