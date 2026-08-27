"""How much space the orphans occupy, and why that number is the listing's.

An operator's first question about a leaking repository is what it is costing,
and the audit could not answer it. The number has to come from the store's own
listing: the `length` recorded in shard metadata is per logical Lucene file and
is summed per snapshot, so it double counts every file two snapshots share,
which is exactly the deduplication that makes snapshots incremental. Measured on
the captured 9.5.2 repository: 143,543 stored bytes against 571,852 summed
declared lengths, a four-fold overstatement.

So: stored object size, from the listing already being fetched, or nothing.
"""

import io
import unittest

from generation_chain.reporting.coverage import human_bytes


class TestHumanBytes(unittest.TestCase):
    def test_picks_the_unit_that_makes_the_number_readable(self):
        # An operator reads this to decide whether the leak matters. Bytes at
        # petabyte scale is a number nobody can size at a glance.
        self.assertEqual(human_bytes(0), "0 B")
        self.assertEqual(human_bytes(512), "512 B")
        self.assertEqual(human_bytes(1_500), "1.5 KB")
        self.assertEqual(human_bytes(2_400_000), "2.4 MB")
        self.assertEqual(human_bytes(7_800_000_000), "7.8 GB")
        self.assertEqual(human_bytes(1_540_000_000_000), "1.54 TB")
        self.assertEqual(human_bytes(3_200_000_000_000_000), "3.2 PB")

    def test_uses_decimal_units_because_the_store_console_does(self):
        # Object stores bill and display in powers of ten. Reporting 1.4 TiB
        # next to a console showing 1.5 TB invites the operator to conclude one
        # of the two is wrong.
        self.assertEqual(human_bytes(1_000_000_000), "1.0 GB")
        self.assertEqual(human_bytes(1_073_741_824), "1.07 GB")

    def test_refuses_a_negative_size_rather_than_formatting_it(self):
        # A negative total means a summing bug upstream. Printing "-4.0 GB"
        # would look like a real measurement of something.
        with self.assertRaises(ValueError):
            human_bytes(-1)


class TestReportNamesTheReclaimableTotal(unittest.TestCase):
    """The report has to say the size, and say when it is only a floor."""

    def test_sums_only_the_condemned_keys(self):
        from generation_chain.reporting.coverage import reclaimable
        sizes = {"a": 100, "b": 200, "c": 4_000}
        total, unsized = reclaimable(["a", "c"], sizes)
        self.assertEqual(total, 4_100)
        self.assertEqual(unsized, 0)

    def test_counts_what_it_could_not_size_instead_of_guessing(self):
        # A key the listing gave no size for is not zero bytes. Treating it as
        # zero produces a total that is quietly too small and looks exact.
        from generation_chain.reporting.coverage import reclaimable
        total, unsized = reclaimable(["a", "b", "c"], {"a": 100})
        self.assertEqual(total, 100)
        self.assertEqual(unsized, 2)

    def test_no_sizes_at_all_reports_nothing_rather_than_zero(self):
        from generation_chain.reporting.coverage import reclaimable
        total, unsized = reclaimable(["a", "b"], {})
        self.assertEqual((total, unsized), (0, 2))


if __name__ == "__main__":
    unittest.main()


class TestSourcesReportStoredSize(unittest.TestCase):
    """Sizes come from the listing already being fetched, or not at all."""

    def test_the_local_mirror_reports_what_is_on_disk(self):
        import os, tempfile
        from generation_chain.sources.local import LocalMirrorSource
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "indices", "u", "0"))
            with open(os.path.join(tmp, "index-0"), "wb") as h:
                h.write(b"x" * 40)
            with open(os.path.join(tmp, "indices", "u", "0", "__a"), "wb") as h:
                h.write(b"y" * 900)
            sizes = LocalMirrorSource(tmp).sizes()
        self.assertEqual(sizes["index-0"], 40)
        self.assertEqual(sizes["indices/u/0/__a"], 900)

    def test_s3_takes_them_from_the_listing_it_already_asked_for(self):
        # The size is a sibling of the key in every ListObjectsV2 entry, so
        # this costs no extra request. A HEAD per object would cost one per
        # key, which at 76,656 keys is the shape of the bug being fixed.
        import os, sys
        sys.path.insert(0, os.path.dirname(__file__))
        import s3rig
        from generation_chain.sources.s3 import S3CompatibleSource, S3Credentials
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "index-0"), "wb") as h:
                h.write(b"z" * 1234)
            with s3rig.S3Rig(root=root) as rig:
                source = S3CompatibleSource(
                    endpoint=rig.endpoint, region=s3rig.TEST_REGION,
                    bucket=rig.bucket, prefix="",
                    credentials=S3Credentials(s3rig.TEST_ACCESS_KEY,
                                              s3rig.TEST_SECRET_KEY))
                source.list_keys()
                self.assertEqual(source.sizes()["index-0"], 1234)


class TestTheReportSaysTheSize(unittest.TestCase):
    def _report(self, condemned_keys, sizes):
        from generation_chain.model import AuditResult, Coverage, Condemnation
        from generation_chain.reporting import coverage as cov
        result = AuditResult(
            condemned=[Condemnation(key=k, category="segment", reason="r",
                                    snapshot_uuid="s", snapshot_name="n",
                                    from_generation=1, to_generation=2)
                       for k in condemned_keys],
            coverage=Coverage(),
        )
        out = io.StringIO()
        cov.write_report(result, "local", "/tmp/x", out, sizes=sizes)
        return out.getvalue()

    def test_names_the_total_in_readable_units(self):
        text = self._report(["a", "b"], {"a": 4_000_000_000, "b": 800_000_000})
        self.assertIn("4.8 GB", text)

    def test_says_it_is_a_floor_when_something_could_not_be_sized(self):
        # Silence here would present a total that is too small as if it were
        # exact, and this is the number an operator quotes upward.
        text = self._report(["a", "b"], {"a": 4_000_000_000})
        self.assertIn("4.0 GB", text)
        self.assertIn("floor", text.lower())

    def test_a_source_that_cannot_size_anything_says_so_plainly(self):
        text = self._report(["a", "b"], {})
        self.assertNotIn("0 B", text)
        self.assertIn("no sizes", text.lower())


class TestWrappersDoNotSwallowTheSizes(unittest.TestCase):
    """The CLI wraps the transport, and a wrapper that drops a method turns
    the feature off without failing anything. Found exactly that way: the
    report said "no sizes available" for a local mirror that could size every
    key, because the budget wrapper had no sizes() to delegate."""

    def _wrapped(self):
        import os, tempfile
        from generation_chain.sources.local import LocalMirrorSource
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "index-0"), "wb") as h:
            h.write(b"q" * 77)
        return LocalMirrorSource(tmp)

    def test_the_budget_wrapper_passes_them_through(self):
        from generation_chain.sources.budget import MemoryBudget
        inner = self._wrapped()
        wrapped = MemoryBudget(inner, limit_bytes=1 << 30)
        wrapped.list_keys()
        self.assertEqual(wrapped.sizes()["index-0"], 77)

    def test_the_readahead_wrapper_passes_them_through(self):
        from generation_chain.sources.readahead import CriticalReads, ReadAhead
        for cls in (CriticalReads, ReadAhead):
            inner = self._wrapped()
            wrapped = cls(inner)
            wrapped.list_keys()
            self.assertEqual(wrapped.sizes()["index-0"], 77, cls.__name__)

    def test_a_wrapper_over_a_transport_that_cannot_size_stays_quiet(self):
        # Delegation must not invent an answer for a transport that has none.
        from generation_chain.sources.budget import MemoryBudget

        class Sizeless:
            def describe(self): return "sizeless"
            def list_keys(self): return ["a"]
            def fetch(self, key): return b""
            def exists(self, key): return True

        self.assertEqual(MemoryBudget(Sizeless(), limit_bytes=1 << 20).sizes(), {})


class TestTheReportSizesEveryDisposition(unittest.TestCase):
    """An operator sizing a leak needs the categories the tool leaves alone.

    The manifest is not the leak. On a run where every shard directory was
    dropped it was 51.97 MB of metadata while 92.86 GB of segment blobs sat in
    `unexplained`, and nothing in the report said so. A reader could take the
    reclaimable figure for the size of the problem and be wrong by three orders
    of magnitude.
    """

    def _placements(self, spec):
        from generation_chain.derivation.classification import Placement
        out = []
        for disposition, n in spec.items():
            out += [Placement(key=f"{disposition}-{i}", disposition=disposition,
                              detail="") for i in range(n)]
        return out

    def test_bytes_are_summed_per_disposition(self):
        from generation_chain.reporting.coverage import bytes_by_disposition
        placements = self._placements({"orphaned": 2, "unexplained": 1})
        sizes = {"orphaned-0": 100, "orphaned-1": 200, "unexplained-0": 4_000}
        got = bytes_by_disposition(placements, sizes)
        self.assertEqual(got["orphaned"], (2, 100 + 200, 0))
        self.assertEqual(got["unexplained"], (1, 4_000, 0))

    def test_a_key_with_no_size_is_counted_not_guessed(self):
        from generation_chain.reporting.coverage import bytes_by_disposition
        placements = self._placements({"evidence": 3})
        got = bytes_by_disposition(placements, {"evidence-0": 50})
        self.assertEqual(got["evidence"], (3, 50, 2))

    def _report(self, spec, sizes):
        from generation_chain.model import AuditResult, Coverage
        from generation_chain.reporting import coverage as cov
        result = AuditResult(condemned=[], coverage=Coverage(),
                             classification=self._placements(spec))
        out = io.StringIO()
        cov.write_report(result, "local", "/tmp/x", out, sizes=sizes)
        return out.getvalue()

    def test_the_report_names_the_size_the_run_left_alone(self):
        text = self._report({"orphaned": 1, "unexplained": 1},
                            {"orphaned-0": 1_000_000, "unexplained-0": 8_000_000_000})
        self.assertIn("8.0 GB", text)

    def test_the_report_says_unexplained_is_not_known_garbage(self):
        # It is the honest answer when a run could not decide, and some of it is
        # live. Printing 92 GB beside the manifest without saying so would invite
        # an operator to read it as a target.
        text = self._report({"unexplained": 1}, {"unexplained-0": 8_000_000_000})
        block = text.split("Dispositions", 1)[1].split("\nReclaimable", 1)[0]
        self.assertIn("not known garbage", block)
        self.assertIn("some of it is live", block)

    def test_only_orphaned_is_named_as_the_delete_list(self):
        text = self._report({"orphaned": 1, "unexplained": 1},
                            {"orphaned-0": 10, "unexplained-0": 8_000_000_000})
        block = text.split("Dispositions", 1)[1].split("\nReclaimable", 1)[0]
        self.assertIn("Only `orphaned` is a list of things to delete", block)
