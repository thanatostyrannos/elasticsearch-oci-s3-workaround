"""What is still alive, and what a read is allowed to be.

Every counterexample a reviewer found against the first version of this
package was in one of these two places: a computation of "what the surviving
snapshots still reference" that treated a partial input as a complete one, or
an assumption that a read which returned bytes returned the RIGHT bytes.

An incomplete live set is not a degraded answer here. It is the one input that
makes this tool condemn live data, so incompleteness is a stop.
"""

import http.client
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import genchain_fixtures as fx
from generation_chain import cli, run_audit
from generation_chain.errors import SourceReadError
from generation_chain.sources import GuardedSource
from generation_chain.sources.http_reads import HttpReader
from generation_chain.sources.local import LocalMirrorSource

HISTORY = [
    {"s1": {"idx": ["__a", "__shared"], "other": ["__o"]}},
    {"s1": {"idx": ["__a", "__shared"], "other": ["__o"]},
     "s2": {"idx": ["__b", "__shared"], "other": ["__o"]}},
    {"s2": {"idx": ["__b", "__shared"], "other": ["__o"]}},
]
LIVE_METADATA = "indices/iuuid-other/meta-md-other.dat"


class _Listing:
    def __init__(self, root, extra=()):
        self.inner = LocalMirrorSource(root)
        self.extra = list(extra)

    def describe(self):
        return "listing wrapper"

    def list_keys(self):
        return self.inner.list_keys() + self.extra

    def exists(self, key):
        return self.inner.exists(key)

    def fetch(self, key):
        return self.inner.fetch(key)


class LiveSetCompleteness(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-live-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        fx.build_repository(self.root, HISTORY)
        self.baseline = self.keys()

    def keys(self, source=None):
        return set(run_audit(source or LocalMirrorSource(self.root)).keys)

    def edit_current(self, change):
        path = os.path.join(self.root, "index-2")
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        change(document)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)

    def test_the_live_snapshot_keeps_its_index_metadata(self):
        # The use case the two abuse cases below break. Both indices are still
        # referenced by the surviving snapshot, so neither metadata blob may
        # be named.
        self.assertNotIn(LIVE_METADATA, self.baseline)

    def test_a_short_metadata_lookup_in_the_current_catalog_is_refused(self):
        # Abuse case. A lookup missing ONE index still parses, and the live
        # set built from it is short by exactly the metadata blob that index
        # is still using. This is the reviewer's counterexample: the manifest
        # grew by one key, and that key was live.
        self.edit_current(
            lambda d: d["snapshots"][0]["index_metadata_lookup"].pop("iuuid-other"))
        after = self.keys()
        self.assertNotIn(LIVE_METADATA, after)
        self.assertTrue(after.issubset(self.baseline))

    def test_a_lookup_value_of_the_wrong_type_is_refused_not_dropped(self):
        # Abuse case, same hole by a second route. A dict comprehension that
        # filters on isinstance is a refusal nobody wrote: the entry vanishes,
        # no note is recorded, and the live set is silently short.
        self.edit_current(
            lambda d: d["snapshots"][0]["index_metadata_lookup"].update(
                {"iuuid-other": 12345}))
        after = self.keys()
        self.assertNotIn(LIVE_METADATA, after)
        self.assertTrue(after.issubset(self.baseline))

    def test_a_part_suffixed_file_in_a_live_snapshot_keeps_its_object(self):
        # Abuse case. A file longer than the part size has no object under its
        # bare name, only `.part<K>` pieces. A live-set predicate that
        # rejected the dot made the live file invisible while the attachment
        # side still hung the part object off the condemned stem, so the
        # manifest named a live object.
        os.rename(os.path.join(self.root, "indices/iuuid-idx/0/__b"),
                  os.path.join(self.root, "indices/iuuid-idx/0/__a.part0"))
        for generation, snapshots in (
                (0, {"s1": ["__a", "__shared"]}),
                (1, {"s1": ["__a", "__shared"], "s2": ["__a.part0", "__shared"]}),
                (2, {"s2": ["__a.part0", "__shared"]})):
            names = sorted({f for files in snapshots.values() for f in files})
            document = {
                "files": [{"name": n, "physical_name": "_" + n[2:]} for n in names],
                "snapshots": {s: {"files": f} for s, f in snapshots.items()}}
            with open(os.path.join(
                    self.root,
                    f"indices/iuuid-idx/0/index-sg-idx-0-{generation}"), "wb") as fh:
                fh.write(fx.codec_wrap(json.dumps(document).encode(),
                                       deflate=(generation % 2 == 1)))
        self.assertNotIn("indices/iuuid-idx/0/__a.part0", self.keys())

    def test_a_listing_that_lags_the_store_cannot_add_a_key(self):
        # Abuse case. A listing is a picture of the store taken earlier, and
        # an entry for an object already deleted would otherwise go straight
        # into a manifest an operator acts on. The listing narrows the
        # candidates; the store settles them.
        os.unlink(os.path.join(self.root, "indices/iuuid-idx/0/__a"))
        after = self.keys(_Listing(self.root, ["indices/iuuid-idx/0/__a"]))
        self.assertNotIn("indices/iuuid-idx/0/__a", after)


class _Truncating:
    """A store that answers, and then stops mid-body."""

    def describe(self):
        return "truncating store"

    def list_keys(self):
        raise http.client.IncompleteRead(b"half", 400)

    def exists(self, key):
        return True

    def fetch(self, key):
        raise http.client.IncompleteRead(b"half", 400)


class SourceContract(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-contract-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        fx.build_repository(self.root, HISTORY)

    def test_a_truncated_body_is_a_read_error_not_a_traceback(self):
        # Abuse case for the three real shapes of a short read: a body shorter
        # than its Content-Length, a chunked response that stops early, and a
        # socket closed after the headers. `IncompleteRead` is an
        # `http.client.HTTPException` rather than an `OSError`, so a narrow
        # except left all three escaping the derivation as a traceback. A
        # stated invariant that is false is worse than one nobody stated.
        with self.assertRaises(SourceReadError):
            GuardedSource(_Truncating()).fetch("index-0")
        result = run_audit(_Truncating())
        self.assertEqual(result.condemned, [])
        self.assertIsNotNone(result.coverage.refused)

    def test_a_reader_turns_any_transport_surprise_into_a_read_error(self):
        # The same contract one layer down, where the store is a socket rather
        # than an object. Anything that is not bytes is not a read.
        def explode(request, timeout=None):
            raise http.client.IncompleteRead(b"half", 10)
        reader = HttpReader(opener=explode, sleep=lambda _s: None,
                            jitter=lambda: 0.0)
        with self.assertRaises(SourceReadError):
            reader.get("http://x/", {})

    def test_a_co_tenant_generation_blob_is_not_read_as_ours(self):
        # Abuse case for a shared bucket. Another tenant's generation blob
        # parses perfectly and describes a different repository, so believing
        # it would produce a delete history for snapshots that never existed
        # here. The uuid separates tenants; it is not proof of authorship, and
        # the report says so rather than overclaiming.
        path = os.path.join(self.root, "index-1")
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        document["uuid"] = "somebody-elses-repository"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        result = run_audit(LocalMirrorSource(self.root))
        self.assertIn(1, result.coverage.generations_rejected)
        self.assertNotIn(1, result.coverage.generations_usable)

    def test_a_key_holding_a_newline_never_reaches_the_manifest(self):
        # Abuse case for the output format. Snapshot names and object keys
        # come out of the repository, so a name holding a newline would append
        # a row to a tab separated manifest an operator is about to act on.
        # Escaping it would be worse: the manifest would then name a key that
        # does not match the key in the store.
        from generation_chain.model import Condemnation
        from generation_chain.reporting import manifest
        rows = [Condemnation(key="good", category="c", reason="r",
                             snapshot_uuid="u", snapshot_name="n",
                             from_generation=1, to_generation=2),
                Condemnation(key="bad\nindices/live/__x", category="c",
                             reason="r", snapshot_uuid="u", snapshot_name="n",
                             from_generation=1, to_generation=2)]
        stream = io.StringIO()
        self.assertEqual(manifest.write_manifest(rows, stream), 1)
        self.assertEqual(len(stream.getvalue().splitlines()), 2)
        self.assertEqual(manifest.excluded_keys(rows), ["bad\nindices/live/__x"])


if __name__ == "__main__":
    unittest.main()


SHARED = [
    {"sh-a": {"idx": ["__1", "__2", "__3", "__4", "__5", "__6"]},
     "sh-b": {"idx": ["__1", "__2", "__3", "__4", "__5", "__6",
                      "__7", "__8", "__9", "__10"]}},
    {"sh-b": {"idx": ["__1", "__2", "__3", "__4", "__5", "__6",
                      "__7", "__8", "__9", "__10"]}},
]
SHARED_OTHER_WAY = [
    {"sh-a": {"idx": ["__1", "__2", "__3", "__4", "__5", "__6"]},
     "sh-b": {"idx": ["__1", "__2", "__3", "__4", "__5", "__6",
                      "__7", "__8", "__9", "__10"]}},
    {"sh-a": {"idx": ["__1", "__2", "__3", "__4", "__5", "__6"]}},
]
REUSED_NAME = [
    {"keep": {"other": ["__k"]}, "s#first": {"idx": ["__old"]}},
    {"keep": {"other": ["__k"]}},
    {"keep": {"other": ["__k"]}, "s#second": {"idx": ["__new"]}},
]


class SharedSegments(unittest.TestCase):
    """Two snapshots of one shard, one file set inside the other.

    This is where a derivation that subtracted the live set wrongly, or not at
    all, names live data, and it is the shape a forcemerge produces every
    time. The subtraction in attribution and the take-back in classification
    are one guard between them, and with both gone every shared blob lands in
    the manifest.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-shared-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def keys_for(self, history):
        root = os.path.join(self.dir, str(len(os.listdir(self.dir))))
        fx.build_repository(root, history)
        return set(run_audit(LocalMirrorSource(root)).keys)

    def test_deleting_the_snapshot_whose_files_are_a_subset_names_nothing(self):
        # Every one of sh-a's six blobs is still named by sh-b, so the delete
        # of sh-a orphaned no segment at all. A manifest naming any of them is
        # the data-loss direction, and this is the state that produces it.
        keys = self.keys_for(SHARED)
        for number in range(1, 11):
            self.assertNotIn(f"indices/iuuid-idx/0/__{number}", keys)

    def test_deleting_the_larger_snapshot_names_exactly_its_extra_blobs(self):
        # The other half. The four blobs only sh-b referenced are genuinely
        # orphaned and have to appear, or the tool leaks silently and an
        # operator comparing manifests reads the gap as agreement.
        keys = self.keys_for(SHARED_OTHER_WAY)
        for number in (7, 8, 9, 10):
            self.assertIn(f"indices/iuuid-idx/0/__{number}", keys)
        for number in range(1, 7):
            self.assertNotIn(f"indices/iuuid-idx/0/__{number}", keys)


class TheDerivationContradictingItself(unittest.TestCase):
    """The tripwire behind the one subtraction, and what it is and is not.

    The dispositions and the manifest are two readings of ONE live set, so a
    key that is both condemned and found live cannot happen unless a refactor
    has broken the derivation. That makes this an invariant check rather than a
    second opinion, and it is worth having precisely because the retired design
    tried to make it a second opinion instead: it had two live-set protections
    that covered for each other so completely that removing either alone
    changed no test result, so neither could be pinned and both were vacuous.

    Here the contradiction is fed in directly, because with one subtraction
    there is no input that reaches it, and a run that meets it produces NO
    manifest rather than a filtered one.
    """

    def _decide_with_a_planted_contradiction(self, root):
        from generation_chain.derivation.chain import load_chain
        from generation_chain.derivation.classification import decide
        from generation_chain.derivation.garbage import condemn
        from generation_chain.derivation.keys import KeyIndex
        from generation_chain.derivation.shards import survey_shards
        from generation_chain.model import Condemnation

        source = LocalMirrorSource(root)
        keys = source.list_keys()
        chain = load_chain(source, keys)
        index = KeyIndex(keys, source)
        survey = survey_shards(source, chain, keys, index)
        notes = []
        condemned = condemn(chain, survey, index, notes)
        live_key = "indices/iuuid-idx/0/__b"
        condemned.append(Condemnation(
            key=live_key, category="segment blob", reason="planted",
            snapshot_uuid="u", snapshot_name="n", from_generation=1,
            to_generation=2))
        return live_key, decide(chain, survey, keys, condemned, notes)

    def test_a_planted_contradiction_refuses_instead_of_filtering(self):
        # It RAISES rather than returning the keys for a caller to check.
        # `run_audit` already turns RunRefused into a refusal for every other
        # stage, so no wiring statement is left for a refactor to delete. The
        # neuter sweep found the earlier shape unpinned, and it is the same
        # shape a reviewer deleted from the retired package with the suite
        # green, silently unwiring the Elasticsearch veto.
        from generation_chain.errors import RunRefused

        root = tempfile.mkdtemp(prefix="genchain-contradiction-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fx.build_repository(root, HISTORY)
        with self.assertRaises(RunRefused):
            self._decide_with_a_planted_contradiction(root)

    def test_a_healthy_run_reports_no_contradiction(self):
        # The abuse case. A tripwire that fires on healthy data would refuse
        # every run, and one that can never fire pins nothing.
        from generation_chain.derivation.chain import load_chain
        from generation_chain.derivation.classification import decide
        from generation_chain.derivation.garbage import condemn
        from generation_chain.derivation.keys import KeyIndex
        from generation_chain.derivation.shards import survey_shards

        root = tempfile.mkdtemp(prefix="genchain-no-contradiction-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fx.build_repository(root, HISTORY)
        source = LocalMirrorSource(root)
        keys = source.list_keys()
        chain = load_chain(source, keys)
        index = KeyIndex(keys, source)
        survey = survey_shards(source, chain, keys, index)
        notes = []
        verdict = decide(chain, survey, keys,
                         condemn(chain, survey, index, notes), notes)
        self.assertGreater(len(verdict.manifest), 0)


class ReusedSnapshotName(unittest.TestCase):

    def test_a_name_two_snapshots_have_carried_attributes_no_file_list(self):
        # Shard documents identify a snapshot by NAME and root catalogs by
        # uuid, so a name that has belonged to two snapshots cannot be joined
        # between the two views. Guessing would credit the live snapshot's
        # files to the dead one. The cost is a real orphan left unnamed, which
        # is the direction this project prefers.
        root = tempfile.mkdtemp(prefix="genchain-reused-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fx.build_repository(root, REUSED_NAME)
        keys = set(run_audit(LocalMirrorSource(root)).keys)
        self.assertNotIn("indices/iuuid-idx/0/__old", keys)
        # The documents named by uuid are unambiguous and still appear, so the
        # guard costs only what it has to.
        self.assertIn("snap-uuid-s#first.dat", keys)


class StaleListingEntries(unittest.TestCase):

    def test_a_root_document_the_store_no_longer_holds_is_not_named(self):
        # The same lag as the segment case, on the path that reaches the
        # listing through a membership test rather than through object
        # attachment. Both paths have to ask the store, or one of them puts a
        # key an earlier sweep already removed back into a manifest.
        root = tempfile.mkdtemp(prefix="genchain-stale-root-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fx.build_repository(root, HISTORY)
        os.unlink(os.path.join(root, "snap-uuid-s1.dat"))
        keys = set(run_audit(_Listing(root, ["snap-uuid-s1.dat"])).keys)
        self.assertNotIn("snap-uuid-s1.dat", keys)


class CoverageHeadline(unittest.TestCase):

    def test_the_headline_is_the_less_flattering_of_the_two_numbers(self):
        # A run can read every generation, find every delete, and still
        # attribute none of them, and a headline built on transitions alone
        # reads 100% on exactly that run. An operator scanning the first line
        # would take a run that explained nothing for a clean repository.
        root = tempfile.mkdtemp(prefix="genchain-headline-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fx.build_repository(root, REUSED_NAME)
        coverage = run_audit(LocalMirrorSource(root)).coverage
        self.assertEqual(coverage.transition_fraction, 1.0)
        self.assertEqual(coverage.operations_found, 1)
        self.assertEqual(coverage.operations_attributed, 0)
        self.assertEqual(coverage.explained_fraction, 0.0)


class PlantedForeignGeneration(unittest.TestCase):

    def test_a_stranger_s_blob_filling_a_hole_neither_counts_nor_contributes(self):
        # A shared bucket, a gap in this repository's chain, and another
        # repository's generation blob sitting at the number the gap left
        # open. It parses perfectly and describes a different history, so
        # believing it would report better coverage than the run earned and
        # name keys derived from somebody else's catalog.
        stranger = tempfile.mkdtemp(prefix="genchain-stranger-")
        self.addCleanup(shutil.rmtree, stranger, ignore_errors=True)
        fx.build_repository(stranger, [
            {"x1": {"idx": ["__strange1", "__strange2"]}},
            {"x1": {"idx": ["__strange1", "__strange2"]},
             "x2": {"idx": ["__strange3"]}},
            {"x2": {"idx": ["__strange3"]}}],
            repo_uuid="a-different-repository")

        root = tempfile.mkdtemp(prefix="genchain-hole-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fx.build_repository(root, HISTORY)
        os.unlink(os.path.join(root, "index-1"))
        with_hole = run_audit(LocalMirrorSource(root))

        shutil.copy(os.path.join(stranger, "index-1"),
                    os.path.join(root, "index-1"))
        planted = run_audit(LocalMirrorSource(root))

        self.assertEqual(planted.coverage.transitions_explained,
                         with_hole.coverage.transitions_explained)
        self.assertEqual(planted.coverage.generations_usable,
                         with_hole.coverage.generations_usable)
        self.assertTrue(set(planted.keys).issubset(set(with_hole.keys)))
        self.assertIn(1, planted.coverage.generations_rejected)
