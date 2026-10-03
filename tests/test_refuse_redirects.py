"""No client follows a redirect, because a redirect carries the credential.

Each case points a client at a loopback server that answers 302 to a second
loopback port, and the second port must see no request at all. Each client
also still reads a plain 200, so a refusal that broke the normal path would
show up here too.
"""

import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generation_chain.corroboration import (CorroborationUnavailable,
                                            Credentials, ElasticsearchVeto)
from generation_chain.errors import SourceReadError
from generation_chain.reclaim.transport import (TransportError, fetch_object,
                                               send_batch_delete)
from generation_chain.redirects import RedirectRefused
from generation_chain.sources.http_reads import HttpReader
from generation_chain.sources.s3 import S3Credentials

import reclaim_test_protocol as protocol
import snapshot_churn_rig as rig
import snapshot_sizes as sizes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET = "SECRETKEY123"


class _Recorder(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.server.seen.append((self.command, self.path,
                                 self.headers.get("Authorization")))
        target = self.server.redirect_to
        if target:
            self.send_response(self.server.status)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        raw = self.server.body
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_DELETE = _answer


class _Server(http.server.HTTPServer):
    def __init__(self, redirect_to=None, body=b"{}", status=302):
        super().__init__(("127.0.0.1", 0), _Recorder)
        self.redirect_to = redirect_to
        self.body = body
        self.status = status
        self.seen = []

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}"


class _Pair(unittest.TestCase):
    """`self.first` redirects to `self.second`, which must stay silent."""

    status = 302

    def setUp(self):
        self.second = _Server()
        self.first = _Server(redirect_to=self.second.url + "/stolen?sig=1",
                             status=self.status)
        self.servers = [self.first, self.second]
        for server in self.servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def assert_nothing_followed(self, message):
        # A client that follows the redirect hands the second host the
        # Authorization header, which for Elasticsearch is a reusable secret.
        self.assertEqual(self.second.seen, [])
        self.assertEqual(len(self.first.seen), 1, "a refused redirect is not retried")
        self.assertIn(str(self.status), message)
        self.assertIn(f"127.0.0.1:{self.second.server_port}", message)
        self.assertNotIn(SECRET, message)
        self.assertNotIn("stolen", message)


class StoreReaderRefusesRedirects(_Pair):

    def test_a_redirect_is_a_read_failure_and_is_not_followed(self):
        # If the reader followed, the SigV4 Authorization would reach
        # whatever host a proxy or a hostile store names.
        slept = []
        reader = HttpReader(sleep=slept.append)
        with self.assertRaises(SourceReadError) as raised:
            reader.get(self.first.url + "/k", {"Authorization": SECRET})
        self.assert_nothing_followed(str(raised.exception))
        self.assertEqual(slept, [], "a redirect must not be retried")

    def test_every_redirect_status_is_refused(self):
        # 307 and 308 keep the method and the headers, so they are the ones
        # a careless allowlist of 301 and 302 would let through.
        for status in (301, 303, 307, 308):
            with self.subTest(status=status):
                self.first.status = status
                self.first.seen.clear()
                with self.assertRaises(SourceReadError) as raised:
                    HttpReader(sleep=lambda _s: None).get(
                        self.first.url + "/k", {"Authorization": SECRET})
                self.assertIn(str(status), str(raised.exception))
                self.assertEqual(self.second.seen, [])

    def test_a_plain_answer_is_still_read(self):
        # The refusal must not break the normal path for every read.
        self.second.body = b"payload"
        response = HttpReader().get(self.second.url + "/k", {})
        self.assertEqual(response.body, b"payload")


class ElasticsearchClientRefusesRedirects(_Pair):

    def _veto(self, server):
        return ElasticsearchVeto(server.url, "repo",
                                 Credentials(api_key=SECRET))

    def test_a_redirect_is_unavailable_and_not_followed(self):
        # The ApiKey is a bearer secret good until revoked. Following the
        # redirect would give it to a third host.
        with self.assertRaises(CorroborationUnavailable) as raised:
            self._veto(self.first)._get("/_snapshot/repo/_all")
        self.assert_nothing_followed(str(raised.exception))
        self.assertFalse(raised.exception.transient,
                         "a scheduler must not retry a redirecting endpoint")

    def test_a_plain_answer_is_still_read(self):
        self.second.body = b'{"snapshots": []}'
        self.assertEqual(
            self._veto(self.second)._get("/_snapshot/repo/_all"),
            {"snapshots": []})


class ReclaimPostRefusesRedirects(_Pair):

    def _send(self, server):
        return send_batch_delete(
            scheme="http", host=f"127.0.0.1:{server.server_port}",
            region="us-east-1", bucket="bucket",
            credentials=S3Credentials("AKIAEXAMPLE", SECRET),
            body=b"<Delete/>", checksum=("x-amz-checksum-crc32", "AAAAAA=="),
            timeout=5.0, sleep=lambda _s: None, jitter=lambda: 0.0)

    def test_a_redirect_is_a_transport_error_and_is_not_followed(self):
        # Python re-sends a redirected POST as a GET with the signed headers.
        # That leaks the signature and ends in an unsigned request nobody
        # approved.
        with self.assertRaises(TransportError) as raised:
            self._send(self.first)
        self.assert_nothing_followed(str(raised.exception))

    def test_a_plain_answer_is_still_returned(self):
        self.second.body = b"<DeleteResult/>"
        self.assertEqual(self._send(self.second), b"<DeleteResult/>")


class ReclaimTargetReadRefusesRedirects(_Pair):

    def _fetch(self, server):
        return fetch_object(
            scheme="http", host=f"127.0.0.1:{server.server_port}",
            region="us-east-1", bucket="bucket", key="index.latest",
            credentials=S3Credentials("AKIAEXAMPLE", SECRET), timeout=5.0,
            sleep=lambda _s: None, jitter=lambda: 0.0)

    def test_a_redirect_is_a_transport_error_and_is_not_followed(self):
        # This GET decides which repository the delete goes to. Following a
        # redirect would hand the signature to another host and let that
        # host answer the identity check for a store it is not.
        with self.assertRaises(TransportError) as raised:
            self._fetch(self.first)
        self.assert_nothing_followed(str(raised.exception))

    def test_a_plain_answer_is_still_returned(self):
        self.second.body = b"\x00" * 8
        self.assertEqual(self._fetch(self.second), b"\x00" * 8)


class ReclaimHarnessRefusesRedirects(_Pair):

    def _args(self, server):
        return types.SimpleNamespace(elasticsearch=server.url, es_user="u",
                                     es_password=SECRET)

    def test_es_call_does_not_follow_a_redirect(self):
        # The harness sends the Elasticsearch Basic credential on every call.
        with self.assertRaises(RedirectRefused) as raised:
            protocol.es_call(self._args(self.first), "/_snapshot")
        self.assert_nothing_followed(str(raised.exception))

    def test_a_plain_answer_is_still_read(self):
        self.second.body = b'{"ok": true}'
        self.assertEqual(protocol.es_call(self._args(self.second), "/x"),
                         {"ok": True})


class VerifyRestorableRefusesRedirects(_Pair):

    def _run(self, server):
        with tempfile.NamedTemporaryFile("w", suffix=".pw") as pw:
            pw.write(SECRET + "\n")
            pw.flush()
            return subprocess.run(
                [sys.executable, os.path.join(ROOT, "verify_restorable.py"),
                 "--elasticsearch", server.url, "--repository", "repo",
                 "--password-file", pw.name],
                capture_output=True, text=True, timeout=60)

    def test_the_script_stops_and_the_redirect_target_sees_nothing(self):
        # The script sends the Elasticsearch Basic credential on every call,
        # and its first call is the one a redirecting endpoint would catch.
        done = self._run(self.first)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.second.seen, [])
        self.assertIn("302", done.stdout + done.stderr)
        self.assertIn(f"127.0.0.1:{self.second.server_port}",
                      done.stdout + done.stderr)
        self.assertNotIn(SECRET, done.stdout + done.stderr)

    def test_a_plain_answer_is_still_read(self):
        self.second.body = json.dumps({"status": "green"}).encode()
        done = self._run(self.second)
        self.assertIn("status=green", done.stdout)


class SizeReportRefusesRedirects(_Pair):

    def _args(self, server):
        return types.SimpleNamespace(
            es=server.url, user="elastic", password=SECRET, api_key_value=None,
            tls=None, repo="repo")

    def test_a_redirect_is_a_request_failure_and_is_not_followed(self):
        # The report sends the Basic credential on its first call. A
        # followed redirect hands that credential to whatever host answers.
        with self.assertRaises(urllib.error.URLError) as raised:
            sizes.http_get("/x", self._args(self.first))
        self.assert_nothing_followed(str(raised.exception))

    def test_the_listing_reports_the_refusal_without_the_credential(self):
        # The operator reads this message; it must name the host and never
        # echo the credential or the full Location.
        err = io.StringIO()
        real, sys.stderr = sys.stderr, err
        try:
            names = sizes.fetch_snapshot_listing(self._args(self.first))
        finally:
            sys.stderr = real
        self.assertIsNone(names)
        self.assert_nothing_followed(err.getvalue())

    def test_every_redirect_status_is_refused(self):
        # 307 and 308 keep method and headers, so an allowlist of 301 and
        # 302 would still leak the credential.
        for status in (301, 303, 307, 308):
            self.first.status = status
            self.first.seen.clear()
            with self.assertRaises(urllib.error.URLError) as raised:
                sizes.http_get("/x", self._args(self.first))
            self.assertIn(str(status), str(raised.exception))
            self.assertEqual(self.second.seen, [])

    def test_a_plain_answer_is_still_read(self):
        # A refusal that broke the normal path would stop every report.
        self.second.body = b'{"ok": true}'
        self.assertEqual(sizes.http_get("/x", self._args(self.second)),
                         {"ok": True})


class ChurnRigRefusesRedirects(_Pair):

    def test_an_elasticsearch_redirect_is_an_error_and_is_not_followed(self):
        # The rig sends the Basic credential on every call, including the
        # connectivity probe a redirecting endpoint would catch.
        client = rig.Es(self.first.url, "elastic", SECRET, None)
        with self.assertRaises(rig.EsError) as raised:
            client.get("/")
        self.assert_nothing_followed(str(raised.exception))

    def test_an_s3_redirect_is_refused_and_is_not_followed(self):
        # The SigV4 signature in the request headers is a reusable secret
        # for its validity window; a followed redirect would replay it.
        store = rig.S3(self.first.url, "us-east-1", "AK", SECRET, "bucket")
        with self.assertRaises(urllib.error.URLError) as raised:
            store.list("p/")
        self.assert_nothing_followed(str(raised.exception))

    def test_a_plain_elasticsearch_answer_is_still_read(self):
        # A refusal that broke the normal path would stop every rig run.
        self.second.body = b'{"ok": true}'
        client = rig.Es(self.second.url, "elastic", SECRET, None)
        self.assertEqual(client.get("/"), {"ok": True})

    def test_a_plain_s3_answer_is_still_read(self):
        # Same, for the store: the listing must still reach the bucket.
        self.second.body = (b'<ListBucketResult><IsTruncated>false'
                            b'</IsTruncated></ListBucketResult>')
        store = rig.S3(self.second.url, "us-east-1", "AK", SECRET, "bucket")
        self.assertEqual(store.list("p/"), [])


if __name__ == "__main__":
    unittest.main()
