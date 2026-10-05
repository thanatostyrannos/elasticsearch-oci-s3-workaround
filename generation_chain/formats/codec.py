"""ChecksumBlobStoreFormat: a Lucene codec header and footer around a payload.

Elasticsearch wraps its shard documents in `CodecUtil` framing, optionally
DEFLATE-compresses the payload, and writes the content as either Jackson SMILE
or JSON. Every check below refuses rather than guesses, because the caller
turns a refusal into a dropped shard and a guess into an attribution.
"""

from __future__ import annotations

import json
import struct
import zlib
from typing import Any, Tuple, Type

from ..errors import BlobFormatError
from .smile import SMILE_SIGNATURE, decode_smile

CODEC_MAGIC = 0x3FD76C17
FOOTER_MAGIC = (~CODEC_MAGIC) & 0xFFFFFFFF
DEFLATE_MARKER = b"DFL\x00"
FOOTER_LENGTH = 16
MAX_VINT_SHIFT = 35
# ChecksumBlobStoreFormat writes this version and reads no other.
FORMAT_VERSION = 1
# Lucene's only checksum algorithm id, a CRC32.
CHECKSUM_ALGORITHM = 0


def read_vint(
    data: bytes,
    offset: int,
    error: Type[BlobFormatError] = BlobFormatError,
    subject: str = "codec header",
) -> Tuple[int, int]:
    """Lucene's writeVInt: little-endian seven-bit groups, high bit continues.

    Returns the value and the offset after it. Raises `error`, naming
    `subject`, when the data ends inside the vint or the vint runs longer
    than a 32-bit value can need.
    """
    value = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise error(f"{subject} ends inside a vint")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > MAX_VINT_SHIFT:
            raise error(f"{subject} vint is too long")


def unwrap(data: bytes, codec_name: str) -> Any:
    """Strip the framing and decode the payload to plain Python values.

    `codec_name` is the format the caller expects, `snapshots` for a shard
    document and `snapshot` for a snapshot document. Elasticsearch's own
    reader refuses any other name, and any version but 1.
    """
    if len(data) < 4 + FOOTER_LENGTH:
        raise BlobFormatError("blob is too short to carry codec framing")
    if struct.unpack_from(">I", data, 0)[0] != CODEC_MAGIC:
        raise BlobFormatError("missing Lucene codec header")
    name_length, offset = read_vint(data, 4)
    name = data[offset:offset + name_length]
    offset += name_length
    if offset + 4 + FOOTER_LENGTH > len(data):
        raise BlobFormatError("codec header runs past the end of the blob")
    if name != codec_name.encode("utf-8"):
        raise BlobFormatError(
            f"blob is framed as codec {name!r}, not {codec_name!r}")
    version = struct.unpack_from(">I", data, offset)[0]
    if version != FORMAT_VERSION:
        raise BlobFormatError(
            f"blob carries format version {version}, not {FORMAT_VERSION}")
    offset += 4
    check_footer(data, len(data) - FOOTER_LENGTH)
    payload = data[offset:len(data) - FOOTER_LENGTH]
    return decode_payload(payload)


def check_footer(data: bytes, body_end: int,
                 error: Type[BlobFormatError] = BlobFormatError,
                 subject: str = "blob") -> None:
    """Lucene's codec footer at `body_end`: magic, algorithm 0 and a CRC32.

    The CRC covers every byte before the checksum itself, footer magic and
    algorithm id included. Raises `error`, naming `subject`, for anything
    else. Both Lucene framing readers in this package call this, so a
    footer one of them refuses the other refuses too.
    """
    footer_magic, algorithm = struct.unpack_from(">II", data, body_end)
    if footer_magic != FOOTER_MAGIC or algorithm != CHECKSUM_ALGORITHM:
        raise error(f"{subject} is missing its Lucene codec footer")
    stored_crc = struct.unpack_from(">Q", data, body_end + 8)[0]
    if stored_crc != zlib.crc32(data[:body_end + 8]) & 0xFFFFFFFF:
        # A blob half-overwritten by a later write, or truncated by a copy
        # tool, still carries a plausible header. The checksum is the only
        # thing that separates those from a document, and reading one anyway
        # would attribute a file list nobody wrote.
        raise error(f"{subject} footer checksum does not match its body")


def decode_payload(payload: bytes) -> Any:
    """Inflate if needed, then decode SMILE or JSON."""
    if payload.startswith(DEFLATE_MARKER):
        payload = _inflate(payload[len(DEFLATE_MARKER):])
    if payload.startswith(SMILE_SIGNATURE):
        return decode_smile(payload)
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BlobFormatError(f"payload is neither SMILE nor JSON: {exc}") from exc


def _inflate(body: bytes) -> bytes:
    for window in (15, -15, 47):
        try:
            return zlib.decompress(body, window)
        except zlib.error:
            continue
    raise BlobFormatError("DEFLATE payload did not decompress")
