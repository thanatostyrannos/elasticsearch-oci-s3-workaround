"""The three reads that end a run get a longer retry policy than the rest."""

import io
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import s3rig
from generation_chain.errors import SourceReadError
from generation_chain.sources.http_reads import (
    CRITICAL_RETRY_POLICY, RetryPolicy, HttpReader)
from generation_chain.sources.oci import OciNativeSource
from generation_chain.sources.s3 import S3CompatibleSource, S3Credentials

from test_generation_chain_transports import _oci_credentials

S3_EMPTY_LISTING = (b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/'
                    b'2006-03-01/"><IsTruncated>false</IsTruncated>'
                    b'</ListBucketResult>')
OCI_EMPTY_LISTING = b'{"objects": []}'

# Above the ordinary policy's eight attempts, below the critical policy's cap.
TRANSIENT_FAILURES = RetryPolicy().max_attempts + 3


class _Reply:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        return self._body


class _FlakyStore:
    """Answers 503 a fixed number of times, then answers with `body`."""

    def __init__(self, failures, body=b"payload"):
        self.failures = failures
        self.body = body
        self.calls = 0

    def __call__(self, request, timeout=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise urllib.error.HTTPError(request.full_url, 503, "busy", {},
                                         io.BytesIO(b""))
        return _Reply(self.body)


def _reader(store):
    return HttpReader(sleep=lambda _s: None, jitter=lambda: 0.0, opener=store)


def _s3(store):
    return S3CompatibleSource(
        endpoint="https://s3.example.invalid", region=s3rig.TEST_REGION,
        bucket="b", credentials=S3Credentials(s3rig.TEST_ACCESS_KEY,
                                              s3rig.TEST_SECRET_KEY),
        reader=_reader(store))


def _oci(store):
    return OciNativeSource(
        endpoint="https://oci.example.invalid", namespace="ns", bucket="b",
        credentials=_oci_credentials(), reader=_reader(store))


class CriticalReadsRetryLonger(unittest.TestCase):

    def test_a_critical_read_outlasts_failures_that_defeat_an_ordinary_read(self):
        # index.latest and the anchor generation end the run if they fail.
        # If fetch_critical stopped using the longer policy, a store that
        # throttles for a few more seconds than usual would refuse a run
        # that thirty minutes of listing had already paid for.
        for build in (_s3, _oci):
            with self.subTest(build.__name__):
                store = _FlakyStore(TRANSIENT_FAILURES)
                self.assertEqual(build(store).fetch_critical("index.latest"),
                                 b"payload")

    def test_the_listing_outlasts_failures_that_defeat_an_ordinary_read(self):
        # The listing has no partial form. If its pages went back to the
        # ordinary policy, one page of a hundred hitting a throttle would
        # throw away every page already read.
        for build, body in ((_s3, S3_EMPTY_LISTING), (_oci, OCI_EMPTY_LISTING)):
            with self.subTest(build.__name__):
                store = _FlakyStore(TRANSIENT_FAILURES, body)
                self.assertEqual(build(store).list_keys(), [])

    def test_an_ordinary_read_does_not_get_the_longer_policy(self):
        # Abuse: a shard document on a struggling store. If fetch quietly
        # took the long policy, every failing key would stall the run for
        # minutes instead of shortening the manifest and moving on.
        for build in (_s3, _oci):
            with self.subTest(build.__name__):
                store = _FlakyStore(TRANSIENT_FAILURES)
                with self.assertRaises(SourceReadError):
                    build(store).fetch("shard-doc")
                self.assertEqual(store.calls, RetryPolicy().max_attempts)

    def test_a_critical_read_still_gives_up_and_refuses(self):
        # Abuse: a store that is down. If the longer policy had no ceiling
        # the run would hang forever instead of refusing, and a hung run is
        # indistinguishable from a slow one.
        for build in (_s3, _oci):
            with self.subTest(build.__name__):
                store = _FlakyStore(10 ** 6)
                with self.assertRaises(SourceReadError):
                    build(store).fetch_critical("index.latest")
                self.assertEqual(store.calls,
                                 CRITICAL_RETRY_POLICY.max_attempts)


if __name__ == "__main__":
    unittest.main()
