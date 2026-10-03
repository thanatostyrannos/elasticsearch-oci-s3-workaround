"""Every HTTP client stops reading an answer that exceeds its cap.

Each case serves from a loopback server. The streaming cases serve a body far
larger than the cap and count the bytes the server managed to send, so a
client that read the whole body shows up as a full count instead of a
buffer-sized one.
"""

import http.server
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generation_chain import body_limits, corroboration
from generation_chain.corroboration import (CorroborationUnavailable,
                                            Credentials, ElasticsearchVeto)
from generation_chain.errors import SourceReadError
from generation_chain.reclaim import transport
from generation_chain.reclaim.transport import (TransportError, fetch_object,
                                               send_batch_delete)
from generation_chain.sources import s3
from generation_chain.sources.http_reads import HttpReader
from generation_chain.sources.s3 import S3Credentials, S3CompatibleSource

CAP = 1000
HUGE = 256 * 1024 * 1024
CHUNK = 64 * 1024


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        server = self.server
        if server.mode == "declare-only":
            self.send_response(200)
            self.send_header("Content-Length", str(HUGE))
            self.end_headers()
            self.wfile.flush()
            # Hold the answer open until the client gives up. A client that
            # tried to read the declared length would block here.
            try:
                self.rfile.read(1)
            except OSError:
                pass
            return
        if server.mode == "exact":
            raw = server.body
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        # Close-delimited stream with no Content-Length.
        self.send_response(200)
        self.end_headers()
        try:
            while server.sent < HUGE:
                self.wfile.write(b" " * CHUNK)
                server.sent += CHUNK
        except OSError:
            pass

    do_GET = do_POST = _answer


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.mode = "exact"
        self.body = b""
        self.sent = 0

    @property
    def host(self):
        return f"127.0.0.1:{self.server_port}"


class _Base(unittest.TestCase):
    def setUp(self):
        self.server = _Server()
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def serve_exact(self, size):
        self.server.mode = "exact"
        self.server.body = b" " * size

    def serve_stream(self):
        self.server.mode = "stream"

    def serve_declared_only(self):
        self.server.mode = "declare-only"

    def assert_stopped_early(self):
        # Breaks if a client buffers the whole answer before checking its
        # size: a hostile store then chooses how much memory the host spends.
        time.sleep(0.2)
        self.assertLess(self.server.sent, HUGE // 2)


class HttpReaderCap(_Base):
    def _get(self, **kwargs):
        return HttpReader(sleep=lambda _s: None, jitter=lambda: 0.0).get(
            f"http://{self.server.host}/k", {}, timeout=5.0, **kwargs)

    def test_a_body_exactly_at_the_cap_is_returned(self):
        # Breaks if the cap is off by one and the largest legitimate blob
        # is refused, which ends an audit that was fine.
        self.serve_exact(CAP)
        self.assertEqual(len(self._get(max_bytes=CAP).body), CAP)

    def test_one_byte_over_the_cap_is_refused(self):
        # Breaks if a body just over the cap is accepted, so the cap is
        # advisory and an oversized document reaches the JSON parser.
        self.serve_exact(CAP + 1)
        with self.assertRaises(SourceReadError) as raised:
            self._get(max_bytes=CAP)
        self.assertIn("larger than", str(raised.exception))

    def test_an_endless_body_stops_being_read_at_the_cap(self):
        # Breaks if the host reads a store's answer to the end: one hostile
        # blob exhausts memory before any check runs.
        self.serve_stream()
        with self.assertRaises(SourceReadError):
            self._get(max_bytes=CAP)
        self.assert_stopped_early()

    def test_an_oversized_content_length_is_refused_before_reading(self):
        # Breaks if the declared length is ignored and the client waits for
        # or buffers a body the store already said is too large.
        self.serve_declared_only()
        started = time.monotonic()
        with self.assertRaises(SourceReadError) as raised:
            self._get(max_bytes=CAP)
        self.assertIn("declares", str(raised.exception))
        self.assertLess(time.monotonic() - started, 3.0)

    def test_an_oversized_body_is_not_retried(self):
        # Breaks if a refusal is retried: a hostile store is read sixteen
        # times and the run stalls, because a retry meets the same body.
        sleeps = []
        reader = HttpReader(sleep=sleeps.append, jitter=lambda: 0.0)
        self.serve_exact(CAP + 1)
        with self.assertRaises(SourceReadError):
            reader.get(f"http://{self.server.host}/k", {}, timeout=5.0,
                       max_bytes=CAP)
        self.assertEqual(sleeps, [])

    def test_the_default_cap_is_the_blob_cap(self):
        # Breaks if a caller that names no cap gets no limit at all.
        self.serve_exact(10)
        with mock.patch("generation_chain.sources.http_reads.read_capped",
                        wraps=body_limits.read_capped) as spy:
            self._get()
        self.assertEqual(spy.call_args.args[1], body_limits.MAX_BLOB_BYTES)


class SourceCaps(_Base):
    def _source(self):
        return S3CompatibleSource(
            f"http://{self.server.host}", "us-east-1", "b",
            S3Credentials("AK", "SK"), allow_plain_http=True,
            reader=HttpReader(sleep=lambda _s: None, jitter=lambda: 0.0))

    def test_a_listing_page_keeps_the_xml_cap(self):
        # Breaks if listing pages are read under the larger blob cap, which
        # lets a hostile listing hand the XML parser 16 times its budget.
        self.serve_exact(CAP + 1)
        with mock.patch.object(s3, "MAX_XML_BODY_BYTES", CAP):
            with self.assertRaises(SourceReadError):
                self._source().list_keys()

    def test_a_blob_fetch_uses_the_blob_cap(self):
        # Breaks if a blob fetch is refused at the XML cap, so a real root
        # generation over 16 MiB can never be read.
        self.serve_exact(CAP + 1)
        self.assertEqual(len(self._source().fetch_critical("index-1")),
                         CAP + 1)


class ReclaimTransportCaps(_Base):
    def _fetch(self):
        return fetch_object(
            scheme="http", host=self.server.host, region="us-east-1",
            bucket="b", key="index.latest",
            credentials=S3Credentials("AKIAEXAMPLE", "SECRET"), timeout=5.0,
            sleep=lambda _s: None, jitter=lambda: 0.0)

    def _delete(self):
        return send_batch_delete(
            scheme="http", host=self.server.host, region="us-east-1",
            bucket="b", credentials=S3Credentials("AKIAEXAMPLE", "SECRET"),
            body=b"<Delete/>", checksum=("x-amz-checksum-crc32", "AAAAAA=="),
            timeout=5.0, sleep=lambda _s: None, jitter=lambda: 0.0)

    def test_fetch_object_accepts_the_cap_and_refuses_one_over(self):
        # Breaks if the identity check can be made to buffer a hostile
        # object, or refuses a legitimate one at the boundary.
        with mock.patch.object(transport, "MAX_BLOB_BYTES", CAP):
            self.serve_exact(CAP)
            self.assertEqual(len(self._fetch()), CAP)
            self.serve_exact(CAP + 1)
            with self.assertRaises(TransportError):
                self._fetch()

    def test_fetch_object_stops_reading_an_endless_body(self):
        # Breaks if the reclaim identity read buffers the whole stream.
        self.serve_stream()
        with mock.patch.object(transport, "MAX_BLOB_BYTES", CAP):
            with self.assertRaises(TransportError):
                self._fetch()
        self.assert_stopped_early()

    def test_fetch_object_refuses_an_oversized_content_length(self):
        # Breaks if a declared length is trusted to be small.
        self.serve_declared_only()
        with mock.patch.object(transport, "MAX_BLOB_BYTES", CAP):
            with self.assertRaises(TransportError) as raised:
                self._fetch()
        self.assertIn("declares", str(raised.exception))

    def test_a_delete_result_keeps_the_xml_cap(self):
        # Breaks if a delete result is read without the XML cap, or under
        # the blob cap, where the next parse would refuse it anyway.
        with mock.patch.object(transport, "MAX_XML_BODY_BYTES", CAP):
            self.serve_exact(CAP)
            self.assertEqual(len(self._delete()), CAP)
            self.serve_exact(CAP + 1)
            with self.assertRaises(TransportError):
                self._delete()
        self.serve_stream()
        with mock.patch.object(transport, "MAX_XML_BODY_BYTES", CAP):
            with self.assertRaises(TransportError):
                self._delete()
        self.assert_stopped_early()


class CorroborationCaps(_Base):
    def _get(self):
        veto = ElasticsearchVeto(
            f"http://{self.server.host}", "repo",
            Credentials(None, None, None))
        return veto._get("/_snapshot/repo/_all")

    def test_an_answer_at_the_cap_parses_and_one_over_is_refused(self):
        # Breaks if Elasticsearch, or anything in front of it, can make the
        # veto buffer an unbounded answer, or if a refusal reads as "no
        # snapshots" and lets the run proceed without protections.
        with mock.patch.object(corroboration, "MAX_JSON_ANSWER_BYTES", CAP):
            self.server.mode = "exact"
            self.server.body = b"{" + b" " * (CAP - 2) + b"}"
            self.assertEqual(self._get(), {})
            self.server.body = b"{" + b" " * (CAP - 1) + b"}"
            with self.assertRaises(CorroborationUnavailable) as raised:
                self._get()
            self.assertFalse(raised.exception.transient)

    def test_an_endless_answer_stops_being_read(self):
        # Breaks if the veto reads a hostile endless answer to the end.
        self.serve_stream()
        with mock.patch.object(corroboration, "MAX_JSON_ANSWER_BYTES", CAP):
            with self.assertRaises(CorroborationUnavailable):
                self._get()
        self.assert_stopped_early()

    def test_an_oversized_content_length_is_refused_before_reading(self):
        # Breaks if the declared length is ignored.
        self.serve_declared_only()
        with mock.patch.object(corroboration, "MAX_JSON_ANSWER_BYTES", CAP):
            with self.assertRaises(CorroborationUnavailable) as raised:
                self._get()
        self.assertIn("declares", str(raised.exception))


class CapValues(unittest.TestCase):
    def test_the_blob_cap_holds_a_large_repositorys_root_generation(self):
        # Breaks if a cap is lowered under the derived 30 MB root generation
        # of a 1000 snapshot by 1000 index repository: the audit would
        # refuse the one blob it cannot do without.
        self.assertGreaterEqual(body_limits.MAX_BLOB_BYTES, 8 * 30_000_000)
        self.assertGreater(body_limits.MAX_BLOB_BYTES,
                           body_limits.MAX_XML_BODY_BYTES)

    def test_the_xml_cap_is_still_the_one_s3_exports(self):
        # Breaks if the 59 cap and the transport cap drift apart.
        self.assertIs(s3.MAX_XML_BODY_BYTES, body_limits.MAX_XML_BODY_BYTES)


if __name__ == "__main__":
    unittest.main()
