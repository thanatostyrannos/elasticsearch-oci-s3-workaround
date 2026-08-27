"""Overlapping the store reads, and proving the answer did not move.

Measured on the scale harness before this existed: concurrency never exceeded
one, and 100 percent of a run's wall clock was serial round trips. 894
generations at a 40 millisecond round trip took 163.5 seconds on the narrowest
repository that can exist, and 53,063 objects over a thousand shard
directories took 48,093 requests, which is 32 minutes.

Overlap is only allowed here because it cannot change what the run says. Every
read is submitted under its key and its outcome, bytes or exception, is
recorded against that key, so a thread decides when bytes arrive and nothing
about a thread decides which bytes belong to which key. These tests are the
part that has to keep being true: the determinism cases below are the reason
the speed cases are allowed to exist.
"""

import os
import random
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import genchain_fixtures as fx
from generation_chain import run_audit
from generation_chain.errors import SourceReadError
from generation_chain.reporting import coverage as coverage_report
from generation_chain.sources import prepared
from generation_chain.sources.local import LocalMirrorSource
from generation_chain.sources.overlap import DEFAULT_CONCURRENCY

HISTORY = [
    {"s1": {"a": {0: ["__a0"], 1: ["__a1"]}, "b": ["__b0"]}},
    {"s1": {"a": {0: ["__a0"], 1: ["__a1"]}, "b": ["__b0"]},
     "s2": {"a": {0: ["__a0", "__a2"], 1: ["__a1", "__a3"]}, "b": ["__b0", "__b1"]}},
    {"s1": {"a": {0: ["__a0"], 1: ["__a1"]}, "b": ["__b0"]},
     "s2": {"a": {0: ["__a0", "__a2"], 1: ["__a1", "__a3"]}, "b": ["__b0", "__b1"]},
     "s3": {"a": {0: ["__a0", "__a4"], 1: ["__a1", "__a5"]}, "b": ["__b0", "__b2"]}},
    {"s2": {"a": {0: ["__a0", "__a2"], 1: ["__a1", "__a3"]}, "b": ["__b0", "__b1"]},
     "s3": {"a": {0: ["__a0", "__a4"], 1: ["__a1", "__a5"]}, "b": ["__b0", "__b2"]}},
    {"s3": {"a": {0: ["__a0", "__a4"], 1: ["__a1", "__a5"]}, "b": ["__b0", "__b2"]}},
]


class _Counting:
    """Counts round trips and records the most that were ever in flight.

    `latency` stands in for the network. A local mirror answers in
    microseconds, so without it the overlap has nothing to hide and a run
    would look serial whether it was or not.
    """

    def __init__(self, root, latency=0.0, shuffle_seed=None, fail_fetch=()):
        self.inner = LocalMirrorSource(root)
        self.latency = latency
        self.shuffle_seed = shuffle_seed
        self.fail_fetch = set(fail_fetch)
        self.max_in_flight = 0
        self.fetches = 0
        self.exists_calls = 0
        self.fetched = []
        self._in_flight = 0
        self._lock = threading.Lock()

    def describe(self):
        return "counting store"

    def _enter(self):
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        if self.latency:
            time.sleep(self.latency)

    def _leave(self):
        with self._lock:
            self._in_flight -= 1

    def list_keys(self):
        keys = self.inner.list_keys()
        if self.shuffle_seed is not None:
            random.Random(self.shuffle_seed).shuffle(keys)
        return keys

    def fetch(self, key):
        self._enter()
        try:
            with self._lock:
                self.fetches += 1
                self.fetched.append(key)
            if key in self.fail_fetch:
                raise SourceReadError(f"cannot read {key}: injected failure")
            return self.inner.fetch(key)
        finally:
            self._leave()

    def exists(self, key):
        self._enter()
        try:
            with self._lock:
                self.exists_calls += 1
            return self.inner.exists(key)
        finally:
            self._leave()


class _Repository(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-readahead-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        fx.build_repository(self.root, HISTORY)


class ReadsOverlap(_Repository):

    def test_a_run_has_more_than_one_read_outstanding(self):
        # The defect. Every read waited for the one before it, so a repository
        # whose audit costs 48,093 round trips cost 32 minutes of doing
        # nothing at 40 milliseconds each. If this ever reads 1 again, the
        # overlap has been disconnected and the wall clock has quietly gone
        # back to linear in the object count.
        source = _Counting(self.root, latency=0.002)
        run_audit(source)
        self.assertGreater(source.max_in_flight, 1)

    def test_both_kinds_of_round_trip_overlap(self):
        # 58 percent of this package's traffic is HEAD, so overlapping only
        # the GETs would leave most of the wall clock exactly where it was.
        gets = _Counting(self.root, latency=0.002)
        run_audit(gets)
        self.assertGreater(gets.exists_calls, 0)
        self.assertGreater(gets.fetches, 0)
        serial = _Counting(self.root)
        run_audit(prepared(serial, concurrency=1))
        # Overlap must not become a way to make MORE requests. A read-ahead
        # that guessed would show up right here as extra round trips.
        overlapped = _Counting(self.root)
        run_audit(prepared(overlapped, concurrency=DEFAULT_CONCURRENCY))
        self.assertLessEqual(overlapped.fetches,
                             serial.fetches + DEFAULT_CONCURRENCY)
        self.assertLessEqual(overlapped.exists_calls, serial.exists_calls)

    def test_the_number_asked_for_is_the_number_the_store_sees(self):
        # One budget for every kind of read, not one each. An operator asking
        # a throttling store for eight requests at a time and getting eight
        # document reads plus eight existence checks plus whatever a later
        # caller added has not been given the setting they asked for, and the
        # store answers the difference with 429s.
        source = _Counting(self.root, latency=0.002)
        run_audit(prepared(source, concurrency=4))
        self.assertLessEqual(source.max_in_flight, 4)
        self.assertGreater(source.max_in_flight, 1)

    def test_concurrency_one_restores_the_fully_serial_run(self):
        # A store that throttles a burst answers it with 429s, and the backoff
        # then costs more than the overlap saved. An operator needs a way back
        # to the old behaviour that is not "check out an older version".
        source = _Counting(self.root, latency=0.001)
        run_audit(prepared(source, concurrency=1))
        self.assertEqual(source.max_in_flight, 1)


class TheAnswerDoesNotMove(_Repository):

    def manifest(self, source=None):
        result = run_audit(source or LocalMirrorSource(self.root))
        return (tuple((c.key, c.category, c.reason) for c in result.condemned),
                tuple(sorted((p.key, p.disposition)
                             for p in result.classification)))

    def test_ten_runs_of_one_repository_agree(self):
        # The retired implementation was verified deterministic across ten
        # runs, and that property is the licence this package has to run reads
        # in parallel at all. A manifest that varied between runs could not be
        # compared with the reachability sweeper's, and an operator could not
        # tell a real difference from a scheduling one.
        first = self.manifest(_Counting(self.root, latency=0.001))
        for _ in range(9):
            self.assertEqual(self.manifest(_Counting(self.root, latency=0.001)),
                             first)

    def test_twenty_shuffled_listing_orders_agree(self):
        # A store is allowed to return its listing in any order it likes, and
        # two stores do not agree on one. Combined with overlap there are two
        # independent sources of ordering here, and neither may reach the
        # output.
        first = self.manifest()
        for seed in range(20):
            self.assertEqual(
                self.manifest(_Counting(self.root, shuffle_seed=seed)), first)

    def test_the_coverage_record_is_the_same_document_every_time(self):
        # The manifest is not the only thing a later differential compares.
        # Coverage numbers, dropped shards and notes go into evidence/ and get
        # diffed months later, so a number that wandered with the thread
        # schedule would read as a change in the repository.
        source = _Counting(self.root, latency=0.001)
        first = coverage_report.as_document(run_audit(source), "local", "x")
        for _ in range(4):
            other = coverage_report.as_document(
                run_audit(_Counting(self.root, latency=0.001)), "local", "x")
            self.assertEqual(other, first)

    def test_a_read_that_fails_fails_in_the_same_place_it_used_to(self):
        # Abuse case. A failure carried across a thread boundary has to arrive
        # at the caller unchanged, or a read error becomes a traceback and the
        # derivation stops degrading locally. Compared against the serial run
        # rather than against a fixed expectation, so this keeps meaning
        # something as the derivation changes.
        broken = "indices/iuuid-a/0/index-sg-a-0-1"
        serial = run_audit(prepared(_Counting(self.root, fail_fetch=[broken]),
                                    concurrency=1))
        overlapped = run_audit(_Counting(self.root, fail_fetch=[broken]))
        self.assertEqual([c.key for c in overlapped.condemned],
                         [c.key for c in serial.condemned])
        self.assertEqual(overlapped.coverage.notes, serial.coverage.notes)

    def test_a_serial_run_and_an_overlapped_run_produce_one_manifest(self):
        # The whole claim in one assertion. Concurrency is a wall-clock change
        # and nothing else, and the way to say that is to run the same
        # repository both ways and compare.
        serial = run_audit(prepared(LocalMirrorSource(self.root),
                                    concurrency=1))
        overlapped = run_audit(prepared(LocalMirrorSource(self.root),
                                        concurrency=DEFAULT_CONCURRENCY))
        self.assertEqual(
            coverage_report.as_document(serial, "local", "x"),
            coverage_report.as_document(overlapped, "local", "x"))


class PrefetchIsAdvisory(_Repository):

    def test_a_prefetched_key_that_fails_raises_when_it_is_asked_for(self):
        # A prefetch must never swallow anything. If a warmed read failed and
        # the later fetch quietly succeeded on a retry, or quietly returned
        # nothing, the derivation would see a different world from the one the
        # store is in.
        source = prepared(_Counting(self.root, fail_fetch=["index-1"]))
        source.list_keys()
        source.prefetch(["index-0", "index-1", "index-2"])
        self.assertTrue(source.fetch("index-0"))
        with self.assertRaises(SourceReadError):
            source.fetch("index-1")

    def test_prefetching_a_key_nobody_asks_for_costs_at_most_the_window(self):
        # Read-ahead is bounded on purpose. An unbounded one would hold every
        # warmed body in memory at once on a repository this package already
        # measures at 1.9 KB resident per object.
        source = prepared(_Counting(self.root), concurrency=4)
        source.prefetch([f"index-{n}" for n in range(5)])
        self.assertEqual(source.warmed, 4)
        source.fetch("index-0")
        self.assertLessEqual(source.warmed, 4)


if __name__ == "__main__":
    unittest.main()
