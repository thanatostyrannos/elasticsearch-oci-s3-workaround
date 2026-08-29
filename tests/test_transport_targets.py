"""What the one deleting request will and will not be sent to.

The host in `--endpoint` is typed by whoever runs the command, and it lands in
two places at once: the URL urllib opens, and the `Host` header this module
signs. A string that is not a host is still a string in both of those places,
so `store.example.com/../elsewhere` reads as an ordinary name to a check that
only looks for the characters someone remembered, and as a different origin to
urllib. These tests hold the check to the shape it actually needs, an allowed
form rather than a list of refused characters, and they hold the other half
too: an ordinary endpoint still goes through untouched.

`tests/test_path_and_endpoint_validation.py` covers the refusals that were
already pinned there (a scheme urllib would open some other way, an empty
host, userinfo, a newline). This file covers the form of the host itself.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generation_chain.reclaim.transport import TransportError, send_batch_delete
from generation_chain.sources.s3 import S3Credentials


class _RefusingOpener:
    """An opener that fails the test by having been reached at all."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("a request was sent that should have been refused")


class _Response:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body


class _RecordingOpener:
    """An opener that answers, and keeps the request it was handed."""

    def __init__(self):
        self.requests = []

    def __call__(self, request, timeout=None, **kwargs):
        self.requests.append(request)
        return _Response(b"<DeleteResult/>")


def _send(host, opener, scheme="https"):
    return send_batch_delete(
        scheme=scheme, host=host, region="us-east-1", bucket="bucket",
        credentials=S3Credentials("AKIAEXAMPLE", "secret"), body=b"<Delete/>",
        checksum=("x-amz-checksum-crc32", "AAAAAA=="), timeout=1.0,
        opener=opener, sleep=lambda _s: None, jitter=lambda: 0.0)


class HostsThatAreRefused(unittest.TestCase):
    """Every one of these is a host that stops being a host inside a URL."""

    def setUp(self):
        self.opener = _RefusingOpener()

    def test_a_host_carrying_a_path_is_refused(self):
        # The one that a list of forbidden characters misses. Everything
        # after the slash is a path to urllib, so the delete would go to
        # `evil.example.com` with this project's signature on it.
        with self.assertRaises(TransportError):
            _send("evil.example.com/store.example.com", self.opener)

    def test_nothing_is_sent_when_the_host_carries_a_path(self):
        with self.assertRaises(TransportError):
            _send("evil.example.com/store.example.com", self.opener)
        self.assertEqual(self.opener.calls, 0)

    def test_a_host_carrying_a_query_is_refused(self):
        # `?delete` is already in the URL this module builds. A second query
        # string arriving inside the host changes which request is signed.
        with self.assertRaises(TransportError):
            _send("store.example.com?x=1", self.opener)

    def test_a_host_carrying_a_fragment_is_refused(self):
        with self.assertRaises(TransportError):
            _send("store.example.com#other", self.opener)

    def test_a_host_carrying_a_backslash_is_refused(self):
        # Some URL parsers read a backslash as a separator and some do not.
        # A host this module cannot predict the reading of is not sendable.
        with self.assertRaises(TransportError):
            _send("store.example.com\\@evil.example.com", self.opener)

    def test_a_percent_encoded_separator_is_refused(self):
        # Refused rather than decoded. Decoding it here would leave two
        # readings of the same host, this module's and urllib's.
        with self.assertRaises(TransportError):
            _send("store.example.com%2Fevil", self.opener)

    def test_a_host_with_a_tab_is_refused(self):
        with self.assertRaises(TransportError):
            _send("store.example.com\tX-Injected: 1", self.opener)

    def test_a_port_that_is_not_a_number_is_refused(self):
        with self.assertRaises(TransportError):
            _send("store.example.com:9000junk", self.opener)

    def test_the_refusal_names_the_host_it_refused(self):
        # An operator who typed the endpoint gets to see what this read.
        with self.assertRaises(TransportError) as caught:
            _send("store.example.com/x", self.opener)
        self.assertIn("store.example.com/x", str(caught.exception))

    def test_the_refusal_says_nothing_was_sent(self):
        with self.assertRaises(TransportError) as caught:
            _send("store.example.com/x", self.opener)
        self.assertIn("Nothing was sent", str(caught.exception))


class HostsThatGoThrough(unittest.TestCase):
    """The half that matters more: an ordinary endpoint is not made harder."""

    def setUp(self):
        self.opener = _RecordingOpener()

    def test_a_plain_name_reaches_the_store(self):
        self.assertEqual(_send("store.example.com", self.opener),
                         b"<DeleteResult/>")

    def test_the_host_reaches_the_url_unchanged(self):
        _send("store.example.com", self.opener)
        self.assertEqual(self.opener.requests[0].full_url,
                         "https://store.example.com/bucket?delete=")

    def test_a_name_with_a_port_reaches_the_store(self):
        _send("127.0.0.1:9000", self.opener, scheme="http")
        self.assertEqual(self.opener.requests[0].full_url,
                         "http://127.0.0.1:9000/bucket?delete=")

    def test_an_ipv6_literal_reaches_the_store(self):
        _send("[::1]:9000", self.opener, scheme="http")
        self.assertEqual(self.opener.requests[0].full_url,
                         "http://[::1]:9000/bucket?delete=")

    def test_a_hyphenated_regional_endpoint_reaches_the_store(self):
        # The shape of a real OCI S3-compatible endpoint, which is the one
        # this tool is pointed at in practice.
        host = "namespace.compat.objectstorage.uk-london-1.oraclecloud.com"
        _send(host, self.opener)
        self.assertEqual(self.opener.requests[0].full_url,
                         f"https://{host}/bucket?delete=")

    def test_a_single_label_host_reaches_the_store(self):
        # A container name on a lab network has no dots in it.
        _send("minio", self.opener, scheme="http")
        self.assertEqual(self.opener.requests[0].full_url,
                         "http://minio/bucket?delete=")

    def test_the_signed_host_header_is_the_host_that_was_asked_for(self):
        _send("store.example.com", self.opener)
        self.assertEqual(self.opener.requests[0].get_header("Host"),
                         "store.example.com")


if __name__ == "__main__":
    unittest.main()
