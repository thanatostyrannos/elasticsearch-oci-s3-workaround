"""The store reads and the delete hold TLS to the same floor as the cluster.

The Elasticsearch client pins TLS 1.2 as its minimum. The S3 and OCI reads,
which carry the request signature, and the DeleteObjects request, which
carries the signature and the list of keys to remove, used to open with no
context at all. urllib then built one from `ssl.create_default_context`,
which leaves `minimum_version` at MINIMUM_SUPPORTED, so the floor for the
requests that matter most was whatever the host's OpenSSL build allowed.

Each test sends to a loopback port nothing listens on, so the connection
fails before any handshake, and records the TLS context the opener was
built with.
"""

import os
import socket
import ssl
import sys
import unittest
import urllib.request
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generation_chain.errors import SourceReadError
from generation_chain.reclaim.transport import (RetryPolicy as DeletePolicy,
                                                TransportError,
                                                send_batch_delete)
from generation_chain.redirects import refusing_urlopen
from generation_chain.sources.http_reads import HttpReader, RetryPolicy
from generation_chain.sources.s3 import S3Credentials


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class EveryHttpsRequestCarriesTheFloor(unittest.TestCase):

    def setUp(self):
        self.contexts = []
        test = self

        class Recording(urllib.request.HTTPSHandler):
            def __init__(self, *args, context=None, **kwargs):
                test.contexts.append(context)
                super().__init__(*args, context=context, **kwargs)

        patcher = patch.object(urllib.request, "HTTPSHandler", Recording)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.port = _closed_port()

    def assertFloorHeld(self):
        self.assertTrue(self.contexts, "no https handler was built")
        for context in self.contexts:
            self.assertIsNotNone(context)
            self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_a_store_read_over_https_refuses_anything_below_tls_1_2(self):
        # The S3 and OCI listings and blob reads carry the signed request.
        # If the store transport went back to urllib's default context, the
        # oldest protocol the host's OpenSSL allowed would carry it.
        # Neutered under "an-https-request-gets-the-tls-floor".
        reader = HttpReader(policy=RetryPolicy(max_attempts=1),
                            sleep=lambda _s: None)
        with self.assertRaises(SourceReadError):
            reader.get(f"https://127.0.0.1:{self.port}/bucket/key", {})
        self.assertFloorHeld()

    def test_the_delete_over_https_refuses_anything_below_tls_1_2(self):
        # The one request that removes objects carries the signature and the
        # whole batch of keys. It gets the floor the read path gets.
        with self.assertRaises(TransportError):
            send_batch_delete(
                scheme="https", host=f"127.0.0.1:{self.port}",
                region="us-east-1", bucket="bucket",
                credentials=S3Credentials("AKIAEXAMPLE", "secret"),
                body=b"<Delete/>",
                checksum=("x-amz-checksum-crc32", "AAAAAA=="), timeout=1.0,
                policy=DeletePolicy(max_attempts=1),
                sleep=lambda _s: None, jitter=lambda: 0.0)
        self.assertFloorHeld()

    def test_a_context_the_caller_supplies_is_used_as_given(self):
        # The cluster client builds its own context to load --es-ca-cert.
        # A default that replaced it would drop the operator's CA, and the
        # cluster re-check would fail on every private certificate.
        supplied = ssl.create_default_context()
        with self.assertRaises(OSError):
            refusing_urlopen(urllib.request.Request(
                f"https://127.0.0.1:{self.port}/"), timeout=1.0,
                context=supplied)
        self.assertEqual([supplied], self.contexts)


if __name__ == "__main__":
    unittest.main()
