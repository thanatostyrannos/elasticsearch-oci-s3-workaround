"""The checksum a batch delete carries, and the refusal for anything else.

`DeleteObjects` is checksum-required on every store this project has
measured, and the store decides which checksum it wants. These tests pin two
things: that each supported algorithm produces the header a real store
verifies, checked against `hashlib`/`zlib` and a published CRC-32C check
value rather than against this package's own arithmetic, and that a value
outside the supported set is refused rather than guessed at.
"""

import base64
import hashlib
import os
import sys
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generation_chain.reclaim import checksum


class Crc32c(unittest.TestCase):

    def test_matches_the_published_check_value(self):
        # Use case. "123456789" -> 0xE3069283 is the standard check value the
        # CRC-32C polynomial is published against; if this ever drifts, the
        # checksum this package sends for `crc32c` no longer matches what any
        # real store computes over the same bytes, and every batch fails.
        self.assertEqual(checksum.crc32c(b"123456789"), 0xE3069283)

    def test_differs_from_plain_crc32_on_the_same_bytes(self):
        # Abuse case. `crc32c` and the IEEE `crc32` this project also
        # supports are different 32-bit values over the same input. If a
        # future edit made `crc32c` alias `zlib.crc32`, this passes silently
        # right up until it is checked against a store that actually
        # verifies CRC-32C, which is the whole scenario `checksum_header`
        # below exists to get right on the first try.
        body = b"whatever the manifest names"
        self.assertNotEqual(checksum.crc32c(body), zlib.crc32(body))


class ChecksumHeader(unittest.TestCase):

    BODY = b"<Delete><Object><Key>a</Key></Object></Delete>"

    def test_md5_matches_hashlib_over_the_same_bytes(self):
        # Use case, and the default: this is `Content-MD5`, what this
        # project's own lab MinIO and OCI require.
        name, value = checksum.checksum_header(checksum.MD5, self.BODY)
        self.assertEqual(name, "Content-MD5")
        self.assertEqual(base64.b64decode(value), hashlib.md5(self.BODY).digest())

    def test_crc32_matches_zlib_over_the_same_bytes(self):
        # Use case. Genuine AWS S3's own accepted alternative, per the issue.
        name, value = checksum.checksum_header(checksum.CRC32, self.BODY)
        self.assertEqual(name, "x-amz-checksum-crc32")
        expected = zlib.crc32(self.BODY).to_bytes(4, "big")
        self.assertEqual(base64.b64decode(value), expected)

    def test_crc32c_matches_the_module_s_own_implementation(self):
        # Use case. Oracle's documented alternative for the Amazon S3
        # Compatibility API.
        name, value = checksum.checksum_header(checksum.CRC32C, self.BODY)
        self.assertEqual(name, "x-amz-checksum-crc32c")
        expected = checksum.crc32c(self.BODY).to_bytes(4, "big")
        self.assertEqual(base64.b64decode(value), expected)

    def test_sha256_matches_hashlib_over_the_same_bytes(self):
        # Use case. Oracle's other documented alternative.
        name, value = checksum.checksum_header(checksum.SHA256, self.BODY)
        self.assertEqual(name, "x-amz-checksum-sha256")
        self.assertEqual(base64.b64decode(value),
                         hashlib.sha256(self.BODY).digest())

    def test_an_unrecognised_algorithm_is_refused_rather_than_guessed(self):
        # Abuse case, and the guard the issue names directly: "an unknown
        # value is refused rather than guessed." If this guard is removed, an
        # operator's typo in --checksum-algorithm would silently fall through
        # to some default header instead of refusing, and the request would
        # go out with a checksum this tool never actually computed for it.
        with self.assertRaises(checksum.ChecksumError):
            checksum.checksum_header("sha1", self.BODY)

    def test_two_different_bodies_never_share_a_header_value(self):
        # Abuse case for the exact-bytes requirement. If `checksum_header`
        # ever memoised on algorithm alone, or read from a cached body, two
        # distinct batches would carry the same checksum and a store that
        # only checks the header's shape (not its value) would accept a
        # delete request its own verification never actually covered.
        one, _ = checksum.checksum_header(checksum.MD5, b"batch one")
        other, value_other = checksum.checksum_header(checksum.MD5, b"batch two")
        _, value_one = checksum.checksum_header(checksum.MD5, b"batch one")
        self.assertNotEqual(value_one, value_other)


if __name__ == "__main__":
    unittest.main()
