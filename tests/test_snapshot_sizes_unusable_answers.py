"""snapshot_sizes.py reports a cluster answer it cannot use, with no traceback."""

import contextlib
import http.server
import io
import json
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import snapshot_sizes as sizes

LISTING = {"snapshots": [{"snapshot": "snap-1"}, {"snapshot": "snap-2"}]}
STATUS = {"snapshots": [
    {"snapshot": "snap-1", "state": "SUCCESS", "stats": {
        "start_time_in_millis": 1000,
        "incremental": {"size_in_bytes": 10}, "total": {"size_in_bytes": 20}}},
    {"snapshot": "snap-2", "state": "SUCCESS", "stats": {
        "start_time_in_millis": 2000,
        "incremental": {"size_in_bytes": 30}, "total": {"size_in_bytes": 40}}},
]}
HTML = b"<html><body>502 Bad Gateway</body></html>"


def route(path):
    if "_status" in path:
        return "status"
    if "verbose=false" in path:
        return "listing"
    if "_settings" in path:
        return "settings"
    return "slm"


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = self.server.answers[route(self.path)]
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def as_json(value):
    return json.dumps(value).encode()


class _Cluster(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.answers = {
            "listing": as_json(LISTING), "status": as_json(STATUS),
            "settings": as_json({}), "slm": as_json({"snapshots": []}),
        }
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def run_tool(self, *extra):
        argv = ["snapshot_sizes.py", "--es",
                f"http://127.0.0.1:{self.server.server_port}",
                "--repo", "r", *extra]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = sizes.main()
        return code, out.getvalue(), err.getvalue()

    def assert_reported(self, answers, message, *extra):
        self.server.answers.update(answers)
        code, _out, err = self.run_tool(*extra)
        self.assertEqual(code, 1)
        self.assertIn(message, err)
        self.assertNotIn("Traceback", err)


class NormalAnswers(_Cluster):
    def test_a_normal_listing_and_status_still_produce_the_report(self):
        # Breaks if the new shape checks reject what a real cluster sends,
        # which would stop every sizing report and every classified export.
        code, out, _err = self.run_tool("--emit-classified")
        self.assertEqual(code, 0)
        self.assertIn("snap-1", out)


class UnusableListing(_Cluster):
    MESSAGE = "snapshot listing"

    def test_a_non_json_200_body_is_reported(self):
        # Abuse: a proxy in front of the cluster answers 200 with an HTML
        # error page. The operator got a JSONDecodeError traceback instead of
        # a line saying the cluster did not answer with a listing.
        self.assert_reported({"listing": HTML}, self.MESSAGE)

    def test_a_json_string_is_reported(self):
        # Abuse: a gateway answers with a bare JSON string such as an error
        # text. The tool died on listing.get with an AttributeError.
        self.assert_reported({"listing": as_json("overloaded")},
                             self.MESSAGE)

    def test_an_object_without_a_snapshot_list_is_reported(self):
        # Abuse: an error object such as {"error": ...} read as a repository
        # with no snapshots would print an empty report that looks healthy.
        self.assert_reported({"listing": as_json({"error": "boom"})},
                             self.MESSAGE)

    def test_a_listing_entry_without_a_name_is_reported(self):
        # Abuse: an entry that is not an object with a snapshot name raised
        # KeyError and left the operator with a traceback.
        self.assert_reported({"listing": as_json({"snapshots": [{}]})},
                             self.MESSAGE)

    def test_the_export_reports_it_too(self):
        # --emit-classified is the mode that writes a file another tool
        # reads. A proxy page must end it with a message, not a traceback.
        self.assert_reported({"listing": HTML}, self.MESSAGE,
                             "--emit-classified")


class UnusableStatus(_Cluster):
    MESSAGE = "_status"

    def test_a_non_json_200_body_is_reported(self):
        # Abuse: a proxy times out mid-run and answers a later _status batch
        # with an HTML page. The run ended in a traceback with no hint which
        # batch failed or that partial results were thrown away.
        self.assert_reported({"status": HTML}, self.MESSAGE)

    def test_a_json_string_is_reported(self):
        # Abuse: a bare JSON string from a gateway failed on st.get.
        self.assert_reported({"status": as_json("overloaded")}, self.MESSAGE)

    def test_an_object_without_a_snapshot_list_is_reported(self):
        # Abuse: reading an error object as zero snapshots would drop the
        # batch silently and size the repository from the rest.
        self.assert_reported({"status": as_json({"error": "boom"})},
                             self.MESSAGE)

    def test_the_export_reports_it_too(self):
        # --emit-classified fetches status through the same function, and a
        # half-written export would be read as the whole repository.
        self.assert_reported({"status": HTML}, self.MESSAGE,
                             "--emit-classified")


class UnusableDiscovery(_Cluster):
    def test_a_settings_answer_that_is_a_list_is_reported(self):
        # Abuse: mounted-index discovery on a non-object answer raised
        # AttributeError, and a swallowed one would classify every pinned
        # snapshot as a plain backup.
        self.assert_reported({"settings": as_json([1])}, "_settings",
                             "--emit-classified")

    def test_a_settings_entry_that_is_a_string_is_reported(self):
        # Abuse: one index whose settings body is a string made the mount
        # scan raise AttributeError partway through the indices.
        self.assert_reported({"settings": as_json({"idx": "x"})}, "_settings",
                             "--emit-classified")

    def test_a_slm_answer_that_is_a_string_is_reported(self):
        # Abuse: the SLM policy fetch on a JSON string raised AttributeError.
        self.assert_reported({"slm": as_json("nope")}, "SLM",
                             "--emit-classified")


if __name__ == "__main__":
    unittest.main()
