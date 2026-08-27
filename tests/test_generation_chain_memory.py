"""Refusing a repository this host cannot hold, before reading any of it.

Measured on the scale harness: 1.9 KB resident per object, linear all the way
to 585,194 objects at 1.55 GB peak. A 2 GB host therefore dies somewhere near
750,000 objects, and it dies at the END of a run, after thirty minutes of
round trips, with an OOM kill and no manifest and nothing said about why.

The remedy here is not a smaller footprint, which would move the cliff without
removing it. It is to work out at the start whether the run fits, and to say
so in a sentence naming the object count, the estimate and the flag that
overrides it. An operator who reads that in the first thirty seconds can move
the job to a bigger host. An operator who reads an OOM kill at minute thirty
cannot tell it from a crash.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import genchain_fixtures as fx
from generation_chain import run_audit
from generation_chain.errors import RunRefused
from generation_chain.sources.budget import (RESIDENT_BYTES_PER_OBJECT,
                                             MemoryBudget, RepositoryTooLarge,
                                             available_bytes)
from generation_chain.sources.local import LocalMirrorSource

HISTORY = [
    {"s1": {"idx": ["__a", "__shared"]}},
    {"s1": {"idx": ["__a", "__shared"]}, "s2": {"idx": ["__b", "__shared"]}},
    {"s2": {"idx": ["__b", "__shared"]}},
]


class WhatFitsOnThisHost(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-budget-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        self.built = fx.build_repository(self.root, HISTORY)
        self.objects = len(LocalMirrorSource(self.root).list_keys())

    def budget(self, limit):
        return MemoryBudget(LocalMirrorSource(self.root), limit_bytes=limit)

    def test_a_repository_that_fits_is_listed_as_it_always_was(self):
        # The use case. The check has to be invisible on every repository
        # anybody actually audits, or it becomes a thing operators route
        # around with a flag and then it protects nobody.
        generous = self.objects * RESIDENT_BYTES_PER_OBJECT * 100
        self.assertEqual(self.budget(generous).list_keys(),
                         LocalMirrorSource(self.root).list_keys())

    def test_a_repository_that_does_not_fit_refuses_before_it_is_read(self):
        # The defect. Without this the run reads the whole repository and the
        # kernel ends it at the point of maximum sunk cost.
        with self.assertRaises(RepositoryTooLarge):
            self.budget(RESIDENT_BYTES_PER_OBJECT).list_keys()

    def test_the_refusal_names_the_numbers_an_operator_needs(self):
        # "Out of memory" sends someone to the wrong page. The count, the
        # estimate and the limit together say whether to move the job, raise
        # the limit or narrow the prefix, and which one.
        with self.assertRaises(RepositoryTooLarge) as caught:
            self.budget(RESIDENT_BYTES_PER_OBJECT).list_keys()
        message = str(caught.exception)
        self.assertIn(str(self.objects), message)
        self.assertIn("--memory-mb", message)

    def test_a_refusal_for_size_is_not_a_refusal_to_retry(self):
        # A scheduled job derives its behaviour from this. Retrying this
        # command on this host produces the same answer and burns the backoff
        # to reach it, so it is not transient. It is also not the same as an
        # unsupported format, and the exit codes keep those apart.
        with self.assertRaises(RepositoryTooLarge) as caught:
            self.budget(RESIDENT_BYTES_PER_OBJECT).list_keys()
        self.assertIsInstance(caught.exception, RunRefused)
        self.assertFalse(caught.exception.transient)
        self.assertTrue(caught.exception.needs_a_bigger_host)

    def test_a_host_that_will_not_say_how_much_it_has_is_not_second_guessed(self):
        # Abuse case. On anything that is not Linux, and inside some
        # containers, there is no number to read. Guessing one and refusing on
        # it would stop a run that would have completed, which is a worse
        # failure than the one this guard exists to prevent.
        self.assertEqual(MemoryBudget(LocalMirrorSource(self.root),
                                      limit_bytes=None).list_keys(),
                         LocalMirrorSource(self.root).list_keys())

    def test_the_run_carries_the_refusal_instead_of_dying_later(self):
        # End to end, because a guard that raises where nobody catches it is
        # a traceback rather than a refusal, and a traceback writes no
        # coverage record.
        result = run_audit(MemoryBudget(LocalMirrorSource(self.root),
                                        limit_bytes=RESIDENT_BYTES_PER_OBJECT))
        self.assertEqual(result.condemned, [])
        self.assertIn("--memory-mb", result.coverage.refused)
        self.assertFalse(result.coverage.refusal_is_transient)
        self.assertTrue(result.coverage.refusal_needs_a_bigger_host)


class WhatTheHostSays(unittest.TestCase):

    def test_the_number_read_from_the_host_is_a_size_or_nothing(self):
        # This is our reading of /proc, not Python's. A parse that returned a
        # small wrong number would refuse every run on the machine, and one
        # that returned a huge wrong number would restore the OOM kill.
        value = available_bytes()
        if value is not None:
            self.assertGreater(value, 1 << 20)

    def test_a_cgroup_limit_beats_a_generous_host(self):
        # The case that matters in production here. A container with a 2 GB
        # limit on a 128 GB node reads MemAvailable and sees 128 GB, then gets
        # killed at 2 GB. The limit the kernel will actually enforce is the
        # one to believe.
        from generation_chain.sources import budget
        self.assertEqual(budget._smallest([None, 8 << 30, 2 << 30]), 2 << 30)
        self.assertEqual(budget._smallest([None, None]), None)
        self.assertEqual(budget._smallest([4 << 30, None]), 4 << 30)


if __name__ == "__main__":
    unittest.main()
