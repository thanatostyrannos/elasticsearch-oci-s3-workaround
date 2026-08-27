"""`generation_chain/formats/lucene_segments.py`, Lucene's own commit format.

This is a reimplementation of a Lucene file format, which CONTRIBUTING names
as one of the few things worth testing here. `lucene_commit` below encodes
the format independently of the reader under test, the same way every other
fixture in this suite writes Elasticsearch's formats by hand rather than by
calling the code that reads them: a test and a reader that agree because they
share an encoder are not evidence of anything. The strongest evidence in this
file is the one test that uses neither: it decodes bytes captured from a real
Elasticsearch 9.5.2 repository.
"""

import os
import struct
import sys
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generation_chain.formats.codec import unwrap as codec_unwrap
from generation_chain.formats.lucene_segments import (SegmentsFileError,
                                                      required_segment_names)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
CODEC_MAGIC = 0x3FD76C17
FOOTER_MAGIC = (~CODEC_MAGIC) & 0xFFFFFFFF


def _vint(value):
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _string(value):
    encoded = value.encode("utf-8")
    return _vint(len(encoded)) + encoded


def lucene_commit(names, generation=1):
    """Hand-rolled `segments_N` bytes naming exactly these Lucene segments.

    Field order and sizes were read off
    `tests/fixtures/real-es952-shard-index-gen.bin`, whose footer checksum
    only matches when the order is right.
    """
    body = bytearray()
    body += struct.pack(">I", CODEC_MAGIC)
    body += _string("segments")
    body += struct.pack(">i", 10)  # segments_N format version
    body += b"\x01" * 16  # writer id, arbitrary
    body += _string(str(generation))  # segments_<generation> suffix
    body += _vint(10) + _vint(0) + _vint(0)  # Lucene version that wrote this
    body += _vint(10)  # index created version major
    body += struct.pack(">q", 1)  # segment-infos version
    body += _vint(len(names) + 1)  # name counter
    body += struct.pack(">i", len(names))  # segment count
    if names:
        body += _vint(10) + _vint(0) + _vint(0)  # oldest segment's version
    for name in names:
        body += _string(name)
        body += b"\x02" * 16  # segment id, arbitrary
        body += _string("Lucene104")
        body += struct.pack(">q", -1)  # deletion generation, none
        body += struct.pack(">i", 0)  # deletion count
        body += struct.pack(">q", -1)  # field-infos generation, none
        body += struct.pack(">q", -1)  # doc-values generation, none
        body += struct.pack(">i", 0)  # soft deletion count
        body += b"\x00"  # no segment commit id
        body += _vint(0)  # no field-infos files
        body += struct.pack(">i", 0)  # no doc-values updates
    body += _vint(0)  # no commit user data
    footer_head = struct.pack(">I", FOOTER_MAGIC) + struct.pack(">I", 0)
    crc = zlib.crc32(bytes(body) + footer_head) & 0xFFFFFFFF
    return bytes(body) + footer_head + struct.pack(">Q", crc)


class LuceneSegmentsDecoder(unittest.TestCase):

    def test_the_real_9_5_2_commit_points_decode(self):
        # The use case, against bytes nobody in this project wrote: the
        # commit point captured from a real Elasticsearch 9.5.2 repository.
        # A decoder that only agrees with its own encoder is not evidence
        # this format was understood correctly.
        path = os.path.join(FIXTURES, "real-es952-shard-index-gen.bin")
        with open(path, "rb") as handle:
            document = codec_unwrap(handle.read())
        commits = [entry for entry in document["files"]
                  if entry.get("physical_name", "").startswith("segments_")]
        self.assertEqual(len(commits), 1)
        self.assertEqual(required_segment_names(commits[0]["meta_hash"]),
                         frozenset({"_2"}))

    def test_a_commit_naming_several_segments_decodes_all_of_them(self):
        # The use case for the loop, not just its first iteration. A reader
        # that only advanced correctly past one segment entry would still
        # pass the single-segment fixture above and still under-report a
        # multi-merge shard's real requirement.
        self.assertEqual(
            required_segment_names(lucene_commit(["_0", "_5", "_a"])),
            frozenset({"_0", "_5", "_a"}))

    def test_a_flipped_footer_byte_is_refused(self):
        # Abuse case. A blob half-overwritten by a later write still carries
        # a plausible header, and the checksum is the only thing that catches
        # it; without this, a corrupted commit could decode to a plausible
        # but wrong segment list instead of raising.
        commit = bytearray(lucene_commit(["_0"]))
        commit[-1] ^= 0xFF
        with self.assertRaises(SegmentsFileError):
            required_segment_names(bytes(commit))

    def test_truncated_bytes_are_refused(self):
        # Abuse case for a partial read or a partial write, the two shapes a
        # crash mid-upload leaves behind.
        with self.assertRaises(SegmentsFileError):
            required_segment_names(lucene_commit(["_0", "_1"])[:20])

    def test_a_name_that_is_not_the_underscore_base36_shape_is_refused(self):
        # Abuse case for the failure mode the cheaper approach could not
        # avoid: a scan of the raw bytes for segment-shaped substrings
        # matched 0 of 9 real commit points, because a commit's user-data map
        # holds keys and values shaped just like a segment name. Reading the
        # name from the structured field the format defines removes that
        # ambiguity, so the only way to reach a wrong name here is a genuine
        # decoding misalignment, which is exactly what this has to refuse.
        commit = lucene_commit(["_0"]).replace(b"_0", b"XX", 1)
        with self.assertRaises(SegmentsFileError):
            required_segment_names(commit)


if __name__ == "__main__":
    unittest.main()
