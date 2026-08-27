"""The harness must not keep running when the audit underneath it is broken.

A run that produces a hundred tidy rows of zeroes reads exactly like a run that
found nothing to do. That happened: the harness was launched from a directory
outside the repository, `python3 -m generation_chain` did not resolve, every
cycle exited 1, and the loop carried on because it only ever inspected `failed`
and `unconfirmed`. Fourteen cycles of `deleted=0 failed=0 unconfirmed=0` looked
like a clean pass while nothing at all had been audited.
"""

import os
import subprocess
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import reclaim_test_protocol as protocol


def _args(**over):
    base = dict(start=1, cycles=5, mode="metadata", sleep=0, out=None,
                elasticsearch=None, credentials="/tmp/c.json",
                endpoint="http://127.0.0.1:9000", region="r", bucket="b",
                prefix="p/", repository=None)
    base.update(over)
    return types.SimpleNamespace(**base)


def _row(n, **over):
    row = {"cycle": n, "utc": "t", "mode": "metadata", "settle": "not waited",
           "shards_read": "2/2", "segments_condemned": 0, "deleted": 1,
           "failed": 0, "unconfirmed": 0, "reclaimable": "", "exit": 0}
    row.update(over)
    return row


class TheAuditIsInvokedWhereItsPackageLives(unittest.TestCase):
    def test_the_subprocess_runs_from_the_repository_root(self):
        # The harness calls `python3 -m generation_chain`, which resolves only
        # when the process cwd holds that package. Launching the harness by
        # absolute path from a scratch directory made every audit fail with
        # "No module named generation_chain", so the cwd cannot be inherited.
        seen = {}

        def fake_run(cmd, **kwargs):
            seen.update(kwargs)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        original = subprocess.run
        subprocess.run = fake_run
        try:
            with tempfile.TemporaryDirectory() as tmp:
                protocol.run(["true"], os.path.join(tmp, "out.txt"), 10)
        finally:
            subprocess.run = original
        self.assertEqual(seen.get("cwd"), protocol.ROOT)
        self.assertTrue(os.path.isdir(os.path.join(protocol.ROOT,
                                                   "generation_chain")))


class BothStreamsReachTheFileTheCountsAreReadFrom(unittest.TestCase):
    """The tally is on stdout and the report is on stderr. Keep both.

    `generation_chain.reclaim` writes `deleted:`, `failed:` and
    `unconfirmed:` to stdout. The harness used to pipe stdout, hand it back,
    and drop it at the call site, then parse the file, which held stderr
    alone. So every execute reported zero deleted while objects really were
    being removed, confirmed against a live store returning 404 afterwards.

    Cosmetic would have been survivable. The run-ending check reads those same
    three numbers, so a batch with failed or unconfirmed keys reported zero and
    the run carried on deleting. For a tool with no recovery path on Oracle
    that is the wrong direction to be blind in.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="protocol-streams-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def _run(self, script):
        out = os.path.join(self.tmp, "out.txt")
        protocol.run([sys.executable, "-c", script], out, 30)
        return open(out).read()

    def test_stdout_reaches_the_file(self):
        text = self._run("import sys; sys.stdout.write('deleted: 7\\n')")
        self.assertIn("deleted: 7", text)

    def test_stderr_still_reaches_the_file(self):
        text = self._run("import sys; sys.stderr.write('shard directories read: 2 of 2\\n')")
        self.assertIn("shard directories read: 2 of 2", text)

    def test_a_tally_split_across_both_streams_is_counted(self):
        # The real shape: the audit's report on stderr, the reclaim tally on
        # stdout, both needed by the same cycle.
        text = self._run(
            "import sys;"
            "sys.stderr.write('shard directories read: 4 of 4\\n');"
            "sys.stdout.write('deleted: 12\\nfailed: 0\\nunconfirmed: 3\\n')")
        self.assertEqual(protocol.counted(protocol.DELETED, text), 12)
        self.assertEqual(protocol.counted(protocol.FAILED, text), 0)
        self.assertEqual(protocol.counted(protocol.UNCONFIRMED, text), 3)

    def test_the_counts_stay_line_anchored(self):
        # A key whose name contains the word must not be read as a tally.
        text = self._run(
            "import sys; sys.stdout.write("
            "'  indices/x/0/deleted: 99\\n'"
            "'deleted: 5\\n')")
        self.assertEqual(protocol.counted(protocol.DELETED, text), 5)


class TheReclaimCallStatesItsCorroborationChoice(unittest.TestCase):
    """The harness drives the reclaim CLI, so it has to satisfy its contract.

    `--execute` requires the operator to say whether the Elasticsearch veto was
    re-checked against the cluster as it is now. The harness did not pass
    either flag, so every execute refused and every cycle reported deleted=0
    while the audit underneath was working perfectly. The tell was an exec file
    that existed and contained a refusal rather than a tally.
    """

    def _command(self, **over):
        args = _args(**over)
        return protocol.reclaim_command(args, "/tmp/m.tsv")

    def test_a_cluster_is_passed_through_when_one_was_named(self):
        command = self._command(elasticsearch="http://es:9200",
                                repository="repo")
        self.assertIn("--elasticsearch", command)
        self.assertIn("http://es:9200", command)
        self.assertIn("--es-repository", command)
        self.assertIn("repo", command)
        self.assertNotIn("--without-elasticsearch", command)

    def test_declining_is_stated_when_no_cluster_was_named(self):
        command = self._command(elasticsearch=None)
        self.assertIn("--without-elasticsearch", command)
        self.assertNotIn("--elasticsearch", command)

    def test_the_choice_is_always_one_or_the_other(self):
        # Neither is what the harness used to send, and it made every execute
        # refuse.
        for over in ({"elasticsearch": "http://es:9200", "repository": "r"},
                     {"elasticsearch": None}):
            with self.subTest(**over):
                command = self._command(**over)
                said = ("--elasticsearch" in command
                        or "--without-elasticsearch" in command)
                self.assertTrue(said)


class ABrokenAuditStopsTheRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="protocol-halt-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.tsv = os.path.join(self.tmp, "cycles.tsv")

    def _drive(self, rows):
        produced = iter(rows)
        calls = []

        def fake_cycle(args, n, mode, outdir, log):
            calls.append(n)
            return next(produced)

        original = protocol.cycle
        protocol.cycle = fake_cycle
        try:
            protocol.run_cycles(_args(out=self.tmp), self.tsv,
                                protocol.COLUMNS, lambda m: None)
        finally:
            protocol.cycle = original
        return calls

    def test_a_nonzero_audit_exit_stops_the_run(self):
        # The defect. Without this the harness burns every remaining cycle
        # against an audit that is not running, and reports zeroes.
        calls = self._drive([_row(1, exit=1, deleted=0)] +
                            [_row(n) for n in range(2, 6)])
        self.assertEqual(calls, [1])

    def test_a_clean_cycle_does_not_stop_the_run(self):
        # The counterpart, so the halt cannot be satisfied by refusing always.
        calls = self._drive([_row(n) for n in range(1, 6)])
        self.assertEqual(calls, [1, 2, 3, 4, 5])

    def test_a_failed_delete_still_stops_the_run(self):
        # The halt that already existed must survive the new one.
        calls = self._drive([_row(1), _row(2, failed=3)] +
                            [_row(n) for n in range(3, 6)])
        self.assertEqual(calls, [1, 2])

    def test_an_unconfirmed_delete_still_stops_the_run(self):
        calls = self._drive([_row(1, unconfirmed=2)] +
                            [_row(n) for n in range(2, 6)])
        self.assertEqual(calls, [1])


class CorroborationNeedsACredentialTheAuditCanRead(unittest.TestCase):
    """--es-password-file authenticates the harness, never the audit.

    The two credential inputs did not compose. `--es-user` and
    `--es-password-file` feed only this harness's own calls to Elasticsearch,
    the ones driving the settle wait. The audit is a separate process and
    reads its cluster credential from the `elasticsearch` section of the file
    named by `--credentials`, and it never falls back to the environment once
    that file is given. So the documented invocation refused on cycle 1, every
    time, on any repository, and the run had to be thrown away.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="protocol-creds-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def _creds(self, body):
        path = os.path.join(self.tmp, "creds.json")
        with open(path, "w") as fh:
            fh.write(body)
        return path

    def test_a_credentials_file_without_the_section_is_refused(self):
        path = self._creds('{"s3": {"access_key_id": "a", '
                           '"secret_access_key": "b"}}')
        args = _args(elasticsearch="http://es:9200", credentials=path)
        problem = protocol.corroboration_credential_problem(args)
        self.assertIsNotNone(problem)
        self.assertIn(path, problem)
        self.assertIn("elasticsearch", problem)

    def test_a_username_and_password_section_is_accepted(self):
        path = self._creds('{"s3": {}, "elasticsearch": '
                           '{"username": "u", "password": "p"}}')
        args = _args(elasticsearch="http://es:9200", credentials=path)
        self.assertIsNone(protocol.corroboration_credential_problem(args))

    def test_an_api_key_section_is_accepted(self):
        path = self._creds('{"s3": {}, "elasticsearch": {"api_key": "k"}}')
        args = _args(elasticsearch="http://es:9200", credentials=path)
        self.assertIsNone(protocol.corroboration_credential_problem(args))

    def test_no_corroboration_requested_needs_no_section(self):
        # The audit runs perfectly well without a cluster. Refusing here would
        # break every run that never asked for corroboration.
        path = self._creds('{"s3": {"access_key_id": "a"}}')
        args = _args(elasticsearch=None, credentials=path)
        self.assertIsNone(protocol.corroboration_credential_problem(args))

    def test_an_unreadable_credentials_file_is_refused_not_crashed(self):
        # Refuse with a message. A traceback out of argument validation tells
        # the operator nothing about what to fix.
        path = self._creds("{ this is not json")
        args = _args(elasticsearch="http://es:9200", credentials=path)
        problem = protocol.corroboration_credential_problem(args)
        self.assertIsNotNone(problem)
        self.assertIn(path, problem)

    def test_the_check_actually_runs_before_the_first_cycle(self):
        # The predicate being right is worth nothing if nothing calls it. A
        # neuter run found this unpinned: removing the call from main() left
        # the whole suite green. So drive the command line itself and require
        # that it refuses, creating nothing.
        import contextlib
        import io
        path = self._creds('{"s3": {"access_key_id": "a"}}')
        out = os.path.join(self.tmp, "never-created")
        argv = ["reclaim_test_protocol.py",
                "--cycles", "1", "--mode", "metadata",
                "--transport", "s3", "--endpoint", "http://127.0.0.1:1",
                "--region", "r", "--bucket", "b", "--prefix", "p/",
                "--credentials", path,
                "--elasticsearch", "http://127.0.0.1:9200",
                "--repository", "repo", "--out", out]
        original, sys.argv = sys.argv, argv
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as raised:
                    protocol.main()
        finally:
            sys.argv = original
        self.assertNotEqual(raised.exception.code, 0)
        self.assertIn("elasticsearch", stderr.getvalue())
        self.assertFalse(os.path.exists(out),
                         "it created its output directory before refusing")

    def test_the_refusal_never_quotes_the_file_contents(self):
        # The message is read by whoever runs this, and the file it names is
        # full of secrets.
        secret = "s3cr3t-value-that-must-not-appear"
        path = self._creds('{"s3": {"secret_access_key": "%s"}}' % secret)
        args = _args(elasticsearch="http://es:9200", credentials=path)
        self.assertNotIn(secret,
                         protocol.corroboration_credential_problem(args))


if __name__ == "__main__":
    unittest.main()
