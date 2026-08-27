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
from typing import Any, Tuple

from ..errors import BlobFormatError
from .smile import SMILE_SIGNATURE, decode_smile

CODEC_MAGIC = 0x3FD76C17
FOOTER_MAGIC = (~CODEC_MAGIC) & 0xFFFFFFFF
DEFLATE_MARKER = b"DFL\x00"
FOOTER_LENGTH = 16
MAX_VINT_SHIFT = 35


def _read_vint(data: bytes, offset: int) -> Tuple[int, int]:
    """Lucene's writeVInt: little-endian seven-bit groups, high bit continues."""
    value = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise BlobFormatError("codec header ends inside a vint")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > MAX_VINT_SHIFT:
            raise BlobFormatError("codec header vint is too long")


def unwrap(data: bytes) -> Any:
    """Strip the framing and decode the payload to plain Python values."""
    if len(data) < 4 + FOOTER_LENGTH:
        raise BlobFormatError("blob is too short to carry codec framing")
    if struct.unpack_from(">I", data, 0)[0] != CODEC_MAGIC:
        raise BlobFormatError("missing Lucene codec header")
    name_length, offset = _read_vint(data, 4)
    offset += name_length + 4  # codec name, then the format version
    if offset + FOOTER_LENGTH > len(data):
        raise BlobFormatError("codec header runs past the end of the blob")
    if struct.unpack_from(">I", data, len(data) - FOOTER_LENGTH)[0] != FOOTER_MAGIC:
        raise BlobFormatError("missing Lucene codec footer")
    stored_crc = struct.unpack_from(">Q", data, len(data) - 8)[0]
    if stored_crc != zlib.crc32(data[:-8]) & 0xFFFFFFFF:
        # A blob half-overwritten by a later write, or truncated by a copy
        # tool, still carries a plausible header. The checksum is the only
        # thing that separates those from a document, and reading one anyway
        # would attribute a file list nobody wrote.
        raise BlobFormatError("codec footer checksum does not match the body")
    payload = data[offset:len(data) - FOOTER_LENGTH]
    return decode_payload(payload)


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
