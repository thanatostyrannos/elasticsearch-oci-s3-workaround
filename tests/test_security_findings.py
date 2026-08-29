"""The findings a static analysis run raised, each pinned so it stays fixed.

Every one was checked against this runtime before being treated as real. The
XML denial of service reproduces here; the external-entity read that the same
rule is usually paired with does not, because ElementTree resolves no external
entities, and reporting it anyway would have been crying wolf.
"""

import hashlib
import inspect
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from generation_chain.errors import SourceReadError
from generation_chain.reclaim import batch, checksum
from generation_chain.sources import s3

import reclaim_test_protocol as protocol
import snapshot_churn_rig as rig

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
 <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
]>
<ListBucketResult>&lol4;</ListBucketResult>"""

REAL_LISTING = (b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<ListBucketResult><KeyCount>0</KeyCount>'
                b'<IsTruncated>false</IsTruncated></ListBucketResult>')

REAL_DELETE_RESULT = (b'<?xml version="1.0" encoding="UTF-8"?>'
                      b'<DeleteResult><Deleted><Key>a</Key></Deleted>'
                      b'</DeleteResult>')


class EntityExpansionIsRefusedBeforeParsing(unittest.TestCase):
    """A store answering with a DOCTYPE is answering something a store does not.

    stdlib ElementTree expands internal entities, measured on this runtime: a
    short body reaching 30,000 characters. The parser feeds the enumeration
    that decides what gets condemned, so a listing response that can hang it is
    a real availability concern on the one path into the delete pipeline.

    Refused rather than parsed with limits, because a legitimate S3 listing
    never carries a DOCTYPE at all, so there is nothing to weigh up.
    """

    def test_a_listing_with_a_doctype_is_refused(self):
        with self.assertRaises(SourceReadError) as raised:
            s3.parse_listing_body(BILLION_LAUGHS)
        self.assertIn("DOCTYPE", str(raised.exception))

    def test_a_real_listing_still_parses(self):
        # The other half. A guard that refused everything would also pass the
        # test above.
        self.assertIsNotNone(s3.parse_listing_body(REAL_LISTING))

    def test_a_delete_response_with_a_doctype_is_refused(self):
        with self.assertRaises(Exception) as raised:
            batch.parse_response(BILLION_LAUGHS.replace(
                b"ListBucketResult", b"DeleteResult"), ["a"])
        self.assertIn("DOCTYPE", str(raised.exception))

    def test_a_real_delete_response_still_parses(self):
        outcome = batch.parse_response(REAL_DELETE_RESULT, ["a"])
        self.assertEqual(list(outcome.deleted), ["a"])

    def test_the_check_looks_past_leading_whitespace(self):
        with self.assertRaises(SourceReadError):
            s3.parse_listing_body(b"\n\n   " + BILLION_LAUGHS)


class Md5IsMarkedAsNotForSecurity(unittest.TestCase):
    """Content-MD5 is an S3 protocol checksum, not a security hash.

    Unmarked, the call raises on a host with OpenSSL in FIPS mode, so the tool
    stops working on exactly the hosts most likely to be running it.
    """

    def test_md5_still_produces_the_right_digest(self):
        body = b"some object listing"
        self.assertEqual(checksum._digest("md5", body),
                         hashlib.md5(body, usedforsecurity=False).digest())

    def test_the_call_is_marked(self):
        source = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "generation_chain", "reclaim", "checksum.py")).read()
        self.assertIn("usedforsecurity=False", source)


class PlainHttpIsRefusedOffLoopback(unittest.TestCase):
    """Manifests name exactly which production objects are about to go.

    The endpoint is operator-supplied, so a copy-paste slip or a stale test
    config promoted to production sends all of it in the clear. Loopback is
    exempt because there is no network path to intercept and the offline suite
    serves plain HTTP there.
    """

    def _source(self, endpoint, **kw):
        return s3.S3CompatibleSource(
            endpoint=endpoint, region="us-east-1", bucket="b",
            credentials=s3.S3Credentials("a", "s"), **kw)

    def test_https_is_accepted(self):
        self.assertEqual(self._source("https://store.example.com").scheme,
                         "https")

    def test_plain_http_to_a_remote_host_is_refused(self):
        with self.assertRaises(SourceReadError) as raised:
            self._source("http://store.example.com")
        self.assertIn("https", str(raised.exception))

    def test_plain_http_to_loopback_is_accepted(self):
        for host in ("http://127.0.0.1:9000", "http://localhost:9000",
                     "http://[::1]:9000"):
            with self.subTest(host=host):
                self.assertEqual(self._source(host).scheme, "http")

    def test_plain_http_off_loopback_is_allowed_when_asked_for(self):
        # A lab MinIO behind an ingress is a real case, and refusing it
        # outright would push someone to a worse tool. It has to be said out
        # loud though, not inherited from a default.
        self.assertEqual(
            self._source("http://minio.lab.internal", allow_plain_http=True).scheme,
            "http")


class ReclaimHarnessEndpointSchemeIsValidated(unittest.TestCase):
    """--elasticsearch reaches reclaim_test_protocol.py's OWN calls (es_call),
    the ones that drive the segment-mode settle wait. It is configuration,
    read from a command line an operator typed, and bandit is right that
    urllib.request.urlopen does not care whether that is http or file://.
    Only http and https may reach it.
    """

    def test_file_scheme_is_refused(self):
        with self.assertRaises(ValueError) as raised:
            protocol.refuse_non_http_scheme(
                "file:///etc/passwd", "--elasticsearch")
        self.assertIn("http", str(raised.exception))

    def test_ftp_scheme_is_refused(self):
        with self.assertRaises(ValueError):
            protocol.refuse_non_http_scheme(
                "ftp://example.com/x", "--elasticsearch")

    def test_https_is_accepted(self):
        protocol.refuse_non_http_scheme(
            "https://cluster.example.com", "--elasticsearch")

    def test_plain_http_is_accepted(self):
        # This harness's own calls are allowed over plain http, unlike the
        # audit's manifest path: they drive a settle wait against a cluster
        # the operator already pointed --elasticsearch at, not a delete.
        protocol.refuse_non_http_scheme(
            "http://127.0.0.1:9200", "--elasticsearch")

    def test_es_call_refuses_before_urlopen_is_ever_reached(self):
        # The guard exists in two places: an early check in main() for a fast
        # command-line error, and here, inside es_call, on the actual network
        # boundary. Patching urlopen to fail the test if called proves the
        # second one is load bearing on its own, not just a fast-fail nicety.
        args = types.SimpleNamespace(
            elasticsearch="file:///etc/passwd", es_user="u", es_password="p")
        with mock.patch.object(protocol.urllib.request, "urlopen") as opened:
            with self.assertRaises(ValueError):
                protocol.es_call(args, "/_snapshot")
        opened.assert_not_called()


class ChurnRigEndpointSchemeIsValidated(unittest.TestCase):
    """snapshot_churn_rig.py makes its own urlopen calls straight against
    --es and --s3-endpoint, both operator-supplied. Same finding as the
    harness above, checked at the point each client is built rather than
    once in main(), because Es and S3 are also exercised directly by tests
    and by any future caller that skips main().
    """

    def test_es_refuses_file_scheme(self):
        with self.assertRaises(SystemExit):
            rig.Es("file:///etc/passwd", "elastic", "p", None)

    def test_es_accepts_http(self):
        es = rig.Es("http://127.0.0.1:9200", "elastic", "p", None)
        self.assertEqual(es.base, "http://127.0.0.1:9200")

    def test_s3_refuses_ftp_scheme(self):
        with self.assertRaises(SystemExit):
            rig.S3("ftp://evil.example.com", "us-east-1", "ak", "sk", "buk")

    def test_s3_accepts_https(self):
        made = rig.S3("https://s3.example.com", "us-east-1", "ak", "sk", "buk")
        self.assertEqual(made.endpoint, "https://s3.example.com")


class ChurnRigTlsAlwaysVerifies(unittest.TestCase):
    """The rig has no switch that turns certificate checking off. A lab
    cluster serving a certificate it signed itself is reached by naming the
    CA that signed it, so the only TLS input is --ca-cert. Pinned against
    ssl's own verification flags rather than against which function built
    the context, so a rewrite that reaches the same relaxed state under a
    different name still fails here.
    """

    def test_verification_is_on(self):
        es = rig.Es("https://cluster.example.com", "elastic", "p", None)
        self.assertEqual(es.ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(es.ctx.check_hostname)

    def test_no_argument_can_relax_the_context(self):
        # The client takes a CA bundle and nothing else about TLS, so there
        # is no value a caller can pass that reaches CERT_NONE.
        self.assertNotIn(
            "insecure",
            inspect.signature(rig.Es.__init__).parameters)

    def test_plain_http_still_needs_no_context(self):
        # The rig's other job: drive a lab cluster over plain http on
        # loopback. Guarding TLS must not touch the non-TLS path at all.
        es = rig.Es("http://127.0.0.1:9200", "elastic", "p", None)
        self.assertIsNone(es.ctx)


class ChurnRigListingReusesTheAuditsDoctypeGuard(unittest.TestCase):
    """The rig lists a bucket with the same stdlib ElementTree the audit
    parses with, and entity expansion does not care which script asked. The
    audit already carries refuse_doctype() for exactly this; this reuses it
    rather than duplicating a second copy that could drift from the first.
    """

    def _s3(self):
        return rig.S3("https://s3.example.com", "us-east-1", "ak", "sk", "buk")

    def test_a_listing_with_a_doctype_is_refused(self):
        made = self._s3()
        made._call = lambda *a, **kw: (200, BILLION_LAUGHS)
        with self.assertRaises(SourceReadError) as raised:
            made.list("prefix")
        self.assertIn("DOCTYPE", str(raised.exception))

    def test_a_real_listing_still_parses(self):
        made = self._s3()
        made._call = lambda *a, **kw: (200, REAL_LISTING)
        self.assertEqual(made.list("prefix"), [])


def _closed_loopback_port():
    """A TCP port on 127.0.0.1 nothing is listening on, for a fast refusal.

    Binding and immediately closing gets the OS to hand back a currently-free
    ephemeral port, so the connection this test drives fails for the right
    reason (ECONNREFUSED) instead of a fixed port that happens to collide
    with something already running on the test host.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class VerifyRestorableEndpointSchemeIsValidated(unittest.TestCase):
    """verify_restorable.py is a flat script, not a library: it parses
    --elasticsearch and opens its first connection at module import time, so
    there is no function to call in-process the way the other two harnesses
    allow. Driven as a real subprocess instead, the same way an operator
    runs it against a customer cluster, to prove the actual shipped entry
    point refuses the bad scheme and never reaches urlopen.
    """

    def _run(self, elasticsearch):
        with tempfile.NamedTemporaryFile("w", suffix=".pw") as pw:
            pw.write("secret\n")
            pw.flush()
            return subprocess.run(
                [sys.executable, os.path.join(ROOT, "verify_restorable.py"),
                 "--elasticsearch", elasticsearch,
                 "--repository", "repo",
                 "--password-file", pw.name],
                capture_output=True, text=True, timeout=15)

    def test_file_scheme_is_refused_before_any_network_call(self):
        completed = self._run("file:///etc/passwd")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("http", completed.stderr)

    def test_https_scheme_passes_the_gate(self):
        # Nothing is listening on this port, so the run still fails, on a
        # connection error further down. That failure is expected and is
        # NOT what this test checks; it only proves the scheme gate itself
        # let a legitimate value through instead of rejecting it.
        completed = self._run(f"https://127.0.0.1:{_closed_loopback_port()}")
        self.assertNotIn("only http and https are accepted", completed.stderr)


if __name__ == "__main__":
    unittest.main()
