"""The findings a static analysis run raised, each pinned so it stays fixed.

Every one was checked against this runtime before being treated as real. The
XML denial of service reproduces here; the external-entity read that the same
rule is usually paired with does not, because ElementTree resolves no external
entities, and reporting it anyway would have been crying wolf.
"""

import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generation_chain.errors import SourceReadError
from generation_chain.reclaim import batch, checksum
from generation_chain.sources import s3

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


if __name__ == "__main__":
    unittest.main()
