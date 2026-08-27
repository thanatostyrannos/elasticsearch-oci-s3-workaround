"""The Elasticsearch veto: it protects, and it never condemns.

The direction is the whole design. Everything the cluster reports leaves the
manifest; nothing it fails to report is thereby condemnable. That makes the
veto a subtraction from a finished list rather than an input to the decision,
so it cannot make the output larger by construction.

The failure case is the one this project has actually lost before. The retired
sweepers aborted correctly when a cross-check could not reach Elasticsearch,
and what defeated the guard was prose: a README that built a key which 403s on
the very call the guard needs, and then said elsewhere that the flag was
optional. So the tests here pin the CODE shape as well as the behaviour.
"""

import ast
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import genchain_fixtures as fx
from generation_chain import cli, corroboration, run_audit
from generation_chain.corroboration import (Credentials, CorroborationUnavailable,
                                            ElasticsearchVeto)
from generation_chain.sources.local import LocalMirrorSource

HISTORY = [
    {"s1": {"idx": ["__a", "__shared"]}},
    {"s1": {"idx": ["__a", "__shared"]}, "s2": {"idx": ["__b", "__shared"]}},
    {"s2": {"idx": ["__b", "__shared"]}},
]

# Shaped like the answers the 9.5.2 test cluster gives. The mounted-index
# settings were read off that cluster rather than invented.
SNAPSHOTS = {"snapshots": [{"snapshot": "s2", "uuid": "uuid-s2"}]}
NO_MOUNTS = {}
MOUNTED = {"frozen-idx": {"settings": {
    "index.store.snapshot.snapshot_uuid": "uuid-s1",
    "index.store.snapshot.index_uuid": "iuuid-idx",
    "index.store.snapshot.repository_name": "repo"}}}
IDLE = {"snapshots": []}


class _Answer:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def _opener(answers):
    calls = iter(answers)

    def open_it(request, timeout=None, **kwargs):
        answer = next(calls)
        if isinstance(answer, Exception):
            raise answer
        return _Answer(answer)
    return open_it


class VetoDirection(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-veto-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        fx.build_repository(self.root, HISTORY)
        self.baseline = set(run_audit(LocalMirrorSource(self.root)).keys)

    def veto(self, answers):
        return ElasticsearchVeto("http://es.invalid", "repo", Credentials(),
                                 opener=_opener(answers)).fetch()

    def test_a_mounted_index_protects_the_snapshot_it_reads(self):
        # The most valuable thing a cluster can say. Elasticsearch does not
        # block deleting a snapshot that backs a mounted searchable-snapshot
        # index, and SLM has no mount awareness, so the index stays green and
        # serving until its next restart and then fails with nothing
        # connecting it to the sweep.
        veto = self.veto([SNAPSHOTS, MOUNTED, IDLE])
        self.assertIn("uuid-s1", veto.snapshot_uuids)
        self.assertIn("iuuid-idx", veto.index_uuids)
        after = set(run_audit(LocalMirrorSource(self.root), veto).keys)
        self.assertEqual(after, set())
        self.assertTrue(after.issubset(self.baseline))

    def test_a_cluster_that_protects_nothing_takes_nothing_away(self):
        # Abuse case for the direction. A cluster reporting only the live
        # snapshot must not turn the manifest into a longer one, and absence
        # from its answer must not condemn anything that survived the
        # derivation.
        veto = self.veto([SNAPSHOTS, NO_MOUNTS, IDLE])
        after = set(run_audit(LocalMirrorSource(self.root), veto).keys)
        self.assertEqual(after, self.baseline)

    def test_the_dispositions_agree_with_the_manifest_after_a_veto(self):
        # The dispositions and the manifest are two views of one answer. An
        # operator reading "orphaned: 17" above a manifest of four rows takes
        # the difference for a bug in whichever they trusted less, and then
        # trusts neither.
        veto = self.veto([SNAPSHOTS, MOUNTED, IDLE])
        result = run_audit(LocalMirrorSource(self.root), veto)
        orphaned = {p.key for p in result.classification
                    if p.disposition == "orphaned"}
        self.assertEqual(orphaned, {row.key for row in result.condemned})
        self.assertTrue(any(p.disposition == "protected"
                            for p in result.classification))

    def test_a_snapshot_in_flight_is_protected(self):
        # A snapshot being written right now is cluster state and appears
        # nowhere in the bucket. Its blobs are in the store and no committed
        # generation names them yet.
        veto = self.veto([SNAPSHOTS, NO_MOUNTS,
                          {"snapshots": [{"snapshot": "s1", "uuid": "uuid-s1"}]}])
        self.assertIn("uuid-s1", veto.snapshot_uuids)
        self.assertEqual(veto.in_flight, ("s1",))


class FailureIsNotAnEmptyVeto(unittest.TestCase):

    def fetch(self, answers):
        return ElasticsearchVeto("http://es.invalid", "repo", Credentials(),
                                 opener=_opener(answers)).fetch()

    def test_every_way_the_call_can_fail_raises(self):
        # Proceeding after a failed corroboration produces a manifest LARGER
        # than a successful call would have, which is the one property this
        # tool exists to guarantee it never has. A 403 in particular is the
        # shape this project has met: a key that works for everything except
        # the call the guard needs.
        failures = [
            urllib.error.HTTPError("u", 403, "forbidden", {}, None),
            urllib.error.HTTPError("u", 404, "no such repository", {}, None),
            urllib.error.URLError("connection refused"),
            TimeoutError("timed out"),
        ]
        for failure in failures:
            with self.assertRaises(CorroborationUnavailable, msg=repr(failure)):
                self.fetch([failure])

    def test_an_answer_that_will_not_parse_raises(self):
        # Abuse case for a proxy or a login page in front of the cluster.
        # Reading an unparseable body as "nothing to protect" is the same
        # defect as reading a failure that way.
        def bad(request, timeout=None, **kwargs):
            class Body:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    return False

                def read(self_inner):
                    return b"<html>login</html>"
            return Body()
        with self.assertRaises(CorroborationUnavailable):
            ElasticsearchVeto("http://es.invalid", "repo", Credentials(),
                              opener=bad).fetch()

    def test_a_snapshot_list_with_no_uuids_raises(self):
        # A cluster answering with a shape this tool cannot read has not
        # corroborated anything. Skipping the entries it could not read would
        # silently shorten the set of protections.
        with self.assertRaises(CorroborationUnavailable):
            self.fetch([{"snapshots": [{"snapshot": "s"}]}, NO_MOUNTS, IDLE])
        with self.assertRaises(CorroborationUnavailable):
            self.fetch([{"ok": True}, NO_MOUNTS, IDLE])

    def test_a_requested_corroboration_that_fails_refuses_the_run(self):
        # The end-to-end contract an operator sees. An operator who asked for
        # corroboration and could not have it is in a different position from
        # one who never asked, and the tool has to treat them differently: no
        # manifest at all, rather than the longer one a missing veto produces.
        # The codes differ too, because a cluster that did not answer is worth
        # retrying and a missing credential is not.
        argv = ["--local-repo", "/nonexistent",
                "--elasticsearch", "http://127.0.0.1:1",
                "--es-repository", "repo"]
        environment = dict(os.environ)
        self.addCleanup(os.environ.update, environment)
        for name in list(os.environ):
            if name.startswith("GENCHAIN_"):
                del os.environ[name]

        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(
            cli.main(argv, stdin=io.StringIO(), stdout=out, stderr=err),
            cli.EXIT_USAGE)
        self.assertEqual(out.getvalue(), "")

        os.environ["GENCHAIN_ES_API_KEY"] = "an-api-key"
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(
            cli.main(argv, stdin=io.StringIO(), stdout=out, stderr=err),
            cli.EXIT_TRANSPORT)
        self.assertEqual(out.getvalue(), "")

    def test_no_except_block_in_the_module_can_return(self):
        # The static half, and it guards a shape rather than a behaviour:
        #
        #     try:    veto = fetch()
        #     except: veto = set()
        #
        # An empty veto on failure IS absence treated as evidence, and it
        # reads as graceful degradation, which is exactly why it survives
        # review. This makes the shape impossible to add quietly.
        source = pathlib.Path(corroboration.__file__).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ExceptHandler):
                for inner in ast.walk(node):
                    self.assertNotIsInstance(
                        inner, ast.Return,
                        f"an except block returns at line {node.lineno}")

    def test_the_documentation_never_offers_dropping_the_flag_as_a_remedy(self):
        # This project lost this guard to prose once already: correct code,
        # and a document that handed the operator a key which 403s on the very
        # call the guard needs and then said the flag was optional. The
        # cheapest way out of a failure the document caused was to disable the
        # guard the document called mandatory.
        root = pathlib.Path(corroboration.__file__).resolve().parent
        forbidden = ("omit --elasticsearch", "drop --elasticsearch",
                     "without --elasticsearch", "run without corroboration",
                     "or omit the flag")
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8").lower()
            for phrase in forbidden:
                self.assertNotIn(phrase, text, f"{path.name}: {phrase}")


class CredentialHandling(unittest.TestCase):

    def test_the_cluster_credential_is_never_a_command_line_flag(self):
        # An Elasticsearch password in argv reaches the process table and the
        # shell history of every operator on the box, the same way an access
        # key does. The rule already existed for the store; it applies here.
        flags = {action.option_strings[0]
                 for action in cli.build_parser()._actions
                 if action.option_strings}
        for flag in ("--es-password", "--es-user", "--es-api-key",
                     "--es-username"):
            self.assertNotIn(flag, flags)

    def test_an_api_key_and_a_password_produce_the_headers_they_should(self):
        # Elasticsearch rejects a malformed Authorization header with a 401
        # that names nothing, which then reads as a corroboration failure and
        # refuses the run. Getting the two schemes right is what stops a
        # working credential from looking like a broken cluster.
        self.assertEqual(Credentials(api_key="abc").header(),
                         {"Authorization": "ApiKey abc"})
        self.assertEqual(Credentials(username="elastic", password="pw").header(),
                         {"Authorization": "Basic ZWxhc3RpYzpwdw=="})
        self.assertEqual(Credentials().header(), {})


if __name__ == "__main__":
    unittest.main()
