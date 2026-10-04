"""Secret files are refused when group or others hold any permission bit.

Every other credential file in the repo gets this refusal. The churn rig and
the reclaim protocol read a password and a store key with no check at all, so
a file copied in with scp at 0644 was accepted without a word.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import reclaim_test_protocol as protocol
import snapshot_churn_rig as rig
import snapshot_sizes as sizes
from generation_chain.credentials import CredentialError, require_private

SECRET = "correct-horse-battery-staple"


class SecretModeCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        # These cases test the mode check, so the secret root is the
        # directory the secrets are written to.
        patcher = mock.patch.dict(
            os.environ, {"GENCHAIN_SECRET_ROOT": self.dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def secret(self, mode):
        path = os.path.join(self.dir.name, "secret")
        with open(path, "w") as handle:
            handle.write(SECRET + "\n")
        os.chmod(path, mode)
        return path


class TheChurnRigRefusesAGroupOrWorldReadableSecret(SecretModeCase):
    def read(self, mode):
        return rig.read_secret_file(self.secret(mode), "--password-file")

    def test_a_0600_file_is_read(self):
        # If this failed, the rig could not start in the chart, which stages
        # every secret at 0600.
        self.assertEqual(self.read(0o600), SECRET)

    def test_a_0400_file_is_read(self):
        # If this failed, the refusal's own advice to tighten the mode would
        # lead to a second refusal.
        self.assertEqual(self.read(0o400), SECRET)

    def test_a_0644_file_is_refused_unread(self):
        # A credential copied in by scp stays readable by every local user.
        with self.assertRaises(SystemExit) as raised:
            self.read(0o644)
        self.assertEqual(raised.exception.code, 2)

    def test_a_0640_file_is_refused_unread(self):
        # Group read is still another user on the host holding the password.
        with self.assertRaises(SystemExit):
            self.read(0o640)

    def test_a_group_write_only_bit_is_refused(self):
        # The check is every group and other bit, not only the read bits.
        with self.assertRaises(SystemExit):
            self.read(0o620)

    def test_the_refusal_names_the_flag_and_never_the_contents(self):
        # The refusal reaches logs. The secret must not.
        import io
        import contextlib
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured), \
                contextlib.redirect_stdout(captured):
            with self.assertRaises(SystemExit):
                self.read(0o644)
        self.assertIn("--password-file", captured.getvalue())
        self.assertNotIn(SECRET, captured.getvalue())

    def test_a_loose_file_behind_a_symlink_is_refused(self):
        # The mode of the file that would be read decides, not the mode of
        # the link, which is always 0777.
        target = self.secret(0o644)
        link = os.path.join(self.dir.name, "link")
        os.symlink(target, link)
        with self.assertRaises(SystemExit):
            rig.read_secret_file(link, "--password-file")


class TheChurnRigConfinesSecretFiles(SecretModeCase):
    """A secret file must sit under the current directory or the secret root."""

    def setUp(self):
        super().setUp()
        self.other = tempfile.TemporaryDirectory()
        self.addCleanup(self.other.cleanup)

    def unset_root(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("GENCHAIN_SECRET_ROOT", None)

    def refusal(self, path):
        import io
        import contextlib
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured), \
                contextlib.redirect_stdout(captured):
            with self.assertRaises(SystemExit) as raised:
                rig.read_secret_file(path, "--password-file")
        self.assertEqual(raised.exception.code, 2)
        return captured.getvalue()

    def test_a_secret_in_the_current_directory_is_read(self):
        # The default for an operator who runs the rig by hand from the
        # directory holding pw.txt. If this failed, every manual run would
        # need an environment variable first.
        self.unset_root()
        path = self.secret(0o600)
        with mock.patch("os.getcwd", return_value=self.dir.name):
            self.assertEqual(rig.read_secret_file(path, "--password-file"),
                             SECRET)

    def test_a_secret_elsewhere_is_refused_unread(self):
        # Abuse case: an agent that builds the command line from untrusted
        # text passes --password-file ~/.ssh/id_rsa. The key is mode 0600, so
        # only the root check stops it being sent as the password.
        self.unset_root()
        path = self.secret(0o600)
        with mock.patch("os.getcwd", return_value=self.other.name):
            text = self.refusal(path)
        self.assertIn("GENCHAIN_SECRET_ROOT", text)
        self.assertIn(os.path.realpath(self.other.name), text)

    def test_the_secret_root_variable_makes_that_directory_readable(self):
        # The chart stages secrets under /secrets while the working directory
        # is the source checkout. If this failed, every rig Job would exit 2.
        path = self.secret(0o600)
        with mock.patch("os.getcwd", return_value=self.other.name):
            self.assertEqual(rig.read_secret_file(path, "--password-file"),
                             SECRET)

    def test_an_empty_secret_root_variable_falls_back_to_the_directory(self):
        # Abuse case: a Job template that renders the variable as an empty
        # string must not turn the confinement off.
        path = self.secret(0o600)
        with mock.patch.dict(os.environ, {"GENCHAIN_SECRET_ROOT": " "}), \
                mock.patch("os.getcwd", return_value=self.other.name):
            self.refusal(path)

    def test_a_link_inside_the_root_to_a_file_outside_is_refused(self):
        # Abuse case: the check runs on the resolved path, so a symlink put
        # inside the root cannot carry the read to a key outside it.
        link = os.path.join(self.dir.name, "pw")
        with open(os.path.join(self.other.name, "key"), "w") as handle:
            handle.write(SECRET)
        os.chmod(handle.name, 0o600)
        os.symlink(handle.name, link)
        self.refusal(link)

    def test_a_sibling_directory_sharing_the_root_prefix_is_refused(self):
        # Abuse case: /secrets-old starts with the text /secrets. A string
        # prefix test would let it through; the path test must not.
        sibling = self.dir.name + "-old"
        os.mkdir(sibling)
        self.addCleanup(os.rmdir, sibling)
        path = os.path.join(sibling, "pw")
        with open(path, "w") as handle:
            handle.write(SECRET)
        os.chmod(path, 0o600)
        self.addCleanup(os.remove, path)
        self.refusal(path)

    def test_the_refusal_never_contains_the_secret(self):
        # The refusal reaches job logs, and the file holds the credential.
        path = self.secret(0o600)
        with mock.patch.dict(os.environ, {
                "GENCHAIN_SECRET_ROOT": self.other.name}):
            self.assertNotIn(SECRET, self.refusal(path))

    def test_a_state_file_outside_the_secret_root_is_still_read(self):
        # The state file lives on its own volume, not under /secrets. If the
        # confinement reached it, no rig could tear down its own state.
        path = os.path.join(self.other.name, "rig-state.json")
        with open(path, "w") as handle:
            handle.write("{}")
        self.assertEqual(
            rig.resolve_input_file(path, "--state-file"),
            os.path.realpath(path))


class TheSecretRootCheckRaises(SecretModeCase):
    """The containment check raises instead of exiting, in both scripts."""

    def test_the_rig_check_raises_outside_the_root(self):
        # SonarQube's path-traversal analysis only treats the check as a
        # guard when it raises; a check that calls die() reads to it as
        # falling through to open(), and the security rating drops to C.
        outside = self.secret(0o600)
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"GENCHAIN_SECRET_ROOT": root}):
                with self.assertRaises(rig.SecretPathRefused):
                    rig.confined_secret_path(outside, "--password-file")

    def test_the_sizes_check_raises_outside_the_root(self):
        # Same guard in snapshot_sizes.py, for the same reason.
        outside = self.secret(0o600)
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"GENCHAIN_SECRET_ROOT": root}):
                with self.assertRaises(sizes.SecretPathRefused):
                    sizes.confined_secret_path(outside, "--password-file")

    def test_a_path_inside_the_root_is_returned_resolved(self):
        # Use case: the check must hand back the path it checked, or the
        # caller would open something else.
        inside = self.secret(0o600)
        with mock.patch.dict(os.environ,
                             {"GENCHAIN_SECRET_ROOT": self.dir.name}):
            self.assertEqual(sizes.confined_secret_path(inside, "--pw"),
                             os.path.realpath(inside))


class TheReclaimProtocolRefusesAGroupOrWorldReadableSecret(SecretModeCase):
    def read(self, mode):
        return protocol.read_secret_file(self.secret(mode),
                                         "--es-password-file")

    def test_a_0600_file_is_read(self):
        # If this failed, the qualify Job could not start in the chart.
        self.assertEqual(self.read(0o600), SECRET)

    def test_a_0644_file_is_refused(self):
        # A scp'd password readable by every local user must not be accepted.
        with self.assertRaises(ValueError):
            self.read(0o644)

    def test_a_0640_file_is_refused(self):
        # Group read is another user on the host holding the password.
        with self.assertRaises(ValueError):
            self.read(0o640)

    def test_the_refusal_never_quotes_the_contents(self):
        # The refusal is printed by argparse into logs.
        with self.assertRaises(ValueError) as raised:
            self.read(0o644)
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertIn("--es-password-file", str(raised.exception))


class RequirePrivateAdmitsExactlyTheFilesItsMessageDescribes(SecretModeCase):
    def test_0600_and_0400_pass(self):
        # The two modes the remedy text tells an operator to use.
        require_private(self.secret(0o600))
        require_private(self.secret(0o400))

    def test_0644_and_0640_are_refused(self):
        # Group and world access is what the check exists to stop.
        for mode in (0o644, 0o640):
            with self.assertRaises(CredentialError):
                require_private(self.secret(mode))

    def test_the_message_states_the_rule_the_check_applies(self):
        # The old text promised only 0600 or 0400 while the check let 0700
        # through, so an operator trusted a guarantee that was not there.
        with self.assertRaises(CredentialError) as raised:
            require_private(self.secret(0o640))
        message = str(raised.exception)
        self.assertIn("group or other", message)
        self.assertNotIn("0600 or", message)

    def test_an_owner_only_execute_mode_is_not_described_as_refused(self):
        # 0700 passes, as it always has. The message must not claim
        # otherwise, so this pins the check as bits-based.
        require_private(self.secret(0o700))


if __name__ == "__main__":
    unittest.main()
