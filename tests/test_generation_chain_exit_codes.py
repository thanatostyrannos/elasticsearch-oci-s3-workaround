"""Exit codes a scheduler acts on, and output paths refused before the run.

A scheduler retries exit 4 and pages a person on exit 3. A store that says
401 or 403 will say it again on every retry, so it has to read as "fix the
credential", and a 30 minute run must not end on a mistyped output path.
"""

import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import genchain_fixtures as fx
import s3rig
from generation_chain import cli
from generation_chain.derivation.audit import run_audit
from generation_chain.sources.http_reads import HttpReader
from generation_chain.sources.s3 import S3CompatibleSource, S3Credentials

HISTORY = [
    {"s1": {"idx": ["__a"]}},
    {"s1": {"idx": ["__a"]}, "s2": {"idx": ["__b"]}},
    {"s2": {"idx": ["__b"]}},
]


class _Cli(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-exit-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        fx.build_repository(self.root, HISTORY)

    def source(self, rig, secret=s3rig.TEST_SECRET_KEY):
        return S3CompatibleSource(
            endpoint=rig.endpoint, region=s3rig.TEST_REGION,
            bucket=rig.bucket,
            credentials=S3Credentials(s3rig.TEST_ACCESS_KEY, secret),
            reader=HttpReader(sleep=lambda _s: None, jitter=lambda: 0.0))

    def run_cli(self, rig, argv, secret=s3rig.TEST_SECRET_KEY, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "build_source",
                               return_value=self.source(rig, secret)), \
                mock.patch.dict(os.environ, env or {}):
            code = cli.main(["--local-repo", self.root, "--quiet",
                             "--memory-mb", "0"] + argv,
                            stdout=out, stderr=err)
        return code, err.getvalue()


class StoreRefusals(_Cli):

    def test_a_wrong_secret_exits_3_and_names_the_credential(self):
        # A revoked key answers 403 on every retry. Exit 4 sent a scheduler
        # into an endless retry loop against a credential that stays wrong.
        with s3rig.S3Rig(self.root) as rig:
            code, err = self.run_cli(rig, [], secret="WRONG-secret-000000")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("credential", err)

    def test_the_right_secret_still_exits_0(self):
        # Use case beside the abuse case: the new refusal must not catch a
        # healthy run, or every scheduled audit stops.
        with s3rig.S3Rig(self.root) as rig:
            code, _err = self.run_cli(rig, [])
        self.assertEqual(code, cli.EXIT_OK)

    def test_a_503_on_the_listing_still_exits_4(self):
        # A busy store recovers. Folding 5xx into exit 3 would page a person
        # for an outage a retry clears.
        with s3rig.S3Rig(self.root, faults=[(503, "SlowDown")] * 40) as rig:
            code, _err = self.run_cli(rig, [])
        self.assertEqual(code, cli.EXIT_TRANSPORT)

    def test_a_429_on_the_listing_still_exits_4(self):
        # Throttling clears with time. Treating it as a bad credential
        # would stop a scheduler from ever retrying a rate limit.
        with s3rig.S3Rig(self.root, faults=[(429, "TooManyRequests")] * 40) as rig:
            code, _err = self.run_cli(rig, [])
        self.assertEqual(code, cli.EXIT_TRANSPORT)

    def test_a_missing_index_latest_exits_3_and_names_the_prefix(self):
        # A wrong --prefix lists fine and finds no index.latest. Exit 4 told
        # the operator to retry a command that names the wrong place.
        os.unlink(os.path.join(self.root, "index.latest"))
        with s3rig.S3Rig(self.root) as rig:
            code, err = self.run_cli(rig, [])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("prefix", err)

    def test_a_403_on_index_latest_alone_exits_3(self):
        # A policy that lists but cannot read is still a credential problem
        # that a retry will not fix.
        with s3rig.S3Rig(self.root, faults={"GET": [(403, "AccessDenied")] * 40}) as rig:
            code, _err = self.run_cli(rig, [])
        self.assertEqual(code, cli.EXIT_USAGE)

    def test_a_404_on_a_generation_blob_is_not_called_a_bad_prefix(self):
        # Abuse case. A generation blob that vanished mid-run is not a wrong
        # prefix, and saying so would send the operator after the wrong fix.
        os.unlink(os.path.join(self.root, "index-2"))
        with s3rig.S3Rig(self.root) as rig:
            code, _err = self.run_cli(rig, [])
        self.assertNotEqual(code, cli.EXIT_USAGE)

    def test_a_refusal_by_status_is_not_derived_from_message_text(self):
        # A 5xx body that happens to say 403 must stay transient. Reading the
        # status out of prose would turn an outage into a credential page.
        with s3rig.S3Rig(self.root, faults=[(503, "Err403AccessDenied")] * 40) as rig:
            result = run_audit(self.source(rig))
        self.assertTrue(result.coverage.refusal_is_transient)


class OutputPathsBeforeTheRun(_Cli):

    def assert_refused_untouched(self, rig, code, err):
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(len(rig.requests), 0)
        self.assertNotIn("Traceback", err)

    def test_an_unwritable_manifest_directory_exits_3_with_no_store_read(self):
        # Before the check this cost the whole run and ended in a traceback.
        missing = os.path.join(self.dir, "no-such-dir", "m.tsv")
        with s3rig.S3Rig(self.root) as rig:
            code, err = self.run_cli(rig, ["--manifest", missing])
            self.assert_refused_untouched(rig, code, err)
        self.assertIn("--manifest", err)

    def test_a_read_only_directory_exits_3_with_no_store_read(self):
        # A directory that exists and refuses writes fails the same way after
        # the same wasted run, so existence alone is not a check.
        locked = os.path.join(self.dir, "locked")
        os.mkdir(locked)
        os.chmod(locked, 0o500)
        self.addCleanup(os.chmod, locked, 0o700)
        if os.access(locked, os.W_OK):
            self.skipTest("running as a user that ignores directory modes")
        with s3rig.S3Rig(self.root) as rig:
            code, err = self.run_cli(
                rig, ["--classification", os.path.join(locked, "c.tsv")])
            self.assert_refused_untouched(rig, code, err)

    def test_a_path_outside_the_file_root_exits_3_with_no_store_read(self):
        # Abuse case. GENCHAIN_FILE_ROOT confines a scheduled job. A path
        # outside it used to be found only after the run.
        confined = os.path.join(self.dir, "confined")
        os.mkdir(confined)
        with s3rig.S3Rig(self.root) as rig:
            code, err = self.run_cli(
                rig, ["--coverage-json", os.path.join(self.dir, "c.json")],
                env={"GENCHAIN_FILE_ROOT": confined})
            self.assert_refused_untouched(rig, code, err)
        self.assertIn("--coverage-json", err)

    def test_an_empty_path_exits_3_with_no_store_read(self):
        # Abuse case. An empty --manifest from an unset shell variable would
        # otherwise run for half an hour first.
        with s3rig.S3Rig(self.root) as rig:
            code, err = self.run_cli(rig, ["--manifest", "  "])
            self.assert_refused_untouched(rig, code, err)

    def test_a_writable_path_leaves_no_probe_file_behind(self):
        # Use case. The probe must clean up, or every run litters the
        # evidence directory with stray files.
        out = os.path.join(self.dir, "out")
        os.mkdir(out)
        with s3rig.S3Rig(self.root) as rig:
            code, _err = self.run_cli(
                rig, ["--manifest", os.path.join(out, "m.tsv")])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(os.listdir(out), ["m.tsv"])

    def test_a_path_that_turns_unwritable_after_the_check_exits_3(self):
        # A directory removed during the run must end in a message and exit
        # 3, not a traceback and exit 1 after the work is done.
        out = os.path.join(self.dir, "out")
        os.mkdir(out)
        real = cli.run_audit

        def audit_then_remove(*a, **k):
            result = real(*a, **k)
            shutil.rmtree(out)
            return result

        with s3rig.S3Rig(self.root) as rig, \
                mock.patch.object(cli, "run_audit", audit_then_remove):
            code, err = self.run_cli(
                rig, ["--manifest", os.path.join(out, "m.tsv")])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
