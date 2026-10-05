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
from generation_chain.errors import (BlobFormatError, ShapeGateError,
                                     UnsupportedRepository)
from generation_chain.formats.codec import unwrap
from generation_chain.formats.latest import parse_index_latest
from generation_chain.formats.repository_data import (parse_repository_data,
                                                      root_generation_number)
from generation_chain.formats.shard_snapshots import (parse_shard_snapshots,
                                                      segment_stem)
from generation_chain.formats.smile import decode_smile
from generation_chain.formats.snapshot_document import parse_snapshot_document
from generation_chain.formats.snapshot_document import parse_snapshot_document


def shard_blob(document, **kwargs):
    return fx.codec_wrap(json.dumps(document).encode("utf-8"), **kwargs)


def _framed(payload, codec_name="snapshots"):
    """Codec framing around a payload used exactly as given."""
    return fx.codec_wrap(payload, codec_name=codec_name)


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


def _empty_catalog(**changes):
    """A catalog that passes every gate and holds no snapshot.

    Each shape-gate test below changes one field of this. Built complete,
    because a document missing something else is refused by whichever
    check reads first, and the test then passes with its own check gone.
    """
    document = {"min_version": "7.12.0", "uuid": "u", "cluster_id": "c",
                "snapshots": [], "indices": {},
                "index_metadata_identifiers": {}}
    document.update(changes)
    return document


class RepositoryDataShapeGate(unittest.TestCase):

    def parse(self, document):
        return parse_repository_data(json.dumps(document).encode(), 4)

    def assertRefusedByTheShapeGate(self, document):
        # The floor check raises UnsupportedRepository, a ShapeGateError
        # too. A test that accepted it would pass on the floor alone.
        with self.assertRaises(ShapeGateError) as caught:
            self.parse(document)
        self.assertNotIsInstance(caught.exception, UnsupportedRepository)

    def test_a_catalog_with_both_halves_present_parses(self):
        # The use case the gate has to let through, including a `_na_` uuid,
        # which is what a repository that has never been assigned one writes.
        parsed = self.parse(_empty_catalog(
            uuid="_na_",
            snapshots=[{"name": "s", "uuid": "u",
                        "index_metadata_lookup": {"i": "L"}}],
            indices={"idx": {"id": "i", "snapshots": ["u"],
                             "shard_generations": ["g", None]}},
            index_metadata_identifiers={"L": "blob"}))
        self.assertEqual(parsed.repository_uuid, "_na_")
        self.assertEqual(parsed.indices["i"].shard_generation(1), None)
        self.assertEqual(parsed.indices["i"].shard_generation(9), None)

    def test_a_catalog_holding_no_snapshot_parses(self):
        # Use case paired with the refusals below: the base they each change
        # one field of is a catalog the gate accepts. Without this, every
        # refusal could be the base failing for some other reason.
        self.assertEqual({}, dict(self.parse(_empty_catalog()).snapshots))

    def test_a_missing_snapshots_array_is_never_an_empty_catalog(self):
        # Abuse case, and the single most expensive misreading available. An
        # empty catalog says every snapshot in the previous generation was
        # just deleted, which is the largest manifest this tool could produce.
        # Neutered under "a-catalog-without-a-snapshots-array-is-refused".
        document = _empty_catalog()
        del document["snapshots"]
        self.assertRefusedByTheShapeGate(document)

    def test_a_shard_document_is_not_mistaken_for_a_catalog(self):
        # A BlobStoreIndexShardSnapshots has a `snapshots` field too, and it
        # is an object. Requiring a list is the second guard behind the
        # key-depth check, so a shard document that reached this parser by
        # some other route still cannot become a repository history.
        # Neutered under "the-snapshots-field-must-be-a-list".
        self.assertRefusedByTheShapeGate(_empty_catalog(snapshots={}))

    def test_a_lookup_entry_that_is_not_two_strings_is_refused(self):
        # Abuse case, and the decision is the refusal itself rather than what
        # happens downstream. A comprehension that filters on isinstance is a
        # refusal nobody wrote: the entry vanishes, nothing is recorded, and
        # the live set built from what remains is short by one index. Every
        # silent filter in this module is one of these waiting to happen.
        # The document is otherwise complete, so this isolates the typing
        # check from the completeness cross-check that would also catch a
        # lookup gone short. Neutered under
        # "a-lookup-entry-must-be-two-strings".
        self.assertRefusedByTheShapeGate(_empty_catalog(
            snapshots=[{"name": "s", "uuid": "u2", "state": 1,
                        "index_metadata_lookup": {"i": 12345}}],
            indices={"idx": {"id": "i", "snapshots": ["u2"],
                             "shard_generations": ["g"]}}))

    def test_a_snapshot_with_no_uuid_is_refused(self):
        # Abuse case. Snapshots are compared between generations by uuid, so a
        # catalog whose entries cannot be identified would make every snapshot
        # in it look deleted in the next generation. Neutered under
        # "a-catalog-snapshot-needs-a-uuid".
        self.assertRefusedByTheShapeGate(_empty_catalog(
            snapshots=[{"name": "s", "state": 1,
                        "index_metadata_lookup": {}}]))


class ShardDocuments(unittest.TestCase):

    def test_a_document_yields_its_snapshots_and_their_segment_blobs(self):
        # The use case, including the two things that are easy to get
        # backwards: the `snapshots` object is keyed by snapshot NAME, and its
        # file lists hold BLOB names rather than physical Lucene names.
        parsed = parse_shard_snapshots(shard_blob({
            "files": [{"name": "__a", "physical_name": "_0.cfs", "length": 1},
                      {"name": "v__b", "physical_name": "segments_3",
                       "length": 1}],
            "snapshots": {"s1": {"files": ["__a", "v__b"]}}}), "where")
        self.assertEqual(parsed.by_snapshot_name["s1"], frozenset({"__a"}))
        self.assertEqual(parsed.blob_names, frozenset({"__a"}))

    def test_each_snapshot_s_files_add_up_to_its_byte_total(self):
        # Use case: the extent check compares this sum with the size the
        # snapshot document declares, and that comparison is the one check
        # that sees a short live list. A sum that counted the wrong files, or
        # another snapshot's, would drop healthy shards or pass short ones.
        parsed = parse_shard_snapshots(shard_blob({
            "files": [{"name": "__a", "physical_name": "_0.cfs", "length": 10},
                      {"name": "__b", "physical_name": "_1.cfs", "length": 32},
                      {"name": "v__c", "physical_name": "segments_3",
                       "length": 5}],
            "snapshots": {"s1": {"files": ["__a", "v__c"]},
                          "s2": {"files": ["__a", "__b", "v__c"]}}}), "where")
        self.assertEqual({"s1": 15, "s2": 47},
                         dict(parsed.length_by_snapshot_name))

    def test_a_file_entry_without_a_usable_length_is_refused(self):
        # Abuse case: Elasticsearch's own FileInfo parser refuses an entry
        # with no length or a negative one. This reader used to count such
        # an entry as 0 bytes, so a document it had misread still produced
        # a byte total, and that total went into the size check as if
        # Elasticsearch had written it. Neutered under
        # "a-file-entry-needs-a-length".
        for length in (None, -1, True, "42", 4.5):
            entry = {"name": "__a", "physical_name": "_0.cfs"}
            if length is not None:
                entry["length"] = length
            with self.subTest(length=length):
                with self.assertRaises(ShapeGateError):
                    parse_shard_snapshots(shard_blob({
                        "files": [entry, {"name": "v__b",
                                          "physical_name": "segments_3",
                                          "length": 1}],
                        "snapshots": {"s1": {"files": ["__a", "v__b"]}}}),
                        "where")

    def test_a_file_entry_without_a_physical_name_is_refused(self):
        # Abuse case: Elasticsearch refuses an entry with no physical name.
        # This reader used to read it as "", which can never be a commit or
        # stand for a segment, so a commit entry that lost the field was
        # indistinguishable from an ordinary file. Neutered under
        # "a-file-entry-needs-a-physical-name".
        with self.assertRaises(ShapeGateError):
            parse_shard_snapshots(shard_blob({
                "files": [{"name": "__a", "length": 1},
                          {"name": "v__b", "physical_name": "segments_3",
                           "length": 1}],
                "snapshots": {"s1": {"files": ["__a", "v__b"]}}}), "where")

    def test_a_renamed_files_array_raises_rather_than_yielding_nothing(self):
        # Abuse case with a measured price. Renaming one field in this
        # document once deleted 96.4% of a rig repository by bytes, because a
        # document that yielded no names read as "this shard references
        # nothing" instead of as a document nobody could parse.
        # With no snapshot entry either, nothing later in the parser can
        # refuse it, so only the missing-files check stands. Neutered under
        # "a-shard-document-without-a-files-array-is-refused".
        with self.assertRaises(ShapeGateError):
            parse_shard_snapshots(shard_blob({
                "fileList": [{"name": "__a", "physical_name": "_0.cfs",
                              "length": 1}],
                "snapshots": {}}), "where")

    def test_a_snapshot_naming_a_file_the_document_does_not_declare_raises(self):
        # Abuse case for a half-decoded document. The `files` array and the
        # per-snapshot lists are written from one state, so a disagreement
        # means one of them was decoded wrongly and there is no way to tell
        # which. Picking a half would attribute a file list nobody wrote.
        # The list carries its commit, so the missing-commit gate cannot be
        # what refuses it. Neutered under
        # "a-snapshot-may-name-only-declared-files".
        with self.assertRaises(ShapeGateError):
            parse_shard_snapshots(shard_blob({
                "files": [{"name": "__a", "physical_name": "_0.cfs",
                           "length": 1},
                          {"name": "v__b", "physical_name": "segments_3",
                           "length": 1}],
                "snapshots": {"s1": {"files": ["__a", "v__b", "__ghost"]}}}),
                "where")

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


class SnapshotDocuments(unittest.TestCase):
    """`snap-<uuid>.dat`: the declaration the extent check measures against."""

    def body(self, **changes):
        body = {"name": "s2", "uuid": "uuid-s2", "state": "SUCCESS",
                "indices": ["wide"], "total_shards": 2,
                "successful_shards": 2,
                "index_details": {"wide": {"shard_count": 2,
                                           "size_in_bytes": 210,
                                           "max_segments_per_shard": 2}}}
        body.update(changes)
        return body

    def parse(self, body):
        return parse_snapshot_document(
            fx.codec_wrap(json.dumps({"snapshot": body}).encode("utf-8"),
                          codec_name="snapshot"), "snap-uuid-s2.dat")

    def test_a_well_formed_document_reads_its_extent(self):
        # The use case every refusal below is measured against. A reader
        # that refused this shape would drop every shard of every live
        # snapshot and report an empty manifest as a careful one.
        extent = self.parse(self.body())
        self.assertEqual((2, 2), (extent.total_shards,
                                  extent.successful_shards))
        self.assertEqual((2, 210), (extent.by_index_name["wide"].shard_count,
                                    extent.by_index_name["wide"].size_in_bytes))

    def test_absent_index_details_declares_nothing_per_index(self):
        # Use case: a document with no index_details map is silent about
        # every index, which the extent check reads as undeclared and drops.
        # Refusing it outright instead would change which code an operator
        # sees for the same missing declaration.
        body = self.body()
        del body["index_details"]
        self.assertEqual({}, dict(self.parse(body).by_index_name))

    def test_index_details_that_is_not_an_object_is_refused(self):
        # Abuse case: a list where the map belongs used to read as absent.
        # Something wrote a field this reader does not understand under a
        # name it relies on, which is the rule repository_data applies to
        # its own fields. Neutered under "index-details-must-be-an-object".
        with self.assertRaises(ShapeGateError):
            self.parse(self.body(index_details=[]))

    def test_a_boolean_count_is_refused(self):
        # Abuse case: JSON true is an int to Python, so a count a decoder
        # turned into a boolean read as 1 and was compared as if declared.
        # Neutered under "a-declared-count-must-be-a-whole-number".
        for field in ("total_shards", "successful_shards"):
            with self.subTest(field=field):
                with self.assertRaises(ShapeGateError):
                    self.parse(self.body(**{field: True}))

    def test_an_index_listed_twice_is_refused(self):
        # Abuse case: a repeated index name is not something Elasticsearch
        # writes, and the extent check counted each repetition, which let a
        # duplicate cancel out an inflated total. Neutered under
        # "a-snapshot-lists-each-index-once".
        with self.assertRaises(ShapeGateError):
            self.parse(self.body(indices=["wide", "wide"]))

    def test_a_body_not_nested_under_snapshot_is_refused(self):
        # Abuse case: Elasticsearch nests the body under `snapshot`, and all
        # four captured documents do. Reading the top level as the body when
        # that key is missing guessed at a shape nobody has seen, and the
        # guess decided which extent a traversal was measured against.
        # Neutered under "a-snapshot-document-must-nest-its-body".
        with self.assertRaises(ShapeGateError):
            parse_snapshot_document(
                fx.codec_wrap(json.dumps(self.body()).encode("utf-8"),
                              codec_name="snapshot"), "snap-uuid-s2.dat")


class CodecFraming(unittest.TestCase):

    def test_a_wrapped_payload_survives_deflate_and_comes_back(self):
        # The use case for both spellings Elasticsearch uses. A reader that
        # handled only the uncompressed form would drop every shard in a
        # repository written with compression on, and report an empty manifest
        # while looking healthy.
        for deflate in (False, True):
            self.assertEqual(
                unwrap(fx.codec_wrap(b'{"files": [], "snapshots": {}}',
                                     deflate=deflate), "snapshots"),
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
            unwrap(bytes(blob), "snapshots")

    def test_framing_that_is_absent_or_truncated_is_refused(self):
        # Abuse case for the two shapes a partial download takes.
        with self.assertRaises(BlobFormatError):
            unwrap(b"\x00" * 40, "snapshots")
        full = fx.codec_wrap(b'{"files": [], "snapshots": {}}')
        with self.assertRaises(BlobFormatError):
            unwrap(full[:12], "snapshots")

    def test_an_over_long_vint_in_the_header_is_refused_as_a_blob_error(self):
        # Abuse case. Both Lucene readers share one vint decoder. If the shared
        # decoder raised the segments reader's error here, shard-document
        # callers that handle BlobFormatError would treat a corrupt shard
        # document as a corrupt commit point, and a mis-parsed document would
        # decide which blobs are condemned.
        blob = struct.pack(">I", 0x3FD76C17) + b"\x80" * 8 + b"\x00" * 32
        with self.assertRaisesRegex(BlobFormatError, "vint") as caught:
            unwrap(blob, "snapshots")
        self.assertIs(type(caught.exception), BlobFormatError)

    def test_a_blob_framed_for_another_codec_is_refused(self):
        # Abuse case: Elasticsearch names the format in the header,
        # `snapshots` for a shard document and `snapshot` for a snapshot
        # document. A reader that skipped the name decoded either as the
        # other, so a misdirected read became a document of the wrong kind.
        # Neutered under "a-blob-carries-the-codec-it-is-read-as".
        blob = fx.codec_wrap(b'{"files": [], "snapshots": {}}',
                             codec_name="snapshot")
        with self.assertRaises(BlobFormatError):
            unwrap(blob, "snapshots")

    def test_a_format_version_elasticsearch_never_wrote_is_refused(self):
        # Abuse case: ChecksumBlobStoreFormat writes version 1 and reads
        # nothing else. A later version may lay out its payload differently,
        # and reading it as version 1 would attribute what it misread.
        blob = fx.codec_wrap(b'{"files": [], "snapshots": {}}', version=2)
        with self.assertRaises(BlobFormatError):
            unwrap(blob, "snapshots")

    def test_a_zlib_wrapped_payload_is_refused(self):
        # Abuse case: Elasticsearch's DeflateCompressor writes raw DEFLATE
        # with no zlib header, and every captured compressed document reads
        # only that way. The zlib form was accepted because this project's
        # own fixtures wrote it, which is a reader agreeing with its tests
        # rather than with Elasticsearch.
        # Neutered under "only-raw-deflate-is-read".
        zlib_framed = b"DFL\x00" + zlib.compress(b'{"files": [], "snapshots": {}}')
        with self.assertRaises(BlobFormatError):
            unwrap(_framed(zlib_framed), "snapshots")

    def test_bytes_after_the_deflate_stream_are_refused(self):
        # Abuse case: a stream that ends before the payload does leaves bytes
        # nobody decoded. Reading the stream and ignoring the rest is how a
        # spliced or overwritten blob would pass as a document. Neutered
        # under "a-deflate-stream-ends-with-its-payload".
        compressor = zlib.compressobj(wbits=-15)
        raw = (compressor.compress(b'{"files": [], "snapshots": {}}')
               + compressor.flush())
        with self.assertRaises(BlobFormatError):
            unwrap(_framed(b"DFL\x00" + raw + b"junk"), "snapshots")

    def test_a_footer_naming_another_checksum_algorithm_is_refused(self):
        # Abuse case: Lucene writes checksum algorithm 0, a CRC32, and
        # refuses any other. The segments_N reader already refused another
        # id while this reader did not, so the same footer was accepted in
        # one place and refused in the other. Neutered under
        # "the-codec-footer-names-algorithm-zero".
        framed = fx.codec_wrap(b'{"files": [], "snapshots": {}}')
        body = framed[:-16] + struct.pack(">II", 0xC02893E8, 7)
        blob = body + struct.pack(">Q", zlib.crc32(body) & 0xFFFFFFFF)
        with self.assertRaises(BlobFormatError):
            unwrap(blob, "snapshots")


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

    def test_a_header_enabling_shared_string_values_is_refused(self):
        # Abuse case: Elasticsearch never turns on shared string values,
        # and all 34 captured documents leave the flag off. A header that
        # sets it describes a writer nobody tested against, and answering
        # value back references from a table this reader only guesses at
        # would rename values silently. Neutered under
        # "smile-shared-string-values-are-refused".
        with self.assertRaises(BlobFormatError):
            decode_smile(b":)\n\x07\xfa\xfb")

    def test_seven_bit_binary_is_refused(self):
        # Abuse case: Elasticsearch writes binary raw, and the 7-bit decoder
        # this replaced lost bits in its last group, so a writer uuid
        # written that way read back as a different one. A token this
        # reader cannot decode correctly is refused rather than misread.
        with self.assertRaises(BlobFormatError):
            decode_smile(b":)\n\x05\xe8\x81\x00\x00")

    def test_bytes_after_the_root_value_are_refused(self):
        # Abuse case: the JSON branch refused trailing bytes and the SMILE
        # branch returned after the first value, so `{}` followed by junk,
        # or by a second document, read as `{}`. A file list cut short and
        # padded would read as a smaller file list. Neutered under
        # "a-smile-document-ends-at-its-root-value".
        for tail in (b"junk", b"\xfa\xfb"):
            with self.subTest(tail=tail):
                with self.assertRaises(BlobFormatError):
                    decode_smile(b":)\n\x05\xfa\xfb" + tail)

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

    def test_an_index_with_no_shard_generations_is_refused(self):
        # Every catalog at or above the supported min_version writes
        # shard_generations for every index. An absent list used to read as
        # an index with no shards, so an upstream rename of the field left
        # every shard unread and the run reported nothing wrong. Neutered
        # under "an-absent-shard-generations-list-is-refused".
        with self.assertRaises(ShapeGateError) as caught:
            parse_repository_data(self._catalog(indices={
                "i": {"id": "iuuid-i", "snapshots": ["uuid-s2"]}}), 1)
        self.assertNotIsInstance(caught.exception, UnsupportedRepository)

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
