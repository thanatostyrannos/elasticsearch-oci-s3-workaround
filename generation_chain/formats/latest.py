"""`index.latest`: eight bytes, big-endian, the current root generation."""

from __future__ import annotations

import struct

from ..errors import BlobFormatError

INDEX_LATEST_KEY = "index.latest"
INDEX_LATEST_SIZE = 8


def parse_index_latest(data: bytes) -> int:
    """The generation number Elasticsearch says is current.

    The length check is not pedantry. A truncated or padded `index.latest` is
    the one input that would silently move the anchor of the whole run, and a
    wrong anchor turns every later comparison into a comparison between two
    different repositories' states.
    """
    if len(data) != INDEX_LATEST_SIZE:
        raise BlobFormatError(
            f"index.latest is {len(data)} bytes, not {INDEX_LATEST_SIZE}")
    (value,) = struct.unpack(">q", data)
    if value < 0:
        raise BlobFormatError(f"index.latest names generation {value}")
    return value
