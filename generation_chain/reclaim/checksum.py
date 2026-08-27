"""The content checksum a batch delete needs, computed over exact bytes.

`DeleteObjects` is checksum-required on every store this project has measured.
Which checksum a store accepts is a property of the store, not of this tool,
so the algorithm is a configuration value with an explicit default rather than
a constant, and a value this module does not recognise is refused rather than
guessed at. Guessing wrong here does not fail loudly: the request that would
have named a header this store rejects instead names one it does not, so the
run gets the store's generic 400 rather than the specific one telling an
operator what to change.

The four values below are the same four the top-level README documents as the
menu proposed upstream (`checksum_algorithm: crc32c | sha256 | crc32 | md5`)
after `Content-MD5` stopped being sent automatically. `md5` is `Content-MD5`
itself, the header this project's own lab MinIO and Oracle's Amazon S3
Compatibility API require. `crc32c` and `sha256` are what Oracle's
documentation names as the accepted alternatives. `crc32` is genuine AWS S3's
own accepted alternative, named in the issue this package answers.

CRC32C IS NOT IN THE STANDARD LIBRARY. `zlib.crc32` and `binascii.crc32` both
compute the IEEE 802.3 polynomial, which is a different checksum from the
Castagnoli polynomial S3 calls `crc32c`. There is no vendored dependency for
it here, in keeping with this project's standard-library-only rule, so the
table is built once at import time from the polynomial's definition.
"""

from __future__ import annotations

import base64
import hashlib
import struct
import zlib
from typing import Tuple

from ..errors import GenerationChainError

MD5 = "md5"
CRC32 = "crc32"
CRC32C = "crc32c"
SHA256 = "sha256"

# Header name a request carries for each algorithm, and the base64 value's
# expected decoded length, used only to catch a hand-typed header early
# rather than send a malformed one and wait for the store to say so.
_HEADER_NAMES = {
    MD5: "Content-MD5",
    CRC32: "x-amz-checksum-crc32",
    CRC32C: "x-amz-checksum-crc32c",
    SHA256: "x-amz-checksum-sha256",
}

SUPPORTED_ALGORITHMS: Tuple[str, ...] = (MD5, CRC32, CRC32C, SHA256)
DEFAULT_ALGORITHM = MD5


class ChecksumError(GenerationChainError):
    """The algorithm named is not one this package can compute or send."""


def _crc32c_table():
    # The reflected Castagnoli polynomial, 0x82F63B78, standard bit-at-a-time
    # construction. Built once and cached rather than reasoned about per byte.
    table = []
    for byte in range(256):
        value = byte
        for _ in range(8):
            value = (value >> 1) ^ (0x82F63B78 if value & 1 else 0)
        table.append(value)
    return tuple(table)


_CRC32C_TABLE = _crc32c_table()


def crc32c(data: bytes) -> int:
    """CRC-32C (Castagnoli), the checksum S3 calls `crc32c`.

    Not `zlib.crc32`: that computes the IEEE 802.3 polynomial S3 calls plain
    `crc32`, a different 32-bit value over the same bytes.
    """
    crc = 0xFFFFFFFF
    for byte in data:
        crc = _CRC32C_TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def _digest(algorithm: str, body: bytes) -> bytes:
    if algorithm == MD5:
        # Content-MD5 is an S3 protocol checksum for integrity, not a security
        # hash, and it plays no part in the delete-authorisation digest, which
        # is SHA-256 in approval.py. Unmarked, this raises on a host with
        # OpenSSL in FIPS mode, so the tool would stop working on exactly the
        # hosts most likely to be running it.
        return hashlib.md5(body, usedforsecurity=False).digest()
    if algorithm == CRC32:
        return struct.pack(">I", zlib.crc32(body))
    if algorithm == CRC32C:
        return struct.pack(">I", crc32c(body))
    return hashlib.sha256(body).digest()  # algorithm == SHA256, checked below


def checksum_header(algorithm: str, body: bytes) -> Tuple[str, str]:
    """The header name and base64 value for `body`, under `algorithm`.

    `body` must be the exact bytes about to go on the wire. A checksum
    computed over anything else, a re-rendered copy, a re-encoded string,
    proves nothing about what the store actually receives.
    """
    if algorithm not in _HEADER_NAMES:
        raise ChecksumError(
            f"{algorithm!r} is not a checksum this package can compute; the "
            f"store decides which one it needs, and the supported values are "
            f"{', '.join(SUPPORTED_ALGORITHMS)}. An unrecognised value is "
            f"refused rather than sent as one of these and silently checked "
            f"against the wrong bytes")
    value = base64.b64encode(_digest(algorithm, body)).decode("ascii")
    return _HEADER_NAMES[algorithm], value
