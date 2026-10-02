"""The memory ceiling applies while the listing is read, not after it.

The budget used to count objects once the transport had returned the whole
listing. A store with millions of keys, or one that answers every page with
the same continuation token, filled memory before the check ever ran.
"""

import http.server
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import genchain_fixtures as fx
import s3rig
from generation_chain.errors import SourceReadError
from generation_chain.sources.budget import (RESIDENT_BYTES_PER_OBJECT,
                                             MemoryBudget, RepositoryTooLarge,
                                             with_budget)
from generation_chain.sources.http_reads import HttpReader
from generation_chain.sources.local import LocalMirrorSource
from generation_chain.sources.oci import OciNativeSource
from generation_chain.sources.s3 import S3CompatibleSource, S3Credentials
from test_generation_chain_transports import _OciRig, _oci_credentials

PAGE = 1000
CEILING_OBJECTS = 2500
CEILING_BYTES = CEILING_OBJECTS * RESIDENT_BYTES_PER_OBJECT
NS = "http://s3.amazonaws.com/doc/2006-03-01/"


class _PagedStore:
    """A loopback S3 listing that counts the pages it serves.

    `fresh_tokens=False` answers every page with the same token, which is the
    store that never ends.
    """

    def __init__(self, total, fresh_tokens=True):
        self.total = total
        self.fresh_tokens = fresh_tokens
        self.pages = 0

    def __enter__(self):
        store = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                params = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(self.path).query)
                token = params.get("continuation-token", ["0"])[0]
                first = int(token) if token.isdigit() else 0
                last = min(first + PAGE, store.total)
                store.pages += 1
                rows = "".join(
                    f"<Contents><Key>k/{i:07d}</Key><Size>1</Size></Contents>"
                    for i in range(first, last))
                more = last < store.total
                following = str(last) if store.fresh_tokens else "SAME"
                tail = (f"<NextContinuationToken>{following}"
                        "</NextContinuationToken>") if more else ""
                body = (f'<?xml version="1.0"?><ListBucketResult xmlns="{NS}">'
                        f"<IsTruncated>{'true' if more else 'false'}"
                        f"</IsTruncated>{tail}{rows}</ListBucketResult>"
                        ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                       Handler)
        threading.Thread(target=self._server.serve_forever,
                         daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    def source(self):
        return S3CompatibleSource(
            endpoint=f"http://127.0.0.1:{self._server.server_address[1]}",
            region=s3rig.TEST_REGION, bucket="b", prefix="",
            credentials=S3Credentials(s3rig.TEST_ACCESS_KEY,
                                      s3rig.TEST_SECRET_KEY),
            reader=HttpReader(sleep=lambda _s: None, jitter=lambda: 0.0))


class TheCeilingAppliesWhileTheListingIsRead(unittest.TestCase):

    def test_a_listing_under_the_ceiling_completes(self):
        # Use case. If the per-page check miscounted by even one page, every
        # repository near the limit would be refused on a host that holds it.
        with _PagedStore(total=2000) as store:
            keys = MemoryBudget(store.source(),
                                limit_bytes=CEILING_BYTES).list_keys()
        self.assertEqual(len(keys), 2000)

    def test_a_listing_over_the_ceiling_stops_within_one_page_of_it(self):
        # Abuse case. A million-key store was fetched in full before the
        # budget spoke, so the host ran out of memory at the point the check
        # existed to prevent. The page count is what proves the stop is early.
        with _PagedStore(total=20 * PAGE) as store:
            with self.assertRaises(RepositoryTooLarge):
                MemoryBudget(store.source(),
                             limit_bytes=CEILING_BYTES).list_keys()
        self.assertLessEqual(store.pages, CEILING_OBJECTS // PAGE + 1)

    def test_a_store_that_repeats_its_token_is_refused_not_followed(self):
        # Abuse case. Without the guard a faulty store makes the run pull
        # pages until MAX_PAGES, which is about a hundred million keys.
        with _PagedStore(total=20 * PAGE, fresh_tokens=False) as store:
            with self.assertRaises(SourceReadError) as caught:
                store.source().list_keys()
        self.assertIn("repeated", str(caught.exception))
        self.assertLessEqual(store.pages, 3)

    def test_a_repeating_token_is_a_read_failure_not_a_size_refusal(self):
        # Operators route "too large" to a bigger host. A broken store needs
        # a different response, so the two must not share a type.
        with _PagedStore(total=20 * PAGE, fresh_tokens=False) as store:
            with self.assertRaises(SourceReadError) as caught:
                MemoryBudget(store.source(),
                             limit_bytes=10 ** 12).list_keys()
        self.assertNotIsInstance(caught.exception, RepositoryTooLarge)

    def test_memory_mb_zero_lists_everything(self):
        # Use case for the off switch. An operator who sets 0 has said the
        # host is bigger than it reports, and a stray limit would refuse a
        # run they chose to make.
        with _PagedStore(total=20 * PAGE) as store:
            keys = with_budget(store.source(), 0).list_keys()
        self.assertEqual(len(keys), 20 * PAGE)
        with _PagedStore(total=20 * PAGE) as store:
            keys = MemoryBudget(store.source(), limit_bytes=0).list_keys()
        self.assertEqual(len(keys), 20 * PAGE)

    def test_a_wrapper_that_ignores_the_page_hook_is_still_checked(self):
        # Abuse case. A source whose list_keys takes no hook, such as a test
        # double or a third-party transport, must not slip past the ceiling.
        class Plain:
            def describe(self):
                return "plain"

            def list_keys(self):
                return ["k%d" % i for i in range(CEILING_OBJECTS + 1)]

        with self.assertRaises(RepositoryTooLarge):
            MemoryBudget(Plain(), limit_bytes=CEILING_BYTES).list_keys()


class OtherTransportsHonourTheCeilingToo(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-listing-budget-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        fx.build_repository(self.root, [
            {"s1": {"idx": ["__a", "__shared"]}},
            {"s1": {"idx": ["__a", "__shared"]},
             "s2": {"idx": ["__b", "__shared"]}},
            {"s2": {"idx": ["__b", "__shared"]}},
        ])
        self.total = len(LocalMirrorSource(self.root).list_keys())

    def test_the_oci_listing_stops_before_its_last_page(self):
        # Without it the OCI transport read every page before the budget
        # looked, which is the same memory failure over a different API.
        with _OciRig(self.root, page_size=3) as rig:
            source = OciNativeSource(
                endpoint=rig.endpoint, namespace=rig.namespace,
                bucket=rig.bucket, prefix="",
                credentials=_oci_credentials(),
                reader=HttpReader(sleep=lambda _s: None, jitter=lambda: 0.0))
            with self.assertRaises(RepositoryTooLarge):
                MemoryBudget(source, limit_bytes=3 * RESIDENT_BYTES_PER_OBJECT
                             ).list_keys()
        self.assertLess(rig.pages_served, -(-self.total // 3))

    def test_the_local_mirror_reports_its_count_while_walking(self):
        # Without the hook a huge mirror tree is walked to the end before the
        # budget can refuse it.
        class Stop(Exception):
            pass

        seen = []

        def hook(count):
            seen.append(count)
            raise Stop

        with self.assertRaises(Stop):
            LocalMirrorSource(self.root).list_keys(on_page=hook)
        self.assertEqual(len(seen), 1)
        self.assertLess(seen[0], self.total)


if __name__ == "__main__":
    unittest.main()
