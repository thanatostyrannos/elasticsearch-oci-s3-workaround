"""The readers for the four formats Elasticsearch owns.

These are reimplementations of somebody else's file formats, which
CONTRIBUTING names as one of the few things worth testing. Each check below
refuses rather than repairs, and each refusal is what turns a format surprise
into a smaller manifest instead of a wrong one.
"""

import json
import os
import struct
import sys
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import genchain_fixtures as fx
from generation_chain.errors import BlobFormatError, ShapeGateError
from generation_chain.formats.codec import unwrap
from generation_chain.formats.latest import parse_index_latest
from generation_chain.formats.repository_data import (parse_repository_data,
                                                      root_generation_number)
from generation_chain.formats.shard_snapshots import (parse_shard_snapshots,
                                                      segment_stem)
from generation_chain.formats.smile import decode_smile


def shard_blob(document, **kwargs):
    return fx.codec_wrap(json.dumps(document).encode("utf-8"), **kwargs)


class IndexLatest(unittest.TestCase):

    def test_eight_big_endian_bytes_name_the_current_generation(self):
        # Everything else in the run hangs off this number. Reading it
        # little-endian or from the wrong offset anchors the whole derivation
        # at a generation that is not current, and every comparison after that
        # compares two different states of the repository.
        self.assertEqual(parse_index_latest(struct.pack(">q", 258)), 258)

    def test_a_pointer_of_the_wrong_length_is_refused(self):
        # Abuse case. A short or padded `index.latest` is what a truncated
        # copy leaves behind, and it is the one input that would silently move
        # the anchor rather than fail.
        for data in (b"\x00" * 7, b"\x00" * 9, b""):
            with self.assertRaises(BlobFormatError):
                parse_index_latest(data)


class RootGenerationKeys(unittest.TestCase):

    def test_a_numeric_shard_generation_is_not_a_root_generation(self):
        # Shard generations can be numeric, so `indices/<uuid>/0/index-3`
        # matches the same pattern as a root catalog. A scan that accepted it
        # at any depth would read a shard file list as a repository catalog
        # and derive a delete history from it.
        self.assertEqual(root_generation_number("index-3"), 3)
        self.assertIsNone(root_generation_number("indices/abc/0/index-3"))
        self.assertIsNone(root_generation_number("index-3Dk9ckdxTpCo"))
        self.assertIsNone(root_generation_number("index.latest"))


class RepositoryDataShapeGate(unittest.TestCase):

    def parse(self, document):
        document.setdefault("min_version", "7.12.0")
        return parse_repository_data(json.dumps(document).encode(), 4)

    def test_a_catalog_with_both_halves_present_parses(self):
        # The use case the gate has to let through, including a `_na_` uuid,
        # which is what a repository that has never been assigned one writes.
        parsed = self.parse({
            "uuid": "_na_",
            "snapshots": [{"name": "s", "uuid": "u",
                           "index_metadata_lookup": {"i": "L"}}],
            "indices": {"idx": {"id": "i", "snapshots": ["u"],
                                "shard_generations": ["g", None]}},
            "index_metadata_identifiers": {"L": "blob"}})
        self.assertEqual(parsed.repository_uuid, "_na_")
        self.assertEqual(parsed.indices["i"].shard_generation(1), None)
        self.assertEqual(parsed.indices["i"].shard_generation(9), None)

    def test_a_missing_snapshots_array_is_never_an_empty_catalog(self):
        # Abuse case, and the single most expensive misreading available. An
        # empty catalog says every snapshot in the previous generation was
        # just deleted, which is the largest manifest this tool could produce.
        with self.assertRaises(ShapeGateError):
            self.parse({"uuid": "u", "indices": {}})

    def test_a_shard_document_is_not_mistaken_for_a_catalog(self):
        # A BlobStoreIndexShardSnapshots has a `snapshots` field too, and it
        # is an object. Requiring a list is the second guard behind the
        # key-depth check, so a shard document that reached this parser by
        # some other route still cannot become a repository history.
        with self.assertRaises(ShapeGateError):
            self.parse({"uuid": "u", "files": [],
                        "snapshots": {"s": {"files": []}}, "indices": {}})

    def test_a_lookup_entry_that_is_not_two_strings_is_refused(self):
        # Abuse case, and the decision is the refusal itself rather than what
        # happens downstream. A comprehension that filters on isinstance is a
        # refusal nobody wrote: the entry vanishes, nothing is recorded, and
        # the live set built from what remains is short by one index. Every
        # silent filter in this module is one of these waiting to happen.
        # The document is otherwise consistent, so this isolates the typing
        # check from the completeness cross-check that would also catch a
        # lookup gone short.
        with self.assertRaises(ShapeGateError):
            self.parse({"uuid": "u",
                        "snapshots": [{"name": "s", "uuid": "u2",
                                       "index_metadata_lookup": {"i": 12345}}],
                        "indices": {"idx": {"id": "i", "snapshots": ["u2"],
                                            "shard_generations": ["g"]}}})

    def test_a_snapshot_with_no_uuid_is_refused(self):
        # Abuse case. Snapshots are compared between generations by uuid, so a
        # catalog whose entries cannot be identified would make every snapshot
        # in it look deleted in the next generation.
        with self.assertRaises(ShapeGateError):
            self.parse({"uuid": "u", "indices": {},
                        "snapshots": [{"name": "s"}]})


class ShardDocuments(unittest.TestCase):

    def test_a_document_yields_its_snapshots_and_their_segment_blobs(self):
        # The use case, including the two things that are easy to get
        # backwards: the `snapshots` object is keyed by snapshot NAME, and its
        # file lists hold BLOB names rather than physical Lucene names.
        parsed = parse_shard_snapshots(shard_blob({
            "files": [{"name": "__a", "physical_name": "_0.cfs"},
                      {"name": "v__b", "physical_name": "segments_3"}],
            "snapshots": {"s1": {"files": ["__a", "v__b"]}}}), "where")
        self.assertEqual(parsed.by_snapshot_name["s1"], frozenset({"__a"}))
        self.assertEqual(parsed.blob_names, frozenset({"__a"}))

    def test_a_renamed_files_array_raises_rather_than_yielding_nothing(self):
        # Abuse case with a measured price. Renaming one field in this
        # document once deleted 96.4% of a rig repository by bytes, because a
        # document that yielded no names read as "this shard references
        # nothing" instead of as a document nobody could parse.
        with self.assertRaises(ShapeGateError):
            parse_shard_snapshots(shard_blob({
                "fileList": [{"name": "__a"}],
                "snapshots": {"s1": {"files": []}}}), "where")

    def test_a_snapshot_naming_a_file_the_document_does_not_declare_raises(self):
        # Abuse case for a half-decoded document. The `files` array and the
        # per-snapshot lists are written from one state, so a disagreement
        # means one of them was decoded wrongly and there is no way to tell
        # which. Picking a half would attribute a file list nobody wrote.
        with self.assertRaises(ShapeGateError):
            parse_shard_snapshots(shard_blob({
                "files": [{"name": "__a", "physical_name": "_0.cfs"}],
                "snapshots": {"s1": {"files": ["__a", "__ghost"]}}}), "where")

    def test_one_predicate_decides_what_a_segment_is(self):
        # Two predicates that disagree about what a segment is will always end
        # up naming a live object: a live file list holding `__a.part0` was
        # invisible to a live-set predicate that rejected the dot, while the
        # attachment side hung `__a.part0` off a condemned `__a`.
        self.assertEqual(segment_stem("__a"), "__a")
        self.assertEqual(segment_stem("__a.part0"), "__a")
        self.assertEqual(segment_stem("__a.part17"), "__a")
        self.assertIsNone(segment_stem("v__a"))
        self.assertIsNone(segment_stem("snap-x.dat"))
        self.assertIsNone(segment_stem("__a.part"))


class CodecFraming(unittest.TestCase):

    def test_a_wrapped_payload_survives_deflate_and_comes_back(self):
        # The use case for both spellings Elasticsearch uses. A reader that
        # handled only the uncompressed form would drop every shard in a
        # repository written with compression on, and report an empty manifest
        # while looking healthy.
        for deflate in (False, True):
            self.assertEqual(
                unwrap(fx.codec_wrap(b'{"files": [], "snapshots": {}}',
                                     deflate=deflate)),
                {"files": [], "snapshots": {}})

    def test_a_blob_whose_checksum_does_not_match_is_refused(self):
        # Abuse case. A blob half-overwritten by a later write, or truncated
        # by a copy tool, still carries a plausible header and plausible
        # framing. The checksum is the only thing separating those from a
        # document, and reading one anyway attributes a file list nobody
        # wrote.
        blob = bytearray(fx.codec_wrap(b'{"files": [], "snapshots": {}}'))
        blob[-1] ^= 0xFF
        with self.assertRaises(BlobFormatError):
            unwrap(bytes(blob))

    def test_framing_that_is_absent_or_truncated_is_refused(self):
        # Abuse case for the two shapes a partial download takes.
        with self.assertRaises(BlobFormatError):
            unwrap(b"\x00" * 40)
        full = fx.codec_wrap(b'{"files": [], "snapshots": {}}')
        with self.assertRaises(BlobFormatError):
            unwrap(full[:12])


class Smile(unittest.TestCase):

    def test_the_token_classes_elasticsearch_writes_decode(self):
        # A known-answer test over hand-built bytes, because the shared
        # back-reference tables are the part a reimplementation gets subtly
        # wrong: the reader has to add exactly what the writer added, in the
        # same order, or the document decodes into a DIFFERENT document
        # rather than into an error.
        data = (b":)\n\x05"
                b"\xfa"                       # start object
                b"\x83name" b"E__abcd"        # short ascii key, tiny ascii value
                b"\x83size" b"\xc4"           # small int, zigzag 4 -> 2
                b"\x83flag" b"\x23"           # true
                b"\x41" b"\xc6"               # shared key 1 is "size", now 3
                b"\x83list" b"\xf8\x22\x23\xf9"  # array of false, true
                b"\xfb")
        self.assertEqual(decode_smile(data),
                         {"name": "__abcd", "size": 3, "flag": True,
                          "list": [False, True]})

    def test_a_reserved_token_raises_instead_of_resynchronising(self):
        # Abuse case for a document from a version nobody tested against. A
        # decoder that skipped what it did not understand would carry on and
        # produce a file list that no Elasticsearch ever wrote.
        with self.assertRaises(BlobFormatError):
            decode_smile(b":)\n\x05\xfa\x83name\x2c\xfb")

    def test_a_back_reference_to_a_table_the_header_disabled_raises(self):
        # Abuse case for a header flag byte that does not describe the body.
        # Answering a back reference out of an empty table would silently
        # rename a field.
        with self.assertRaises(BlobFormatError):
            decode_smile(b":)\n\x00\xfa\x40\x21\xfb")


class TheCatalogsTwoHalvesMustAgree(unittest.TestCase):
    """RepositoryData states the same fact twice, and both readings must match.

    Elasticsearch writes the snapshots array and the indices map from one
    state, so every index a live snapshot references appears in the map and
    every snapshot an index names appears in the array. When they disagree, one
    half was decoded wrongly and there is no way to tell which. Reading on
    builds a live set out of half a document, and a live set that is too small
    is how this tool comes to name a blob that is still in use.

    This is the check the shard survey used to repeat. It is here instead
    because this is where it can actually fire.
    """

    def _catalog(self, **changes):
        document = {
            "min_version": "7.12.0", "uuid": "u", "cluster_id": "c",
            "snapshots": [{"name": "s2", "uuid": "uuid-s2", "state": 1,
                           "index_metadata_lookup": {"iuuid-i": "lookup-i"}}],
            "indices": {"i": {"id": "iuuid-i", "snapshots": ["uuid-s2"],
                              "shard_generations": ["g"]}},
            "index_metadata_identifiers": {"lookup-i": "md-i"},
        }
        document.update(changes)
        return json.dumps(document).encode("utf-8")

    def test_a_healthy_catalog_parses(self):
        # The abuse case, first. A cross-check that refused every document
        # would drop every generation of every repository, and the tool would
        # report an empty manifest while looking careful.
        parsed = parse_repository_data(self._catalog(), 1)
        self.assertEqual({"uuid-s2"}, set(parsed.snapshots))

    def test_an_index_naming_a_snapshot_the_array_lacks_is_refused(self):
        with self.assertRaises(ShapeGateError):
            parse_repository_data(self._catalog(indices={
                "i": {"id": "iuuid-i", "snapshots": ["uuid-gone", "uuid-s2"],
                      "shard_generations": ["g"]}}), 1)

    def test_a_snapshot_referencing_an_index_the_map_lacks_is_refused(self):
        with self.assertRaises(ShapeGateError):
            parse_repository_data(self._catalog(snapshots=[{
                "name": "s2", "uuid": "uuid-s2", "state": 1,
                "index_metadata_lookup": {"iuuid-i": "lookup-i",
                                          "iuuid-missing": "lookup-x"}}]), 1)

    def test_an_index_whose_snapshot_lookup_omits_it_is_refused(self):
        # The direction that costs data. A snapshot whose lookup is SHORT by
        # one index still parses, and the live set built from it is then short
        # by one index metadata blob the snapshot is still using.
        with self.assertRaises(ShapeGateError):
            parse_repository_data(self._catalog(snapshots=[{
                "name": "s2", "uuid": "uuid-s2", "state": 1,
                "index_metadata_lookup": {}}]), 1)


if __name__ == "__main__":
    unittest.main()
