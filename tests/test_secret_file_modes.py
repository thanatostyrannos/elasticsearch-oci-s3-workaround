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
from generation_chain.credentials import CredentialError, require_private

SECRET = "correct-horse-battery-staple"


class SecretModeCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

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


class TheChurnRigHonoursTheFileRoot(SecretModeCase):
    """GENCHAIN_FILE_ROOT confines what the rig opens, as it does the audit."""

    def test_a_secret_inside_the_root_is_read(self):
        # A scheduled job that sets the root and keeps its secret there must
        # still start; a confinement that refused this would be switched off.
        path = self.secret(0o600)
        with mock.patch.dict(os.environ, {"GENCHAIN_FILE_ROOT": self.dir.name}):
            self.assertEqual(rig.read_secret_file(path, "--password-file"),
                             SECRET)

    def test_a_secret_outside_the_root_is_refused_unread(self):
        # Abuse case: a command line assembled by a wrapper or an agent names
        # a file outside the directory the job was confined to. The rig must
        # refuse before opening it, as generation_chain does.
        path = self.secret(0o600)
        with tempfile.TemporaryDirectory() as elsewhere:
            with mock.patch.dict(os.environ, {"GENCHAIN_FILE_ROOT": elsewhere}):
                with self.assertRaises(SystemExit):
                    rig.read_secret_file(path, "--password-file")

    def test_a_link_inside_the_root_to_a_file_outside_is_refused(self):
        # Abuse case: the check runs on the resolved path, so a symlink placed
        # inside the root cannot carry the read outside it.
        with tempfile.TemporaryDirectory() as root:
            link = os.path.join(root, "pw")
            os.symlink(self.secret(0o600), link)
            with mock.patch.dict(os.environ, {"GENCHAIN_FILE_ROOT": root}):
                with self.assertRaises(SystemExit):
                    rig.read_secret_file(link, "--password-file")


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
