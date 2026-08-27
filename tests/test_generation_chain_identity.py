"""Read identity: proving the bytes are the object that was asked for.

The live edge set for a shard is ONE OBJECT. `indices/X/N/index-<gen>`, at the
generation the catalog names, holds every snapshot's file list for that shard
already unioned, and blobs are shard-scoped so nothing outside that directory
can contribute an edge. It is complete by construction and needs no
reconstruction at all.

So there are exactly two ways to get a shard's live set wrong: the document
was not read, or something else was read in its place. The first is a failure
that announces itself. The second does not, and it is where every remaining
counterexample against this package has lived. Four checks stand there, and
this file exists because a reviewer showed all of them were passing only
because the others covered for them: each test below is built so that removing
ONE check turns it red.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import genchain_fixtures as fx
from generation_chain import run_audit
from generation_chain.formats.codec import unwrap
from generation_chain.sources.local import LocalMirrorSource

# Two indices, two shards on one of them, one snapshot deleted and leaking.
HISTORY = [
    {"s1": {"one": {0: ["__a0", "__sh0"], 1: ["__a1"]}, "two": ["__t"]}},
    {"s1": {"one": {0: ["__a0", "__sh0"], 1: ["__a1"]}, "two": ["__t"]},
     "s2": {"one": {0: ["__b0", "__sh0"], 1: ["__b1"]}, "two": ["__t"]}},
    {"s2": {"one": {0: ["__b0", "__sh0"], 1: ["__b1"]}, "two": ["__t"]}},
]
VICTIM = "indices/iuuid-one/0"
CURRENT = f"{VICTIM}/index-sg-one-0-2"
LIVE_IN_VICTIM = f"{VICTIM}/__sh0"


class _Swapping:
    """A store that succeeds and returns bytes of the caller's choosing."""

    def __init__(self, root, swap):
        self.inner = LocalMirrorSource(root)
        self.swap = dict(swap)

    def describe(self):
        return "a store answering one key with another object"

    def list_keys(self):
        return self.inner.list_keys()

    def exists(self, key):
        return self.inner.exists(key)

    def fetch(self, key):
        return self.swap.get(key) or self.inner.fetch(key)


class ReadIdentity(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-identity-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        fx.build_repository(self.root, HISTORY)
        self.baseline = set(run_audit(LocalMirrorSource(self.root)).keys)

    def audit(self, swap=None):
        source = _Swapping(self.root, swap) if swap else LocalMirrorSource(self.root)
        return run_audit(source)

    def forge(self, template, change):
        """Rewrite one real document, keeping everything the test is not aiming at."""
        with open(os.path.join(self.root, template), "rb") as handle:
            document = unwrap(handle.read())
        change(document)
        return fx.codec_wrap(json.dumps(document).encode())

    def test_the_healthy_repository_condemns_the_dead_and_keeps_the_live(self):
        # The baseline the four checks are measured against. If it were empty
        # every assertion below would pass while testing nothing.
        self.assertIn(f"{VICTIM}/__a0", self.baseline)
        self.assertNotIn(LIVE_IN_VICTIM, self.baseline)
        self.assertNotIn(f"{VICTIM}/__b0", self.baseline)

    def test_a_document_that_names_no_blob_is_refused_by_the_parser(self):
        # ISOLATES THE NO-BLOB-NAMES GATE. The document is this shard's own
        # current one with its file lists emptied, so it satisfies every other
        # check: containment holds vacuously, the snapshot names match, and
        # the writer uuids it no longer carries cannot conflict. Only the
        # parser gate stands here, and `s3_repo_sweeper.py` has carried it all
        # along: a shard snapshot always carries at least its Lucene commit,
        # so an empty list is a list that was not read.
        forged = self.forge(CURRENT, lambda d: (
            d.update(files=[], snapshots={n: {"files": []}
                                          for n in d["snapshots"]})))
        result = self.audit({CURRENT: forged})
        self.assertIn(VICTIM, result.coverage.shards_dropped)
        self.assertNotIn(LIVE_IN_VICTIM, result.keys)
        self.assertTrue(set(result.keys).issubset(self.baseline))

    def test_a_document_naming_only_inline_entries_is_unattributable(self):
        # ISOLATES ATTRIBUTABILITY. Elasticsearch really writes this shape,
        # for a shard whose whole commit fits inline, so it passes the parser:
        # it names a file, and that file is a Lucene commit. Its blob set is
        # empty, so containment is satisfied against every directory in the
        # store and cannot separate it from anything. Nothing ties it to this
        # shard, and a read that cannot be attributed is a read not done.
        forged = self.forge(CURRENT, lambda d: (
            d.update(files=[e for e in d["files"] if e["name"].startswith("v__")],
                     snapshots={n: {"files": [e["name"] for e in d["files"]
                                              if e["name"].startswith("v__")]}
                                for n in d["snapshots"]})))
        result = self.audit({CURRENT: forged})
        self.assertIn(VICTIM, result.coverage.shards_dropped)
        self.assertNotIn(LIVE_IN_VICTIM, result.keys)

    def test_a_neighbouring_shard_s_document_conflicts_on_writer_lineage(self):
        # ISOLATES THE WRITER-UUID CHECK. The forged document names blobs this
        # directory really holds, so containment passes and attributability
        # finds a witness unique to this directory. Its snapshot names match
        # the catalog. What gives it away is Lucene's IndexWriter identity,
        # measured on a two-shard index built for the purpose: disjoint
        # between two shards of one index, shared between two generations of
        # one shard. Without this check a live segment quietly replaces a dead
        # one and the manifest does not even change size.
        forged = self.forge(CURRENT, lambda d: [
            e.update(writer_uuid="writer-one-1") for e in d["files"]])
        result = self.audit({CURRENT: forged})
        self.assertIn(VICTIM, result.coverage.shards_dropped)
        self.assertNotIn(LIVE_IN_VICTIM, result.keys)

    def test_a_document_holding_a_blob_from_elsewhere_fails_containment(self):
        # ISOLATES CONTAINMENT. The document keeps this shard's own writer
        # uuids and its own snapshot names, and it still names a witness
        # unique to this directory, so the other three checks are satisfied.
        # It also names a blob that lives in a different shard, which is a
        # file list assembled from two places and not this shard's.
        def add_foreign(document):
            document["files"].append(
                {"name": "__b1", "physical_name": "_b1", "length": 42,
                 "writer_uuid": "writer-one-0", "checksum": "a",
                 "written_by": "9.11.1"})
            for entry in document["snapshots"].values():
                entry["files"].append("__b1")
        result = self.audit({CURRENT: self.forge(CURRENT, add_foreign)})
        self.assertIn(VICTIM, result.coverage.shards_dropped)
        self.assertNotIn(LIVE_IN_VICTIM, result.keys)

    def test_an_older_generation_s_document_fails_on_snapshot_names(self):
        # ISOLATES THE SNAPSHOT-NAME CHECK. This is the same shard's own
        # document from an earlier generation, so its writer lineage matches,
        # its blobs are all in this directory, and it names witnesses unique
        # to it. The three other checks all pass. What separates it is the set
        # of snapshots it carries, which belongs to a generation the catalog
        # has moved past.
        older = os.path.join(self.root, f"{VICTIM}/index-sg-one-0-1")
        with open(older, "rb") as handle:
            result = self.audit({CURRENT: handle.read()})
        self.assertIn(VICTIM, result.coverage.shards_dropped)
        self.assertNotIn(LIVE_IN_VICTIM, result.keys)

    def test_a_short_file_list_contradicts_the_size_the_snapshot_declares(self):
        # ISOLATES THE DECLARED-SIZE CHECK. The document is this shard's own,
        # with the live snapshot's file list shortened by one entry. Its
        # writer lineage matches, its blobs are its own, its snapshot names
        # match, and it names a unique witness. Every identity check passes,
        # because the bytes really are this shard's. What is wrong is that
        # they are INCOMPLETE, and the only thing that can see that is the
        # size the snapshot recorded for itself when it was taken.
        def shorten(document):
            entry = document["snapshots"]["s2"]
            entry["files"] = [f for f in entry["files"] if f != "__sh0"]
        result = self.audit({CURRENT: self.forge(CURRENT, shorten)})
        self.assertIn(VICTIM, result.coverage.shards_dropped)
        self.assertNotIn(LIVE_IN_VICTIM, result.keys)
        self.assertTrue(set(result.keys).issubset(self.baseline))

    def test_a_shard_missing_from_the_catalog_contradicts_the_shard_count(self):
        # The other half of the extent check. A `shard_generations` array one
        # entry short leaves a shard untraversed, and an untraversed shard has
        # an empty live set, which condemns everything in it. The snapshot
        # recorded how many shards it covered, so the gap is visible.
        path = os.path.join(self.root, "index-2")
        with open(path, encoding="utf-8") as handle:
            catalog = json.load(handle)
        catalog["indices"]["one"]["shard_generations"] = \
            catalog["indices"]["one"]["shard_generations"][:1]
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(catalog, handle)
        result = self.audit()
        self.assertNotIn("indices/iuuid-one/1/__b1", result.keys)
        self.assertTrue(set(result.keys).issubset(self.baseline))


if __name__ == "__main__":
    unittest.main()
