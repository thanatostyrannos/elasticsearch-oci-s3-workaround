"""`segments_N`: Lucene's own commit point, read as a second opinion.

Issue #1 found that the Elasticsearch file list is not enough on its own.
Elasticsearch keeps two copies of a shard's file list in sync (`index-<gen>`
and the per-snapshot `snap-<uuid>.dat`), and both are read out of the same
object store this tool reads. A tamper that removes the same live segment
from both copies, and patches the counts to match, satisfies every check that
compares the two copies to each other, because the two copies agree. A
genuine upstream format change produces the identical drift by accident: both
copies are written from one in-memory list, so a change that moves what
Elasticsearch writes moves both at once.

`segments_N` breaks that common mode. It is Lucene's own record of which
segments the index needs to open, written by a different layer for a
different reason than the snapshot file list, and it never round-trips
through Elasticsearch's corroboration. A file list that under-references what
this commit point requires is drift this reader can see without asking
Elasticsearch anything.

Verified against `tests/fixtures/real-es952-shard-index-gen.bin` and the two
repository archives alongside it: every commit point those fixtures carry, 14
in total across generations, decodes under this format, byte for byte, up to
a footer checksum that matches. Real Elasticsearch 9.5.2 stores every one of
them inline, as a `v__` entry carrying its bytes in the file entry's
`meta_hash` field, so reading one costs no extra round trip to the object
store. That is a property of the sample, not a guarantee this reader assumes:
see `required_segment_names`.

A cheaper approach was tried and measured before this one was built: scanning
the inline bytes for substrings that look like segment names. It matched the
real file list on 0 of 9 real commit points, because a commit's user-data map
carries keys and values that are shape-identical to a segment name and a scan
cannot tell them apart. This reader decodes the structure instead, so a
segment name is only ever read from the field the format defines for it.

THE LIMIT, STATED ONCE HERE. This is a second opinion, not a lock. Reaching
it needs the same object-store write access that lets an attacker delete the
blobs directly, and a patient attacker with that access could rewrite this
commit point too. Against that adversary this raises the bar rather than
closing the door. Against the realistic case, an upstream format change that
nobody staged, the two are different sources of truth written by different
code for different reasons, and a change to one does not silently move the
other.
"""

from __future__ import annotations

import re
import struct
import zlib
from typing import FrozenSet, Set

from ..errors import BlobFormatError
from .codec import CODEC_MAGIC, FOOTER_MAGIC

SEGMENTS_CODEC_NAME = "segments"
FOOTER_LENGTH = 16
# Lucene names a segment "_" followed by a base36 counter, and writes that
# exact shape into the name field of every segment entry. A value that does
# not match it is proof the read is no longer aligned with the format, not a
# real segment this tool has not seen the shape of yet.
SEGMENT_NAME = re.compile(r"^_[0-9a-zA-Z]+$")
MAX_VINT_SHIFT = 35


class SegmentsFileError(BlobFormatError):
    """The bytes handed to this reader are not a `segments_N` commit point.

    Raised on anything the format does not define: a bad header, a count that
    implies more data than remains, a segment name of the wrong shape, or a
    footer checksum that does not match the body. The caller's job is to
    treat this the same as a commit point it never got the bytes for, which
    is documented at the one place that decides what happens next:
    `required_segment_names`.
    """


class _Cursor:
    """A position in a byte string, with the reads `segments_N` is built from.

    Every read checks its own bounds and raises `SegmentsFileError` rather
    than letting a slice run past the end return short. A short slice on a
    truncated blob would parse as a plausible smaller value instead of as the
    truncation it is.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.position = 0

    def _take(self, size: int) -> bytes:
        end = self.position + size
        if end > len(self._data):
            raise SegmentsFileError(
                f"segments_N ends {end - len(self._data)} byte(s) into a "
                "field that needed more data")
        chunk = self._data[self.position:end]
        self.position = end
        return chunk

    def byte(self) -> int:
        return self._take(1)[0]

    def bytes(self, size: int) -> bytes:
        return self._take(size)

    def uint32(self) -> int:
        return struct.unpack(">I", self._take(4))[0]

    def int32(self) -> int:
        return struct.unpack(">i", self._take(4))[0]

    def int64(self) -> int:
        return struct.unpack(">q", self._take(8))[0]

    def vint(self) -> int:
        """Lucene's variable-length integer: little-endian groups of 7 bits."""
        value = 0
        shift = 0
        while True:
            piece = self.byte()
            value |= (piece & 0x7F) << shift
            if not piece & 0x80:
                return value
            shift += 7
            if shift > MAX_VINT_SHIFT:
                raise SegmentsFileError("segments_N carries an oversized vint")

    def string(self) -> str:
        length = self.vint()
        raw = self._take(length)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SegmentsFileError(
                f"segments_N string field is not valid UTF-8: {exc}") from exc


def _skip_string_set(cursor: _Cursor) -> None:
    for _ in range(cursor.vint()):
        cursor.string()


def _skip_string_map(cursor: _Cursor) -> None:
    for _ in range(cursor.vint()):
        cursor.string()  # key
        cursor.string()  # value


def _skip_docvalues_updates(cursor: _Cursor) -> None:
    for _ in range(cursor.int32()):
        cursor.int32()  # field number
        _skip_string_set(cursor)  # that field's update files


def _skip_lucene_version(cursor: _Cursor) -> None:
    cursor.vint()  # major
    cursor.vint()  # minor
    cursor.vint()  # bugfix


def _read_segment_name(cursor: _Cursor) -> str:
    name = cursor.string()
    if not SEGMENT_NAME.match(name):
        raise SegmentsFileError(
            f"segments_N names a segment {name!r}, which is not the "
            "_<base36> shape Lucene writes")
    return name


def _read_one_segment(cursor: _Cursor) -> str:
    """One entry of the segment loop. Returns its segment name.

    Field order and sizes are `SegmentInfos.parseSegmentInfos` verbatim:
    every count and generation here is a fixed-width int or long, never a
    vint, which is the field this reader got wrong on the first pass against
    the captured fixture before the footer checksum caught the misalignment.
    This module never reads a segment's own `.si` file. Doing that would need
    a second, independent reimplementation of a second Lucene format, which
    is the reimplementation-on-reimplementation cost this fix was built to
    avoid taking on twice over.
    """
    name = _read_segment_name(cursor)
    cursor.bytes(16)  # segment id, opaque to this reader
    cursor.string()  # segment info codec name, opaque to this reader
    cursor.int64()  # deletion generation
    delete_count = cursor.int32()
    if delete_count < 0:
        raise SegmentsFileError(
            f"segments_N names segment {name!r} with a negative deletion "
            f"count ({delete_count})")
    cursor.int64()  # field-infos generation
    cursor.int64()  # doc-values generation
    soft_delete_count = cursor.int32()
    if soft_delete_count < 0:
        raise SegmentsFileError(
            f"segments_N names segment {name!r} with a negative soft "
            f"deletion count ({soft_delete_count})")
    marker = cursor.byte()
    if marker == 1:
        cursor.bytes(16)  # segment commit id, opaque to this reader
    elif marker != 0:
        raise SegmentsFileError(
            f"segments_N names segment {name!r} with commit-id marker "
            f"{marker}, which is neither 0 nor 1")
    _skip_string_set(cursor)  # field-infos files
    _skip_docvalues_updates(cursor)
    return name


def _check_header(cursor: _Cursor) -> None:
    if cursor.uint32() != CODEC_MAGIC:
        raise SegmentsFileError("segments_N is missing its Lucene codec header")
    codec_name = cursor.string()
    if codec_name != SEGMENTS_CODEC_NAME:
        raise SegmentsFileError(
            f"segments_N carries codec name {codec_name!r}, not "
            f"{SEGMENTS_CODEC_NAME!r}")
    cursor.int32()  # header format version, not checked here
    cursor.bytes(16)  # writer id, opaque to this reader
    cursor.string()  # generation suffix, e.g. "4" for segments_4


def _check_footer(data: bytes, body_end: int) -> None:
    if body_end + FOOTER_LENGTH != len(data):
        raise SegmentsFileError(
            "segments_N has trailing bytes after its footer, or its body "
            "runs past where the footer should start")
    footer_magic = struct.unpack_from(">I", data, body_end)[0]
    algorithm_id = struct.unpack_from(">I", data, body_end + 4)[0]
    if footer_magic != FOOTER_MAGIC or algorithm_id != 0:
        raise SegmentsFileError("segments_N is missing its Lucene codec footer")
    stored_crc = struct.unpack_from(">Q", data, body_end + 8)[0]
    computed_crc = zlib.crc32(data[:body_end + 8]) & 0xFFFFFFFF
    if stored_crc != computed_crc:
        # Same reasoning as the outer document footer in codec.py: a blob
        # half-overwritten or truncated in place still carries a plausible
        # header, and the checksum is the only thing that catches it.
        raise SegmentsFileError(
            "segments_N footer checksum does not match its body")


def required_segment_names(data: bytes) -> FrozenSet[str]:
    """Every Lucene segment this commit point says the index needs to open.

    Raises `SegmentsFileError` on anything that does not decode as a
    `segments_N` file: a bad header, a count implying more data than is
    there, a segment name of the wrong shape, or a footer whose checksum
    does not match. The caller must treat that exactly like a commit point it
    was never handed the bytes for, dropping the shard from consideration
    rather than trusting a file list this could not corroborate. Every
    uncertainty here resolves toward naming fewer keys, never more.
    """
    cursor = _Cursor(data)
    _check_header(cursor)
    _skip_lucene_version(cursor)  # the Lucene version that wrote this commit
    cursor.vint()  # major version the index was created under
    cursor.int64()  # segment-infos version, incremented on every change
    cursor.vint()  # counter used to name the next new segment
    count = cursor.int32()
    if count < 0:
        raise SegmentsFileError(
            f"segments_N declares a negative segment count ({count})")
    if count > 0:
        _skip_lucene_version(cursor)  # the oldest segment's Lucene version
    names: Set[str] = set()
    for _ in range(count):
        names.add(_read_one_segment(cursor))
    _skip_string_map(cursor)  # commit user data, e.g. seq-no bookkeeping
    _check_footer(data, cursor.position)
    return frozenset(names)
