"""One test per claim this package makes that nothing else checks.

Two reviewers independently found the same thing: guards with no test that
goes red when the guard is removed. A guard nothing checks is a comment, and
reading the code does not tell the difference. This project has shipped four
that were structurally incapable of firing, so the standard here is that every
entry below has been neutered and watched to fail. `tests/genchain_neuter.py`
reruns that proof.
"""

import io
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import genchain_fixtures as fx
import s3rig
from generation_chain import cli, run_audit
from generation_chain.errors import (BlobFormatError, ShapeGateError,
                                     SourceReadError, UnsupportedRepository)
from generation_chain.formats.codec import unwrap
from generation_chain.formats.repository_data import parse_repository_data
from generation_chain.sources.local import LocalMirrorSource
from generation_chain.sources.oci import OciNativeSource
from generation_chain.sources.s3 import S3CompatibleSource, S3Credentials
from generation_chain.supported import require_supported_format

HISTORY = [
    {"s1": {"idx": ["__a", "__shared"]}},
    {"s1": {"idx": ["__a", "__shared"]}, "s2": {"idx": ["__b", "__shared"]}},
    {"s2": {"idx": ["__b", "__shared"]}},
]


class _Counting:
    """Records which keys the derivation asked the store for."""

    def __init__(self, root):
        self.inner = LocalMirrorSource(root)
        self.fetched = []

    def describe(self):
        return "counting mirror"

    def list_keys(self):
        return self.inner.list_keys()

    def exists(self, key):
        return self.inner.exists(key)

    def fetch(self, key):
        self.fetched.append(key)
        return self.inner.fetch(key)


class Repository(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-guards-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        fx.build_repository(self.root, HISTORY)

    def edit(self, generation, change):
        path = os.path.join(self.root, f"index-{generation}")
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        change(document)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)

    def audit(self):
        return run_audit(LocalMirrorSource(self.root))


class SupportedFormat(Repository):

    def test_a_supported_catalog_passes_the_precondition(self):
        # The use case. A precondition that refused everything would be safe
        # and useless, and the tool would look broken against exactly the
        # repositories it is for.
        require_supported_format(
            {"min_version": "7.12.0", "index_metadata_identifiers": {},
             "snapshots": [{"name": "s", "index_metadata_lookup": {}}]}, 2)
        self.assertIsNone(self.audit().coverage.refused)

    def test_an_unsupported_catalog_is_refused_before_anything_is_derived(self):
        # Abuse case, and the reason the check is a precondition rather than a
        # missing field somewhere: half support is what let a reviewer grow
        # the manifest by a live key. Asserting only that the run refuses
        # would not catch a version that refused AFTER computing a live set
        # from a shape the guards cannot see into, so this asserts that no
        # shard document was ever read.
        self.edit(2, lambda d: d.pop("index_metadata_identifiers"))
        source = _Counting(self.root)
        result = run_audit(source)
        self.assertEqual(result.condemned, [])
        self.assertIsNotNone(result.coverage.refused)
        self.assertEqual([k for k in source.fetched if k.startswith("indices/")],
                         [])

    def test_a_catalog_that_contradicts_its_own_declaration_is_refused(self):
        # A catalog declaring a supported min_version always carries the
        # lookups. One that declares 7.12.0 and omits them is disagreeing with
        # itself, and the live set is built out of exactly those fields, so
        # believing the declaration over the document would leave the live set
        # short.
        with self.assertRaises(UnsupportedRepository):
            parse_repository_data(json.dumps({
                "min_version": "7.12.0", "uuid": "u",
                "index_metadata_identifiers": {},
                "snapshots": [{"name": "s", "uuid": "u2"}],
                "indices": {}}).encode(), 2)

    def test_a_catalog_below_the_floor_or_with_no_declaration_is_refused(self):
        # The precondition proper. `min_version` is RepositoryData's own
        # statement of the minimum Elasticsearch version able to read it, and
        # a catalog below the floor carries none of the fields the live-set
        # cross-check is built on. A missing or unreadable declaration is not
        # "probably fine": absence is not evidence here either.
        for declared in ("7.11.0", "6.8.23", None, "", "seven", 712):
            document = {"index_metadata_identifiers": {}, "snapshots": []}
            if declared is not None:
                document["min_version"] = declared
            with self.assertRaises(UnsupportedRepository, msg=repr(declared)):
                require_supported_format(document, 2)


class Anchoring(Repository):

    def test_a_co_tenants_generation_above_the_anchor_is_rejected(self):
        # A bucket shared with another repository can hold higher-numbered
        # generation blobs that say nothing about ours. Elasticsearch's rule is
        # to take the highest generation IN THIS REPOSITORY, so the anchor is
        # the highest one carrying our uuid, and the uuid comes from the
        # generation `index.latest` names.
        with open(os.path.join(self.root, "index-9"), "wb") as handle:
            handle.write(json.dumps({
                "min_version": "7.12.0", "uuid": "somebody-elses-repository",
                "snapshots": [], "indices": {},
                "index_metadata_identifiers": {}}).encode())
        coverage = self.audit().coverage
        self.assertEqual(coverage.current_generation, 2)
        self.assertNotIn(9, coverage.generations_usable)
        self.assertIn(9, coverage.generations_rejected)

    def test_an_anchor_with_no_repository_uuid_explains_nothing(self):
        # A generation with no uuid states no opinion about which repository
        # it belongs to. With the anchor stating none, no other generation
        # blob in the bucket can be tied to this repository, so the run has no
        # way to tell its own history from a co-tenant's.
        self.edit(2, lambda d: d.pop("uuid"))
        result = self.audit()
        self.assertEqual(result.condemned, [])
        self.assertIsNotNone(result.coverage.refused)

    def test_a_negative_generation_pointer_is_refused(self):
        # `index.latest` is a signed 64-bit big-endian value, so a byte-order
        # mistake or a truncated copy reads as an enormous negative number. It
        # must not become a key the run then fails to find and shrugs at.
        with open(os.path.join(self.root, "index.latest"), "wb") as handle:
            handle.write(struct.pack(">q", -1))
        self.assertIsNotNone(self.audit().coverage.refused)

    def test_a_non_string_repository_uuid_is_refused(self):
        # Something wrote a field this tool relies on under a shape it does
        # not understand. Reading on would compare that value against every
        # other generation's uuid and match none of them, which silently
        # empties the chain.
        with self.assertRaises(ShapeGateError):
            parse_repository_data(json.dumps({
                "min_version": "7.12.0", "uuid": 12345,
                "snapshots": [], "indices": {},
                "index_metadata_identifiers": {}}).encode(), 1)

    def test_a_catalog_with_no_indices_map_is_refused(self):
        # The indices map is one of the two halves that establish which
        # snapshots still use which index. A catalog without it cannot
        # establish a live set at all.
        with self.assertRaises(ShapeGateError):
            parse_repository_data(json.dumps({
                "min_version": "7.12.0", "uuid": "u", "snapshots": [],
                "index_metadata_identifiers": {}}).encode(), 1)

    def test_a_blob_with_no_codec_header_is_not_read_as_a_document(self):
        # The header is what says these bytes are a framed document at all.
        # Skipping straight to the payload would decode whatever happened to
        # be at that offset and hand back a file list nobody wrote.
        body = fx.codec_wrap(b'{"files": [], "snapshots": {}}')
        with self.assertRaises(BlobFormatError):
            unwrap(b"\x00\x00\x00\x00" + body[4:])


class ShardDrops(Repository):

    def current_document(self):
        return "indices/iuuid-idx/0/index-sg-idx-0-2"

    def test_a_current_document_that_will_not_read_drops_its_shard(self):
        # The live set for a shard comes from this one document. Reading a
        # failure as an empty live set would condemn every segment in the
        # shard, which is the largest single mistake available here and the
        # exact inversion of what this tool is for.
        fx.corrupt(self.root, self.current_document())
        result = self.audit()
        self.assertIn("indices/iuuid-idx/0", result.coverage.shards_dropped)
        for blob in ("__a", "__b", "__shared"):
            self.assertNotIn(f"indices/iuuid-idx/0/{blob}", result.keys)

    def test_a_shard_the_current_generation_names_no_generation_for_is_dropped(self):
        # A null shard generation is Elasticsearch saying it has no opinion
        # about that shard. No opinion is not an empty live set, and treating
        # it as one condemns everything in the directory.
        self.edit(2, lambda d: [entry.update(shard_generations=[None])
                                for entry in d["indices"].values()])
        result = self.audit()
        self.assertIn("indices/iuuid-idx/0", result.coverage.shards_dropped)
        self.assertNotIn("indices/iuuid-idx/0/__a", result.keys)

    def _forget_index(self, root, name, also_in_snapshot_document):
        """Take one index out of the current catalog, and optionally out of the
        snapshot document that declares it too."""
        path = os.path.join(root, "index-2")
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        for snapshot in document["snapshots"]:
            snapshot["index_metadata_lookup"].pop(f"iuuid-{name}", None)
        document["indices"].pop(name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        if not also_in_snapshot_document:
            return
        for uuid in ("uuid-s2",):
            key = os.path.join(root, f"snap-{uuid}.dat")
            body = unwrap(open(key, "rb").read())["snapshot"]
            body["indices"] = [i for i in body["indices"] if i != name]
            body["index_details"].pop(name, None)
            body["total_shards"] = len(body["index_details"])
            body["successful_shards"] = body["total_shards"]
            with open(key, "wb") as handle:
                handle.write(fx.codec_wrap(
                    json.dumps({"snapshot": body}).encode(),
                    codec_name="snapshot"))

    def _two_index_repository(self):
        root = os.path.join(self.dir, "second")
        fx.build_repository(root, [
            {"s1": {"idx": ["__a"], "other": ["__o"]}},
            {"s1": {"idx": ["__a"], "other": ["__o"]},
             "s2": {"idx": ["__b"], "other": ["__o"]}},
            {"s2": {"idx": ["__b"], "other": ["__o"]}}])
        return root

    def test_a_catalog_short_of_what_a_snapshot_declares_drops_its_shards(self):
        # `snap-<uuid>.dat` states which indices the snapshot holds, written at
        # snapshot time by a different part of Elasticsearch into a different
        # object. A catalog that names fewer is provably short, and a live set
        # built from a short catalog reads an index nobody traversed as having
        # nothing alive in it.
        #
        # The snapshot's shards contribute nothing and the rest of the run
        # continues. Refusing the whole run was the earlier answer and it was
        # heavier than the evidence justifies: the contradiction is about ONE
        # snapshot's extent, so it condemns that snapshot's shards to silence
        # and says nothing about a shard no live snapshot in the disagreement
        # touches.
        root = self._two_index_repository()
        self._forget_index(root, "other", also_in_snapshot_document=False)
        result = run_audit(LocalMirrorSource(root))
        self.assertIn("indices/iuuid-idx/0", result.coverage.shards_dropped)
        self.assertNotIn("indices/iuuid-other/0/__o", result.keys)

    def test_a_live_snapshot_s_own_document_means_the_directory_is_in_use(self):
        # The store's own contradiction, isolated: here the catalog and the
        # snapshot document AGREE that the index is gone, so the extent check
        # has nothing to object to. What remains is a `snap-<uuid>.dat` for a
        # LIVE snapshot sitting in that shard directory, which says the
        # directory is in use right now whatever the catalog says.
        root = self._two_index_repository()
        self._forget_index(root, "other", also_in_snapshot_document=True)
        result = run_audit(LocalMirrorSource(root))
        self.assertIn("indices/iuuid-other/0", result.coverage.shards_dropped)
        self.assertNotIn("indices/iuuid-other/0/__o", result.keys)

    def test_a_snapshot_still_in_the_current_catalog_is_never_an_operation(self):
        # A snapshot can leave one generation and be back in a later one only
        # if the chain was misread. Attributing a delete to a snapshot the
        # current catalog still holds would name that live snapshot's blobs.
        keys = set(self.audit().keys)
        self.assertNotIn("snap-uuid-s2.dat", keys)
        self.assertNotIn("meta-uuid-s2.dat", keys)


class TransportGuards(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-transport-guards-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        fx.build_repository(self.root, HISTORY)

    def source(self, rig, prefix=""):
        return S3CompatibleSource(
            endpoint=rig.endpoint, region=s3rig.TEST_REGION, bucket=rig.bucket,
            prefix=prefix,
            credentials=S3Credentials(s3rig.TEST_ACCESS_KEY,
                                      s3rig.TEST_SECRET_KEY))

    def test_a_listing_that_claims_more_pages_and_names_none_is_refused(self):
        # A store that says IsTruncated and sends no continuation token has
        # ended the listing early. Accepting it returns a repository smaller
        # than it is, and every generation and blob beyond that point silently
        # does not exist as far as the run is concerned.
        with s3rig.S3Rig(root=self.root, page_size=3,
                         drop_token_after=1) as rig:
            with self.assertRaises(SourceReadError):
                self.source(rig).list_keys()

    def test_keys_outside_the_configured_prefix_are_not_this_repository(self):
        # One bucket holds several repositories. A run that read keys from
        # outside its base_path would build a chain out of two repositories'
        # generations and attribute one's deletes to the other's blobs.
        with s3rig.S3Rig(root=self.root, prefix="base/path",
                         objects={"elsewhere/index-0": b"{}"}) as rig:
            keys = self.source(rig, "base/path").list_keys()
        self.assertNotIn("elsewhere/index-0", keys)
        self.assertIn("index-0", keys)

    def test_a_listing_entry_with_no_name_is_refused(self):
        # Oracle's listing carries the object name in a field this tool reads
        # by name. An entry without one is a listing this tool cannot use, and
        # skipping it would quietly shorten the repository.
        source = OciNativeSource.__new__(OciNativeSource)
        source.prefix = ""
        from generation_chain.sources.http_reads import Response
        with self.assertRaises(SourceReadError):
            source._page(Response(200, {}, b'{"objects": [{"size": 1}]}'))

    def test_a_key_that_climbs_out_of_the_mirror_is_refused(self):
        # A key is data read out of a listing, so it gets treated as data. A
        # key holding ".." would read a document from outside the mirror, and
        # the derivation would then attribute another repository's catalog to
        # this one.
        with self.assertRaises(SourceReadError):
            LocalMirrorSource(self.root).fetch("../../etc/passwd")


class _Answers(io.StringIO):
    def __init__(self, text="", tty=True):
        super().__init__(text)
        self._tty = tty

    def isatty(self):
        return self._tty


class TransportPrompt(unittest.TestCase):

    def test_an_unanswered_choice_selects_nothing(self):
        # This project's standing rule is that an unanswered question is a
        # refusal rather than a pass. A prompt that let an empty line, an
        # end of file or a stray word fall through to the first option would
        # pick a transport for an operator who chose none, which is the exact
        # defect the prompt exists to prevent.
        for answer in ("", "\n", "yes\n", "0\n", "4\n", "1x\n", "11\n",
                       "s3\n", "local\n"):
            with self.assertRaises(cli.ChoiceAbandoned, msg=repr(answer)):
                cli.choose_transport(_Answers(answer), io.StringIO())

    def test_surrounding_whitespace_is_trimmed_and_nothing_else_is_read(self):
        # An operator who typed a space before the digit chose. Anything more
        # generous than trimming starts interpreting, and interpretation is
        # how a prompt ends up choosing for someone who did not.
        self.assertEqual(
            cli.choose_transport(_Answers("  2  \n"), io.StringIO()), "oci")

    def test_each_offered_number_selects_the_transport_it_names(self):
        # The use case, and it pins the ORDER as well: an operator reading
        # "1) S3" and getting the local path would not notice until the run
        # produced a manifest against the wrong store.
        for answer, expected in (("1\n", "s3"), ("2\n", "oci"), ("3\n", "local")):
            self.assertEqual(
                cli.choose_transport(_Answers(answer), io.StringIO()), expected)


if __name__ == "__main__":
    unittest.main()
