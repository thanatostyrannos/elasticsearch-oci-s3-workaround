"""Facts about Oracle's API that cost real work to establish.

The vectors below were established against a live tenancy by earlier work in
this repository and are carried forward as data. There is no OCI endpoint on
this machine, so a signer written from scratch can only ever agree with
itself, and these are what stop that. The tests are this package's own,
written against this package's own signer, because a transport that inherited
the old client's assumptions would inherit its blind spots too.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generation_chain.errors import SourceReadError
from generation_chain.sources import http_reads
from generation_chain.sources.oci import endpoint_for_region
from generation_chain.sources.signing import oci_signature

FIXED_DATE = "Thu, 05 Jan 2014 21:31:40 GMT"


class SigningStringVectors(unittest.TestCase):

    def test_the_exact_bytes_for_a_known_request(self):
        # Oracle rebuilds these bytes server side and answers a mismatch with
        # a bare 401 naming nothing, so a stray space or a capitalised verb
        # costs an afternoon against a live bucket instead of a local diff.
        self.assertEqual(
            oci_signature.signing_string(
                "GET", "/n/ns/b/bkt/o/index.latest",
                "objectstorage.us-ashburn-1.oraclecloud.com", FIXED_DATE),
            b"date: Thu, 05 Jan 2014 21:31:40 GMT\n"
            b"(request-target): get /n/ns/b/bkt/o/index.latest\n"
            b"host: objectstorage.us-ashburn-1.oraclecloud.com")

    def test_the_declared_header_list_describes_the_bytes_that_were_signed(self):
        # The Authorization line tells Oracle which headers to reassemble and
        # in which order. If that list stops describing what was signed, every
        # request 401s and the header names in the log still look right, so
        # nothing in the failure points at the ordering.
        header = oci_signature.authorization_header("t/u/f", b"\x00")
        declared = header.split('headers="')[1].split('"')[0].split(" ")
        signed = oci_signature.signing_string(
            "GET", "/p", "h", FIXED_DATE).decode().split("\n")
        self.assertEqual(declared, [line.split(":")[0] for line in signed])

    def test_the_query_string_is_inside_the_request_target(self):
        # Abuse case: a replayed signature against the same path with a
        # different query. For a paginated listing the query is where the page
        # token lives, so signing the path alone would let one page's
        # signature fetch any other page.
        with_query = oci_signature.signing_string(
            "GET", "/n/ns/b/bkt/o?prefix=a&limit=1000", "h", FIXED_DATE)
        self.assertIn(b"(request-target): get /n/ns/b/bkt/o?prefix=a&limit=1000",
                      with_query)
        self.assertNotEqual(
            with_query,
            oci_signature.signing_string("GET", "/n/ns/b/bkt/o", "h", FIXED_DATE))

    def test_a_percent_encoded_path_is_signed_exactly_as_it_is_sent(self):
        # Abuse case built from the keys this tool really meets. Signing the
        # raw name while sending the encoded one fails only for the odd key,
        # so a run looks healthy until it 401s halfway through.
        encoded = oci_signature.quote_segment("a b/c%d")
        self.assertEqual(encoded, "a%20b/c%25d")
        self.assertNotEqual(
            oci_signature.signing_string("GET", "/o/" + encoded, "h", FIXED_DATE),
            oci_signature.signing_string("GET", "/o/a b/c%d", "h", FIXED_DATE))

    def test_object_names_keep_their_slashes_and_encode_everything_else(self):
        # The classic hand-rolled-client bug, in both directions. Over-encode
        # and every slash becomes %2F, which addresses a different object, so
        # a run 404s on everything and reports the repository as already gone.
        for raw, want in {
                "es-snapshots/indices/Abc123XyZ/0/__Trace1234567890":
                    "es-snapshots/indices/Abc123XyZ/0/__Trace1234567890",
                "a b/c": "a%20b/c",
                "a+b/c": "a%2Bb/c",
                "50%25/x": "50%2525/x",
                "snåpshot/é": "sn%C3%A5pshot/%C3%A9",
                "a?b/c": "a%3Fb/c",
                "a#b/c": "a%23b/c",
                "a&b=c/d": "a%26b%3Dc/d"}.items():
            self.assertEqual(oci_signature.quote_segment(raw), want, raw)

    def test_a_space_in_a_page_token_is_never_sent_as_a_plus(self):
        # A next-page token is an object name and can hold a space. urlencode
        # would send it as "+", the service reads that back as a literal plus,
        # the listing resumes from a key that does not exist, and the run
        # silently returns short while looking clean.
        query = oci_signature.query_string({"start": "a b/c", "limit": "1000"})
        self.assertIn("start=a%20b%2Fc", query)
        self.assertNotIn("+", query)


class EndpointResolution(unittest.TestCase):

    def test_the_region_names_the_host_and_the_realms_differ(self):
        # Built from the region rather than shipping Oracle's 85-entry table.
        # Commercial OCI is oraclecloud.com and the government realms are not,
        # so one default sends every us-gov request to a name that does not
        # resolve.
        self.assertEqual(endpoint_for_region("eu-frankfurt-1"),
                         "objectstorage.eu-frankfurt-1.oraclecloud.com")
        self.assertEqual(endpoint_for_region("us-gov-ashburn-1"),
                         "objectstorage.us-gov-ashburn-1.oraclegovcloud.com")
        self.assertEqual(endpoint_for_region("uk-gov-london-1"),
                         "objectstorage.uk-gov-london-1.oraclegovcloud.uk")

    def test_a_realm_outside_the_table_can_be_overridden(self):
        # Abuse case: the realm nobody enumerated. Without a working override
        # a dedicated-region or sovereign-cloud operator cannot use this tool
        # at all, so the escape hatch takes a bare host and a pasted URL alike.
        self.assertEqual(
            endpoint_for_region("us-ashburn-1",
                                "https://objectstorage.example.test/"),
            "objectstorage.example.test")
        self.assertEqual(endpoint_for_region("us-ashburn-1", "os.internal:8443"),
                         "os.internal:8443")


class _Attempt:
    def __init__(self, status=200, headers=None, body=b"ok"):
        self.status = status
        self.headers = headers or {}
        self.body = body


class _Opener:
    """Answers the calls a reader makes, in the order a test lists them."""

    def __init__(self, attempts):
        self.attempts = list(attempts)
        self.calls = 0

    def __call__(self, request, timeout=None):
        self.calls += 1
        attempt = self.attempts.pop(0) if self.attempts else _Attempt()
        if attempt.status >= 400:
            raise http_reads.urllib.error.HTTPError(
                request.full_url, attempt.status, "err",
                attempt.headers, None)
        return _Body(attempt)


class _Body:
    def __init__(self, attempt):
        self._attempt = attempt

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._attempt.body

    @property
    def status(self):
        return self._attempt.status

    @property
    def headers(self):
        return self._attempt.headers


class RetryPolicy(unittest.TestCase):

    def reader(self, attempts, **kwargs):
        self.slept = []
        return http_reads.HttpReader(
            opener=_Opener(attempts), sleep=self.slept.append,
            jitter=lambda: 1.0, **kwargs)

    def test_a_throttled_request_is_retried_and_then_succeeds(self):
        # A repository listing is thousands of calls and any store will
        # throttle some of them. Giving up on the first 429 turns a large but
        # healthy repository into a refused run.
        reader = self.reader([_Attempt(429), _Attempt(200, body=b"hi")])
        self.assertEqual(reader.get("http://x/", {}).body, b"hi")

    def test_retry_after_is_honoured_and_capped(self):
        # Stores send Retry-After values in the hundreds of seconds. Ignoring
        # it hammers a store that just asked for room; obeying it without a
        # cap parks the run for an hour with nothing on screen.
        reader = self.reader([_Attempt(429, {"Retry-After": "600"}),
                              _Attempt(200)])
        reader.get("http://x/", {})
        self.assertEqual(self.slept, [http_reads.RetryPolicy().max_sleep_seconds])

    def test_a_404_or_a_403_is_never_retried(self):
        # A missing object and a denied one are answers, not weather. Retrying
        # either turns one wrong credential into eight, and the run takes
        # eight times as long to tell the operator the same thing.
        for status in (403, 404):
            reader = self.reader([_Attempt(status)] * 8)
            with self.assertRaises(SourceReadError):
                reader.get("http://x/", {})
            self.assertEqual(reader._opener.calls, 1, status)

    def test_unbounded_server_errors_give_up_and_raise(self):
        # Abuse case for a store that is simply down. The run has to end with
        # a refusal an operator can act on rather than retrying until someone
        # notices, and a read that never completed must never look like a read
        # that returned nothing.
        reader = self.reader([_Attempt(500)] * 20)
        with self.assertRaises(SourceReadError):
            reader.get("http://x/", {})
        self.assertEqual(reader._opener.calls,
                         http_reads.RetryPolicy().max_attempts)


if __name__ == "__main__":
    unittest.main()
