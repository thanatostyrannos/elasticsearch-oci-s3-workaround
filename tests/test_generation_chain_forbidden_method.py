"""ForbiddenMethod reaches the operator instead of becoming a read failure.

The derivation turns every SourceReadError into less output, which is safe for
a read that failed. A request the package must never send is not a failed
read, so folding it into one hides the single promise the tool makes.
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import genchain_fixtures as fx
from generation_chain import cli
from generation_chain.credentials import Secret
from generation_chain.derivation.keys import KeyIndex
from generation_chain.errors import ForbiddenMethod, SourceReadError
from generation_chain.sources import GuardedSource
from generation_chain.sources.local import LocalMirrorSource
from generation_chain.sources.oci import OciNativeSource
from generation_chain.sources.s3 import S3CompatibleSource, S3Credentials

from test_generation_chain_transports import _oci_credentials

HISTORY = [{"s1": {"idx": ["__a"]}}, {"s2": {"idx": ["__b"]}}]


class _SendsNothing:
    """A reader that records every request, so a test can see none left."""

    def __init__(self):
        self.calls = []

    def get(self, url, headers, method="GET", **_kw):
        self.calls.append(method)
        raise AssertionError("a request reached the wire")


def _s3(reader):
    return S3CompatibleSource(
        endpoint="https://objects.example.com", region="r", bucket="b",
        credentials=S3Credentials("AK", Secret("SK")), reader=reader)


def _oci(reader):
    return OciNativeSource(
        endpoint="https://objects.example.com", namespace="n", bucket="b",
        credentials=_oci_credentials(), reader=reader)


def _send_delete(source):
    if isinstance(source, S3CompatibleSource):
        return source._request("DELETE", "/b/key", {})
    return source._request("DELETE", "/n/n/b/b/o/key")


class _Deleting:
    """A source whose every read tries to send a DELETE through a transport."""

    def __init__(self, transport):
        self.transport = transport

    def describe(self):
        return "deleting"

    def list_keys(self):
        return _send_delete(self.transport)

    def fetch(self, key):
        return _send_delete(self.transport)

    def exists(self, key):
        return _send_delete(self.transport)


class TransportsRefuseForbiddenMethods(unittest.TestCase):

    def test_a_delete_is_refused_before_anything_is_signed_or_sent(self):
        # If either transport let a DELETE through, "reads and never deletes"
        # would be false for the one request that destroys a repository.
        for make in (_s3, _oci):
            reader = _SendsNothing()
            with self.subTest(make.__name__):
                with self.assertRaises(ForbiddenMethod):
                    _send_delete(make(reader))
                self.assertEqual(reader.calls, [])

    def test_a_refusal_is_not_an_assertion_error(self):
        # Callers that re-raise ForbiddenMethod would miss an AssertionError,
        # and the escalation would silently depend on a stripped statement.
        for make in (_s3, _oci):
            with self.subTest(make.__name__):
                try:
                    _send_delete(make(_SendsNothing()))
                except ForbiddenMethod:
                    pass
                except AssertionError:
                    self.fail("raised AssertionError, not ForbiddenMethod")

    def test_a_read_is_still_allowed_through(self):
        # A method check that refused everything would pass the tests above
        # while stopping every audit.
        reader = _SendsNothing()
        with self.assertRaises(AssertionError):
            _s3(reader)._request("GET", "/b/key", {})
        self.assertEqual(reader.calls, ["GET"])


class EscalationThroughTheBoundary(unittest.TestCase):

    def test_the_guard_passes_a_forbidden_method_through_unchanged(self):
        # Folded into SourceReadError, the derivation drops the shard and the
        # run finishes with a shorter manifest as though nothing happened.
        for make in (_s3, _oci):
            guarded = GuardedSource(_Deleting(make(_SendsNothing())))
            for name, call in (("list", guarded.list_keys),
                               ("fetch", lambda: guarded.fetch("k")),
                               ("exists", lambda: guarded.exists("k"))):
                with self.subTest(f"{make.__name__} {name}"):
                    with self.assertRaises(ForbiddenMethod):
                        call()

    def test_the_key_index_passes_a_forbidden_method_through(self):
        # A confirmation that swallowed it would record the key UNANSWERED and
        # the run would carry on past a request it must never have built.
        index = KeyIndex(["k"], _Deleting(_s3(_SendsNothing())))
        with self.assertRaises(ForbiddenMethod):
            index.confirm("k")

    def test_an_ordinary_read_failure_still_becomes_a_source_read_error(self):
        # Abuse case for the fix itself: re-raising too much would crash runs
        # on a store that was merely busy instead of shortening them.
        class Broken:
            def describe(self):
                return "broken"

            def fetch(self, key):
                raise ConnectionResetError("reset")

            def exists(self, key):
                raise ConnectionResetError("reset")

        with self.assertRaises(SourceReadError):
            GuardedSource(Broken()).fetch("k")
        self.assertEqual(KeyIndex(["k"], Broken()).confirm("k"), "unanswered")

    def test_a_forbidden_method_survives_python_dash_O(self):
        # `-O` strips assert. If the check regressed to one, this is the run
        # that would send a DELETE while every normal test run stayed green.
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {ROOT!r})
            sys.path.insert(0, {os.path.join(ROOT, "tests")!r})
            from generation_chain.errors import ForbiddenMethod
            from generation_chain.sources import GuardedSource
            from generation_chain.derivation.keys import KeyIndex
            import test_generation_chain_forbidden_method as t

            if __debug__:
                raise SystemExit("not running under -O")
            for make in (t._s3, t._oci):
                reader = t._SendsNothing()
                src = t._Deleting(make(reader))
                for call in (GuardedSource(src).list_keys,
                             lambda: KeyIndex(["k"], src).confirm("k")):
                    try:
                        call()
                    except ForbiddenMethod:
                        continue
                    raise SystemExit("no ForbiddenMethod from " + make.__name__)
                if reader.calls:
                    raise SystemExit("sent " + repr(reader.calls))
            print("ok")
        """)
        done = subprocess.run([sys.executable, "-O", "-c", script],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "ok"),
                         done.stderr)


class CommandLineExit(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-forbidden-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        fx.build_repository(self.root, HISTORY)

    def run_cli(self, patched):
        out, err = io.StringIO(), io.StringIO()
        manifest = os.path.join(self.dir, "out.tsv")
        with patched:
            code = cli.main(["--local-repo", self.root, "--manifest", manifest],
                            stdin=io.StringIO(), stdout=out, stderr=err)
        return code, err.getvalue()

    def forbid(self, method):
        def refuse(*_a, **_kw):
            raise ForbiddenMethod("DELETE is not a method this package may send")
        return mock.patch.object(LocalMirrorSource, method, refuse)

    def test_a_forbidden_method_is_not_reported_as_a_store_failure(self):
        # Exit 4 tells a scheduler to retry. Retrying a request this package
        # must never send would repeat it on every backoff.
        for method in ("list_keys", "fetch"):
            with self.subTest(method):
                code, err = self.run_cli(self.forbid(method))
                self.assertNotIn(code, (cli.EXIT_OK, cli.EXIT_TRANSPORT))
                self.assertEqual(code, cli.EXIT_REFUSED)
                self.assertIn("DELETE", err)

    def test_an_unreachable_store_still_exits_as_a_transport_failure(self):
        # Abuse case: mapping ForbiddenMethod must not move the ordinary
        # store failure off the retryable code.
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["--local-repo", "/nonexistent"], stdin=io.StringIO(),
                        stdout=out, stderr=err)
        self.assertEqual(code, cli.EXIT_TRANSPORT)


if __name__ == "__main__":
    unittest.main()
