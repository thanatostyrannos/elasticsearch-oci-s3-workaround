"""verify_restorable.py removes its probe index however the run ends.

The script restores a snapshot index under the name probe<stamp> on the
cluster being checked. Run as an operator runs it, against a loopback
stand-in for Elasticsearch that records every DELETE it receives.
"""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT = {"snapshot": "snap-1", "state": "SUCCESS", "indices": ["data-1"],
            "start_time_in_millis": 1}


class FakeElasticsearch(http.server.HTTPServer):
    """Answers the handful of calls the script makes and logs the DELETEs."""

    def __init__(self, restore, count=None, delete_status=200):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.restore = restore
        self.count = count if count is not None else {"count": 5}
        self.delete_status = delete_status
        self.deletes = []
        self.drop_when = ()
        # Path, without its query, to the (status, body) it answers with
        # in place of the defaults below, or to a function of the whole
        # request target that returns one. Every request target is kept.
        self.answers = {}
        self.targets = []


class _Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass

    def _send(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _drop(self):
        self.close_connection = True
        self.connection.close()

    def _answer(self):
        path = self.path.split("?")[0]
        self.server.targets.append((self.command, self.path))
        if any(marker in path for marker in self.server.drop_when):
            return self._drop()
        if path in self.server.answers:
            answer = self.server.answers[path]
            if callable(answer):
                answer = answer(self.path)
            return self._send(*answer)
        if self.command == "DELETE":
            self.server.deletes.append(path)
            return self._send(self.server.delete_status, {"acknowledged": True})
        if "/_restore" in path:
            if self.server.restore == "drop":
                return self._drop()
            return self._send(*self.server.restore)
        if path.endswith("/_count"):
            if self.server.count == "drop":
                return self._drop()
            return self._send(200, self.server.count)
        if path == "/_cluster/health":
            return self._send(200, {"status": "green"})
        if path.endswith("/_all"):
            return self._send(200, {"snapshots": [SNAPSHOT]})
        return self._send(200, {})

    do_GET = do_POST = do_DELETE = _answer


def run_script(server):
    """The script's exit code and stdout, against the stand-in."""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".pw") as pw:
            pw.write("secret\n")
            pw.flush()
            done = subprocess.run(
                [sys.executable, os.path.join(ROOT, "verify_restorable.py"),
                 "--elasticsearch", f"http://127.0.0.1:{server.server_port}",
                 "--repository", "repo", "--password-file", pw.name],
                capture_output=True, text=True, timeout=60)
    finally:
        server.shutdown()
        server.server_close()
    return done.returncode, done.stdout


def failed_shards():
    return 200, {"snapshot": {"shards": {"total": 1, "failed": 1}}}


def clean_restore():
    return 200, {"snapshot": {"shards": {"total": 1, "failed": 0}}}


class ProbeIndexIsRemoved(unittest.TestCase):

    def assert_one_probe_delete(self, server):
        self.assertEqual(len(server.deletes), 1, server.deletes)
        self.assertRegex(server.deletes[0], r"^/probe\d+$")

    def test_intact_run_removes_the_probe(self):
        # If the success path stopped deleting, every passing check would
        # leave a full copy of a production index on the cluster.
        server = FakeElasticsearch(clean_restore())
        code, out = run_script(server)
        self.assertEqual(code, 0)
        self.assertIn("INTACT", out)
        self.assert_one_probe_delete(server)

    def test_failed_shards_still_remove_the_probe(self):
        # A restore with failed shards is the case an operator reruns the
        # check on. Without the delete, each rerun strands another probe
        # index until the disk fills, on the cluster that is already ailing.
        server = FakeElasticsearch(failed_shards())
        code, out = run_script(server)
        self.assertEqual(code, 1)
        self.assertIn("FAIL: 1 shard(s) failed to restore", out)
        self.assert_one_probe_delete(server)

    def test_a_dropped_count_request_still_removes_the_probe(self):
        # A connection that dies mid-run raises out of call(). The probe
        # exists by then, and the traceback must not be the last thing that
        # touches the cluster.
        server = FakeElasticsearch(clean_restore(), count="drop")
        code, _ = run_script(server)
        self.assertNotEqual(code, 0)
        self.assert_one_probe_delete(server)

    def test_an_unanswered_restore_still_attempts_the_delete(self):
        # When the restore request times out, Elasticsearch may still be
        # creating the index. Skipping the delete there leaves the one
        # probe nobody knows to look for.
        server = FakeElasticsearch("drop")
        code, _ = run_script(server)
        self.assertNotEqual(code, 0)
        self.assert_one_probe_delete(server)

    def test_a_refused_restore_exits_one_without_a_warning(self):
        # The index never existed, so the delete answers 404. If that
        # printed a warning, every refused restore would send an operator
        # hunting for an index that is not there.
        server = FakeElasticsearch((400, {"error": "no"}))
        server.delete_status = 404
        code, out = run_script(server)
        self.assertEqual(code, 1)
        self.assertIn("FAIL: restore refused", out)
        self.assertNotIn("WARNING", out)


class FailedCleanupDoesNotMaskTheVerdict(unittest.TestCase):

    def test_failed_delete_is_reported_and_exit_code_is_unchanged(self):
        # Abuse: the cluster refuses the delete, as a read-only or
        # overloaded cluster does. The operator must still get the original
        # failure and exit code 1, plus a line saying which index to remove.
        server = FakeElasticsearch(failed_shards(), delete_status=503)
        code, out = run_script(server)
        self.assertEqual(code, 1)
        self.assertIn("FAIL: 1 shard(s) failed to restore", out)
        self.assertRegex(out, r"WARNING: could not delete probe\d+ \(http=503")

    def test_unanswered_restore_names_the_possible_late_index(self):
        # Abuse: the restore request got no answer and the delete is
        # refused too. The warning must say the index may still be
        # arriving, or the operator deletes once, sees success elsewhere,
        # and the late index lands afterwards.
        server = FakeElasticsearch("drop", delete_status=503)
        code, out = run_script(server)
        self.assertNotEqual(code, 0)
        self.assertIn("may still be creating it", out)

    def test_failed_delete_after_an_intact_run_keeps_exit_zero(self):
        # Abuse: a leftover probe is a housekeeping problem, not proof the
        # repository is broken. Turning it into exit 1 would tell the
        # caller to stop on a repository that restored cleanly.
        server = FakeElasticsearch(clean_restore(), delete_status=503)
        code, out = run_script(server)
        self.assertEqual(code, 0)
        self.assertIn("INTACT", out)
        self.assertIn("WARNING: could not delete", out)


if __name__ == "__main__":
    unittest.main()
