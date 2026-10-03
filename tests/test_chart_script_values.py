"""The test-rig chart must not paste values into shell text, and its clone step
must work for a branch, a tag or a commit and must not trust an unknown SSH host.

The static checks read the template text. The clone checks run the real clone
snippet from the helper template against a bare repository in a temp directory.
The render checks need helm and skip without it.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

CHART = Path(__file__).resolve().parent.parent / "gitlab" / "kubernetes-test-rig" / "chart"
TEMPLATES = sorted((CHART / "templates").glob("*"))
HELPERS = (CHART / "templates" / "_helpers.tpl").read_text()
DIRECTIVE = re.compile(r"\{\{-?\s*(?:if|else|end|with)\b[^}]*\}\}|\{\{/\*.*?\*/\}\}")


def script_blocks(text):
    """Yield (line number, body lines) for every `- |` literal block."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\s*- \|\s*$", line):
            indent = len(line) - len(line.lstrip())
            body = []
            for later in lines[i + 1:]:
                if later.strip() and len(later) - len(later.lstrip()) <= indent:
                    break
                body.append(later)
            yield i + 1, body


def value_lines(body):
    """Lines of a script that print a template value, not just branch on one."""
    return [l for l in body if "{{" in DIRECTIVE.sub("", l)]


def clone_script(with_ssh=False):
    start = HELPERS.index('- name: clone-source')
    block = next(b for _, b in script_blocks(HELPERS[start:]))
    keep = []
    skipping = False
    for line in block:
        tag = line.strip()
        if tag.startswith("{{- if .Values.source.existingSshSecret"):
            skipping = not with_ssh
            continue
        if skipping and tag.startswith("{{- end"):
            skipping = False
            continue
        if not skipping and not tag.startswith("{{"):
            keep.append(line)
    return re.sub(r"^ {6}", "", "\n".join(keep), flags=re.M)


class NoValuesInShellText(unittest.TestCase):

    def test_no_template_value_inside_any_script_body(self):
        # A value pasted into script text is parsed by the shell, so a bucket
        # name like x$(cmd) runs cmd in the pod. Values must arrive through env
        # entries and be read as "$VAR".
        found = []
        for path in TEMPLATES:
            for lineno, body in script_blocks(path.read_text()):
                found += [f"{path.name}:{lineno}: {l.strip()}" for l in value_lines(body)]
        self.assertEqual(found, [])

    def test_abuse_checker_flags_a_pasted_value(self):
        # Models the original defect so the check above cannot pass vacuously
        # if the block parser stops finding scripts.
        bad = 'args="$args --bucket {{ .Values.auditCronJob.bucket }}"'
        self.assertEqual(value_lines([bad, "{{- if .Values.x }}"]), [bad])
        text = "  args:\n    - |\n      echo {{ .Values.a }}\n  next: 1\n"
        self.assertEqual(len(list(script_blocks(text))), 1)

    def test_every_script_block_is_found(self):
        # If the parser silently found no blocks, the first test would pass on
        # any template. The chart has scripts in at least these files.
        names = {p.name for p in TEMPLATES if list(script_blocks(p.read_text()))}
        self.assertTrue({"_helpers.tpl", "audit-cronjob.yaml", "qualify-job.yaml",
                         "minio-bucket-job.yaml"} <= names)


class CloneSnippet(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        self.env = env
        work = self.tmp / "work"
        self.git(self.tmp, "init", "-q", "-b", "main", str(work))
        (work / "f").write_text("one")
        self.git(work, "add", "f")
        self.git(work, "commit", "-qm", "one")
        self.first = self.git(work, "rev-parse", "HEAD")
        self.git(work, "tag", "v1")
        self.git(work, "checkout", "-qb", "side")
        (work / "f").write_text("side")
        self.git(work, "commit", "-qam", "side")
        self.git(work, "checkout", "-q", "main")
        (work / "f").write_text("two")
        self.git(work, "commit", "-qam", "two")
        self.second = self.git(work, "rev-parse", "HEAD")
        self.bare = self.tmp / "bare.git"
        self.git(self.tmp, "clone", "-q", "--bare", str(work), str(self.bare))

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def clone(self, ref, script=None):
        dest = self.tmp / "ws"
        shutil.rmtree(dest, ignore_errors=True)
        script = (script or clone_script()).replace("/workspace", str(dest))
        run = subprocess.run(["sh", "-c", script], env={**self.env, "REPO_URL": str(self.bare),
                             "REPO_REF": ref}, capture_output=True, text=True)
        return run, dest

    def test_full_commit_sha_checks_out_that_commit(self):
        # source.ref is documented as the way to pin the code a run executes.
        # git clone --branch <sha> is refused, so a pinned rig would not start.
        run, dest = self.clone(self.first)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.git(dest, "rev-parse", "HEAD"), self.first)
        self.assertEqual((dest / "f").read_text(), "one")

    def test_branch_tag_and_default_ref_still_work(self):
        # The default ref is main and operators use tags; the SHA support must
        # not break the refs that worked before.
        for ref, want in (("main", "two"), ("side", "side"), ("v1", "one")):
            run, dest = self.clone(ref)
            self.assertEqual(run.returncode, 0, (ref, run.stderr))
            self.assertEqual((dest / "f").read_text(), want, ref)

    def test_unknown_ref_fails_the_init_container(self):
        # Abuse case: a mistyped SHA or deleted branch. The pod must stop, not
        # run whatever the default branch holds.
        run, _ = self.clone("0" * 40)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("not a branch, tag or commit", run.stderr)

    def test_ref_that_looks_like_an_option_is_refused(self):
        # Abuse case: a ref such as --upload-pack=cmd must never reach git as
        # an option.
        run, _ = self.clone("--upload-pack=touch /tmp/never")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("must not start with a dash", run.stderr)

    def test_ref_with_shell_syntax_is_data(self):
        # Abuse case: the ref is read from env, so command substitution in it
        # is a ref name that does not exist, not a command that runs.
        marker = self.tmp / "ran"
        run, _ = self.clone(f"$(touch {marker})")
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(marker.exists())


class SshHostKeys(unittest.TestCase):

    def test_no_trust_on_first_use(self):
        # ssh-keyscan at pod start trusts whatever answers on the network at
        # that moment, so a man in the middle supplies the key the clone
        # trusts. Known hosts must come from values.
        # The one mention allowed is the render error that tells the operator
        # how to produce a known_hosts value.
        for path in TEMPLATES:
            for line in path.read_text().splitlines():
                if "ssh-keyscan" in line:
                    self.assertIn("fail ", line, f"{path.name}: {line.strip()}")

    def test_ssh_script_pins_known_hosts_and_strict_checking(self):
        # Abuse case: the script falls back to accept-new or an empty
        # known_hosts. Strict checking against the supplied file is what makes
        # an unknown host a failure.
        script = clone_script(with_ssh=True)
        self.assertIn("StrictHostKeyChecking=yes", script)
        self.assertIn('"$SSH_KNOWN_HOSTS" > /root/.ssh/known_hosts', script)
        self.assertIn("SSH_KNOWN_HOSTS:?", script)
        self.assertNotIn("StrictHostKeyChecking=no", script)
        self.assertNotIn("accept-new", script)

    def test_ssh_script_exits_when_known_hosts_is_empty(self):
        # Abuse case: the render guard is bypassed (helm template of an old
        # release, a patched manifest). The pod must still refuse.
        script = clone_script(with_ssh=True)
        guard = [l for l in script.splitlines() if "SSH_KNOWN_HOSTS:?" in l][0]
        run = subprocess.run(["sh", "-c", "set -eu\n" + guard], env={"PATH": os.environ["PATH"]},
                             capture_output=True, text=True)
        self.assertNotEqual(run.returncode, 0)


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class Rendered(unittest.TestCase):

    def render(self, *sets):
        args = ["helm", "template", "r", str(CHART)]
        for s in sets:
            args += ["--set-string", s]
        return subprocess.run(args, capture_output=True, text=True)

    def test_ssh_without_known_hosts_fails_the_render(self):
        # An SSH source with no host key must not render a pod that guesses.
        run = self.render("source.existingSshSecret=k")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("sshKnownHosts", run.stderr)

    def test_ssh_with_known_hosts_renders_the_value_as_env(self):
        # The host key list reaches the pod as data, never as script text.
        run = self.render("source.existingSshSecret=k", "source.sshKnownHosts=h ssh-ed25519 AAAA")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("name: SSH_KNOWN_HOSTS", run.stdout)
        self.assertNotIn("ssh-keyscan", run.stdout)

    def test_hostile_values_render_only_into_env(self):
        # Abuse case from the review: a $(...) bucket name. It may appear as an
        # env value and must not appear in any script.
        run = self.render("auditCronJob.enabled=true", "qualify.enabled=true", "minio.enabled=true",
                          "auditCronJob.bucket=x$(touch /tmp/pwn)", "qualify.bucket=y`id`",
                          "minio.bucket=z$(id)")
        self.assertEqual(run.returncode, 0, run.stderr)
        for line in run.stdout.splitlines():
            if "touch /tmp/pwn" in line or "`id`" in line or "$(id)" in line:
                self.assertRegex(line, r"^\s*value: ")


if __name__ == "__main__":
    unittest.main()
