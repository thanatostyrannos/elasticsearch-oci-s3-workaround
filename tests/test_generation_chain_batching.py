"""Batching the derivation over shard directories, and what must not move.

`derivation/shards.py` used to read every shard document for the whole run
into one cache before condemning anything, so the largest repository this
tool could audit was set by the host's memory rather than by the repository.
It now reads era documents one group of shard directories at a time and
discards them before the next group starts. Segment condemnation is sound at
any group size because a segment edge is complete by construction inside one
shard directory (see `derivation/garbage.py`); these tests are the property
that licenses batching to exist at all, the same way
`tests/test_generation_chain_readahead.py` is the property that licenses
overlapping reads.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import genchain_fixtures as fx
import genchain_repo as repo
from generation_chain import run_audit
from generation_chain.derivation.chain import load_chain
from generation_chain.derivation.shards import (ShardDirectoryTooLarge,
                                                plan_shard_batches)
from generation_chain.reporting import coverage as coverage_report
from generation_chain.sources.budget import RESIDENT_BYTES_PER_OBJECT
from generation_chain.sources.local import LocalMirrorSource

# Three indices of two shards each, five generations. Every add generation
# gives every shard two more segments, and one snapshot is deleted per step
# after the live window fills, so every shard directory carries orphaned
# blobs a batched run has to find regardless of where a batch boundary falls.
HISTORY = [
    {"s1": {"a": {0: ["__a0"], 1: ["__a1"]},
           "b": {0: ["__b0"], 1: ["__b1"]},
           "c": {0: ["__c0"], 1: ["__c1"]}}},
    {"s1": {"a": {0: ["__a0"], 1: ["__a1"]},
           "b": {0: ["__b0"], 1: ["__b1"]},
           "c": {0: ["__c0"], 1: ["__c1"]}},
     "s2": {"a": {0: ["__a0", "__a2"], 1: ["__a1", "__a3"]},
           "b": {0: ["__b0", "__b2"], 1: ["__b1", "__b3"]},
           "c": {0: ["__c0", "__c2"], 1: ["__c1", "__c3"]}}},
    {"s2": {"a": {0: ["__a0", "__a2"], 1: ["__a1", "__a3"]},
           "b": {0: ["__b0", "__b2"], 1: ["__b1", "__b3"]},
           "c": {0: ["__c0", "__c2"], 1: ["__c1", "__c3"]}},
     "s3": {"a": {0: ["__a0", "__a2", "__a4"], 1: ["__a1", "__a3", "__a5"]},
           "b": {0: ["__b0", "__b2", "__b4"], 1: ["__b1", "__b3", "__b5"]},
           "c": {0: ["__c0", "__c2", "__c4"], 1: ["__c1", "__c3", "__c5"]}}},
    {"s3": {"a": {0: ["__a0", "__a2", "__a4"], 1: ["__a1", "__a3", "__a5"]},
           "b": {0: ["__b0", "__b2", "__b4"], 1: ["__b1", "__b3", "__b5"]},
           "c": {0: ["__c0", "__c2", "__c4"], 1: ["__c1", "__c3", "__c5"]}},
     "s4": {"a": {0: ["__a0", "__a2", "__a4", "__a6"],
                 1: ["__a1", "__a3", "__a5", "__a7"]},
           "b": {0: ["__b0", "__b2", "__b4", "__b6"],
                 1: ["__b1", "__b3", "__b5", "__b7"]},
           "c": {0: ["__c0", "__c2", "__c4", "__c6"],
                 1: ["__c1", "__c3", "__c5", "__c7"]}}},
    {"s4": {"a": {0: ["__a0", "__a2", "__a4", "__a6"],
                 1: ["__a1", "__a3", "__a5", "__a7"]},
           "b": {0: ["__b0", "__b2", "__b4", "__b6"],
                 1: ["__b1", "__b3", "__b5", "__b7"]},
           "c": {0: ["__c0", "__c2", "__c4", "__c6"],
                 1: ["__c1", "__c3", "__c5", "__c7"]}}},
]

SHARD_DIRECTORIES = 6  # three indices, two shards each


class _Repository(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-batching-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        fx.build_repository(self.root, HISTORY)
        self.source = LocalMirrorSource(self.root)

    def smallest_budget_that_does_not_refuse(self) -> int:
        """The tightest `--max-ram` this repository accepts.

        Doubling from 1 byte rather than asserting a byte figure keeps this
        test honest about what it is proving (batching works AT THE TIGHTEST
        budget the planner will run) without pinning
        `RESIDENT_BYTES_PER_OBJECT` or this fixture's exact blob count.
        """
        chain = load_chain(self.source, self.source.list_keys())
        budget = RESIDENT_BYTES_PER_OBJECT
        while True:
            try:
                plan_shard_batches(chain, self.source.list_keys(), budget)
            except ShardDirectoryTooLarge:
                budget *= 2
                continue
            return budget


class BatchSizeNeverMovesTheAnswer(_Repository):

    def test_the_tightest_budget_really_is_one_directory_per_batch(self):
        # Guards the test fixture itself. If this fixture ever stopped being
        # uniform enough for the tightest budget to force one directory per
        # batch, every test below would still pass while silently testing a
        # coarser batch size than it claims to.
        chain = load_chain(self.source, self.source.list_keys())
        budget = self.smallest_budget_that_does_not_refuse()
        groups = plan_shard_batches(chain, self.source.list_keys(), budget)
        self.assertEqual(SHARD_DIRECTORIES, sum(len(g) for g in groups))
        self.assertTrue(all(len(g) == 1 for g in groups),
                        f"batch sizes were {[len(g) for g in groups]}, not "
                        "all singletons")

    def manifest(self, budget_bytes):
        result = run_audit(LocalMirrorSource(self.root),
                           budget_bytes=budget_bytes)
        return (tuple((c.key, c.category, c.reason) for c in result.condemned),
                tuple(sorted((p.key, p.disposition)
                             for p in result.classification)))

    def test_no_budget_and_a_huge_budget_agree(self):
        # The two shapes an operator never has to think about: no ceiling at
        # all, and a ceiling so generous it could never bind. Both mean "one
        # group", and they had better mean the same one group.
        self.assertEqual(self.manifest(None), self.manifest(10 ** 15))

    def test_the_tightest_budget_that_runs_agrees_with_no_budget(self):
        # The batch size of one this repository can produce without refusing.
        # If grouping shard directories ever let one batch see a blob or a
        # snapshot name another batch could not, this is where it would show:
        # the smallest group size disagrees with the largest.
        tightest = self.smallest_budget_that_does_not_refuse()
        self.assertEqual(self.manifest(tightest), self.manifest(None))

    def test_a_spread_of_batch_sizes_all_agree(self):
        tightest = self.smallest_budget_that_does_not_refuse()
        reference = self.manifest(None)
        for budget in (tightest, tightest * 2, tightest * 3, tightest * 5,
                      tightest * 1000):
            self.assertEqual(self.manifest(budget), reference,
                             f"budget_bytes={budget} disagreed with no budget")

    def test_the_coverage_record_agrees_across_batch_sizes(self):
        # The manifest is not the only thing evidence/ diffs later. Coverage
        # counts, dropped shards and notes have to be the same document
        # whatever the batch size, the same claim the concurrency work
        # already established for thread scheduling.
        tightest = self.smallest_budget_that_does_not_refuse()
        reference = coverage_report.as_document(
            run_audit(LocalMirrorSource(self.root)), "local", "x")
        for budget in (tightest, tightest * 4, None):
            got = coverage_report.as_document(
                run_audit(LocalMirrorSource(self.root), budget_bytes=budget),
                "local", "x")
            self.assertEqual(got, reference)


class BatchingNeverNamesMore(_Repository):

    def test_the_tightest_batching_names_no_key_the_unbatched_run_missed(self):
        # The weaker half of the determinism property, checked on its own
        # because it is the direction that matters most: an operator would
        # rather a batched run miss a key than invent one. Byte-for-byte
        # equality above already proves this; this test is what stays true
        # even if equality ever loosens to "batching may condemn less".
        tightest = self.smallest_budget_that_does_not_refuse()
        batched = {c.key for c in
                  run_audit(LocalMirrorSource(self.root),
                           budget_bytes=tightest).condemned}
        whole = {c.key for c in
                run_audit(LocalMirrorSource(self.root)).condemned}
        self.assertTrue(batched.issubset(whole))
        self.assertEqual(batched, whole)


class ADeclaredExtentSpanningBatchesIsStillChecked(_Repository):
    """The guard batching depends on but does not itself add.

    Batching moved `_check_declared_extent` to run before any batch is read,
    against every shard directory's CURRENT document, precisely so a
    snapshot's declared shard count is judged against every shard it touches
    rather than against whichever batch happened to read it. Every index in
    this fixture has two shards, so at a batch size of one, a snapshot's
    declared extent for index "a" spans two different batches; if the extent
    check only saw one batch's shards, index "a" would look one shard short
    in every batch and get dropped everywhere, which the determinism tests
    above would already have caught. This test names the mechanism directly
    rather than relying on that indirection.
    """

    def test_no_index_is_dropped_for_a_short_count_at_the_tightest_batch_size(self):
        from generation_chain.derivation.shards import EXTENT_SHARD_COUNT

        tightest = self.smallest_budget_that_does_not_refuse()
        result = run_audit(LocalMirrorSource(self.root),
                           budget_bytes=tightest)
        codes = " ".join(str(doubt) for doubt
                         in result.coverage.shards_dropped.values())
        self.assertNotIn(EXTENT_SHARD_COUNT, codes)
        self.assertEqual({}, result.coverage.shards_dropped)


class TheBudgetIsAThrottleNotOnlyAGate(_Repository):
    """`--max-ram` used to refuse the whole repository; it now sizes batches.

    A budget too small for the WHOLE repository at once, but big enough for
    one shard directory, must still complete. That is the difference between
    a gate and a throttle: the old `MemoryBudget` wrapper (`sources/budget.py`,
    still there and still correct for what it measures) would have refused
    this exact repository at this exact number, because it estimates against
    the object count of the whole listing.
    """

    def test_a_budget_too_small_for_the_whole_repository_still_completes(self):
        tightest = self.smallest_budget_that_does_not_refuse()
        result = run_audit(LocalMirrorSource(self.root),
                           budget_bytes=tightest)
        self.assertIsNone(result.coverage.refused)
        self.assertGreater(len(result.condemned), 0)

    def test_the_old_whole_repository_gate_would_have_refused_this_budget(self):
        # Names the contrast directly, so a reader does not have to take the
        # docstring's word for it: the same number that lets a batched run
        # complete is one `MemoryBudget` (unbatched) refuses outright.
        from generation_chain.sources.budget import (MemoryBudget,
                                                     RepositoryTooLarge)

        tightest = self.smallest_budget_that_does_not_refuse()
        with self.assertRaises(RepositoryTooLarge):
            MemoryBudget(LocalMirrorSource(self.root),
                        limit_bytes=tightest).list_keys()


class ASingleShardDirectoryTooLargeStillRefuses(_Repository):
    """The one case batching does not paper over: it does not fit even alone.

    Sizing a group to the budget only helps when some group smaller than the
    whole repository fits. A directory that alone exceeds the budget has
    nowhere smaller to go, and reading it anyway is exactly the OOM-at-the-end
    failure this whole change exists to turn into a refusal at the door.
    """

    def test_a_budget_smaller_than_one_directory_refuses_before_reading(self):
        from generation_chain.derivation.shards import ShardDirectoryTooLarge

        chain = load_chain(self.source, self.source.list_keys())
        with self.assertRaises(ShardDirectoryTooLarge):
            plan_shard_batches(chain, self.source.list_keys(), 1)

    def test_the_refusal_names_the_directory_and_both_numbers(self):
        from generation_chain.derivation.shards import ShardDirectoryTooLarge

        chain = load_chain(self.source, self.source.list_keys())
        with self.assertRaises(ShardDirectoryTooLarge) as caught:
            plan_shard_batches(chain, self.source.list_keys(), 1)
        message = str(caught.exception)
        self.assertIn("indices/", message)
        self.assertIn("MB", message)
        self.assertIn("--max-ram", message)

    def test_the_run_carries_the_refusal_and_needs_a_bigger_host(self):
        result = run_audit(LocalMirrorSource(self.root), budget_bytes=1)
        self.assertEqual(result.condemned, [])
        self.assertIsNotNone(result.coverage.refused)
        self.assertFalse(result.coverage.refusal_is_transient)
        self.assertTrue(result.coverage.refusal_needs_a_bigger_host)

    def test_a_budget_that_fits_only_the_largest_directory_still_refuses_nothing(self):
        # The boundary the refusal has to respect in the other direction. A
        # budget just barely large enough for the single biggest directory
        # must not refuse, or the ceiling is stricter than the design calls
        # for: refuse only when a directory truly does not fit, not when it
        # merely uses the whole budget.
        chain = load_chain(self.source, self.source.list_keys())
        budget = self.smallest_budget_that_does_not_refuse()
        groups = plan_shard_batches(chain, self.source.list_keys(), budget)
        self.assertGreater(len(groups), 0)


class AWriterUuidCollisionSpanningTwoBatchesIsStillCaught(unittest.TestCase):
    """The one check batching could have quietly narrowed, and did not.

    `identity.check_directory` is necessary and not sufficient against a
    fetch that returns another directory's document: a real document from a
    different shard whose blob set happens to be contained in the victim
    directory's still passes it (docs/repository-layout-and-reachability.md).
    The writer-uuid check is what stands against exactly that case, and it
    only ever narrows the manifest by dropping a directory, never widens it,
    so a batched run has to keep it at full strength: comparing every
    surviving directory's writer uuids against every other one, not only
    the directories that happened to land in the same batch.
    """

    HISTORY = [
        {"s1": {"a": {0: ["__a1"]}, "b": {0: ["__b1"]}}},
        {"s1": {"a": {0: ["__a1"]}, "b": {0: ["__b1"]}},
         "s2": {"a": {0: ["__a2"]}, "b": {0: ["__b2"]}}},
        {"s2": {"a": {0: ["__a2"]}, "b": {0: ["__b2"]}}},
    ]

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-writer-collision-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        # Both shards claim the SAME Lucene writer identity, which real
        # Elasticsearch never produces: two shards of one index share none,
        # and two generations of one shard share every value (measured on
        # the rig, see identity.writer_uuid_collisions). This is what a
        # fetch returning the wrong directory's document looks like from
        # the outside.
        repo.build(self.root, self.HISTORY, defects=repo.Defects(
            writer_uuid_of={("a", 0): "shared-writer",
                           ("b", 0): "shared-writer"}))
        self.a = repo.directory_of("a", 0)
        self.b = repo.directory_of("b", 0)

    def tightest_budget(self):
        source = LocalMirrorSource(self.root)
        chain = load_chain(source, source.list_keys())
        budget = RESIDENT_BYTES_PER_OBJECT
        while True:
            try:
                groups = plan_shard_batches(chain, source.list_keys(), budget)
            except ShardDirectoryTooLarge:
                budget *= 2
                continue
            self.assertTrue(all(len(g) == 1 for g in groups),
                            f"batch sizes were {[len(g) for g in groups]}, "
                            "not all singletons, so this budget would not "
                            "put the two colliding directories in different "
                            "batches and would not exercise the guard")
            return budget

    def test_both_directories_are_dropped_at_the_tightest_batch_size(self):
        # The use case. Neither directory is trustworthy once the other one
        # is known to claim the same writer identity, and there is no way
        # to tell which of the two reads was wrong, so naming only one would
        # be a guess.
        budget = self.tightest_budget()
        result = run_audit(LocalMirrorSource(self.root), budget_bytes=budget)
        self.assertIn(self.a, result.coverage.shards_dropped)
        self.assertIn(self.b, result.coverage.shards_dropped)
        for doubt in (result.coverage.shards_dropped[self.a],
                     result.coverage.shards_dropped[self.b]):
            self.assertIn("writer", str(doubt).lower())

    def test_no_segment_from_either_directory_is_condemned(self):
        # The retraction this guard depends on: `__a1` and `__b1` are
        # genuinely orphaned by s1's deletion and would be condemned if the
        # collision were not caught, because segment condemnation for a
        # batch runs before the whole run's writer uuids are known. A test
        # that only checked `shards_dropped` could still pass while a stale
        # condemnation from before the collision was found sat in the
        # manifest.
        budget = self.tightest_budget()
        result = run_audit(LocalMirrorSource(self.root), budget_bytes=budget)
        keys = {c.key for c in result.condemned}
        self.assertNotIn(f"{self.a}/__a1", keys)
        self.assertNotIn(f"{self.b}/__b1", keys)

    def test_the_same_repository_without_the_shared_writer_condemns_normally(self):
        # The abuse case. A guard that fires on healthy data would drop
        # every shard of every repository at a small enough batch size, and
        # this repository would otherwise condemn exactly __a1 and __b1.
        clean_root = os.path.join(self.dir, "clean-repo")
        repo.build(clean_root, self.HISTORY)
        result = run_audit(LocalMirrorSource(clean_root))
        keys = {c.key for c in result.condemned}
        self.assertIn(f"{self.a}/__a1", keys)
        self.assertIn(f"{self.b}/__b1", keys)
        self.assertEqual({}, result.coverage.shards_dropped)


if __name__ == "__main__":
    unittest.main()
