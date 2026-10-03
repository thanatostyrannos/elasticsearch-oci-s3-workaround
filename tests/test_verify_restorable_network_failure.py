"""verify_restorable.py reports a network failure before the restore as FAIL.

Run as an operator runs it, against the loopback stand-in from the cleanup
tests, which can drop the connection on a chosen path.
"""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_verify_restorable_cleanup import (  # noqa: E402
    FakeElasticsearch, ROOT, clean_restore, run_script)


class NetworkFailureBeforeTheRestore(unittest.TestCase):

    def assert_clean_fail(self, server, step):
        code, out = run_script(server)
        self.assertEqual(code, 1)
        self.assertRegex(out, rf"FAIL: .*{step}.*(RemoteDisconnected|"
                              r"ConnectionResetError|URLError|BadStatusLine)")
        self.assertNotIn("Traceback", out)

    def test_dropped_health_check_fails_with_a_line(self):
        # A cluster that resets the first request used to end the run in a
        # traceback, which a caller scripting around exit codes cannot tell
        # from a bug in the script itself.
        server = FakeElasticsearch(clean_restore())
        server.drop_when = ("/_cluster/health",)
        self.assert_clean_fail(server, "cluster health")

    def test_dropped_snapshot_listing_fails_with_a_line(self):
        # The listing is the step that names the repository. Without the
        # step in the message the operator cannot tell which call died.
        server = FakeElasticsearch(clean_restore())
        server.drop_when = ("/_all",)
        self.assert_clean_fail(server, "snapshot listing")

    def test_dropped_integrity_check_fails_with_a_line(self):
        # The integrity call is the slowest pre-restore step and the likeliest
        # to meet a proxy timeout.
        server = FakeElasticsearch(clean_restore())
        server.drop_when = ("/_verify_integrity",)
        self.assert_clean_fail(server, "integrity")

    def test_abuse_refused_connection_never_prints_the_credential(self):
        # Abuse: nothing listens on the port. The message names the error
        # class, and the Basic credential and password must not appear in it,
        # or a CI log would publish the cluster password.
        server = FakeElasticsearch(clean_restore())
        port = server.server_port
        server.server_close()
        with tempfile.NamedTemporaryFile("w", suffix=".pw") as pw:
            pw.write("hunter2-secret\n")
            pw.flush()
            done = subprocess.run(
                [sys.executable, os.path.join(ROOT, "verify_restorable.py"),
                 "--elasticsearch", f"http://127.0.0.1:{port}",
                 "--repository", "repo", "--password-file", pw.name],
                capture_output=True, text=True, timeout=60)
        both = done.stdout + done.stderr
        self.assertEqual(done.returncode, 1)
        self.assertIn("FAIL: cluster health", done.stdout)
        self.assertNotIn("Traceback", both)
        self.assertNotIn("hunter2-secret", both)
        self.assertNotIn("Basic ", both)


if __name__ == "__main__":
    unittest.main()
