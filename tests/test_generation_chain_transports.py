"""The three transports, and the one answer they all have to give.

This package deliberately has ONE derivation behind three transports rather
than one tool per store. The two sweepers in this repository took the other
route and drifted: for the same operator mistake one warns and the other is
silent, one catches its transport errors and the other exits on a traceback.
The equivalence test below is what stops that happening here.

A transport that failed open would also be the one input that could make this
tool name MORE keys, so each transport gets the invariant applied to it
directly rather than by argument from the local case.
"""

import ast
import base64
import http.server
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import tokenize
import subprocess
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import genchain_fixtures as fx
import s3rig
from generation_chain import run_audit
from generation_chain.sources import http_reads
from generation_chain.sources.http_reads import HttpReader
from generation_chain.sources.local import LocalMirrorSource
from generation_chain.sources.oci import OciCredentials, OciNativeSource
from generation_chain.sources.s3 import S3CompatibleSource, S3Credentials
from generation_chain.sources.signing.rsa import RsaPrivateKey

HISTORY = [
    {"s1": {"idx": ["__a", "__shared"]}},
    {"s1": {"idx": ["__a", "__shared"]}, "s2": {"idx": ["__b", "__shared"]}},
    {"s2": {"idx": ["__b", "__shared"]}},
]
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


class _OciRig:
    """The slice of Oracle's native API this package calls, over a socket.

    It checks that the Authorization header names the three signed headers in
    Oracle's order and that the signature verifies against the public half of
    the test key, so a request assembled wrongly fails here rather than
    against a tenancy nobody has.
    """

    def __init__(self, root, namespace="ns", bucket="b", prefix="",
                 page_size=3, fail_keys=(), fail_list_after=None):
        self.root = root
        self.namespace = namespace
        self.bucket = bucket
        self.prefix = (prefix.strip("/") + "/") if prefix.strip("/") else ""
        self.page_size = page_size
        self.fail_keys = set(fail_keys)
        self.fail_list_after = fail_list_after
        self.pages_served = 0
        with open(os.path.join(FIXTURES,
                               "genchain-oci-signing-vector.json")) as fh:
            key = RsaPrivateKey.from_pem(
                json.load(fh)["private_key_pkcs8"].encode())
        self.modulus = key.modulus
        self.key = key

    def keys(self):
        out = []
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, self.root).replace(os.sep, "/")
                out.append(self.prefix + rel)
        return sorted(out)

    def __enter__(self):
        rig = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_HEAD(self):
                self._head = True
                self.do_GET()

            def do_GET(self):
                try:
                    rig._check(self)
                except AssertionError as exc:
                    self.send_response(401)
                    body = str(exc).encode()
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("X-Why", str(exc))
                    self.end_headers()
                    self.wfile.write(body)
                    return None
                path, _, query = self.path.partition("?")
                params = urllib.parse.parse_qs(query)
                head = f"/n/{rig.namespace}/b/{rig.bucket}/o"
                if not path.startswith(head):
                    return self._send(404, b"no such bucket")
                name = urllib.parse.unquote(path[len(head):].lstrip("/"))
                if not name:
                    return rig._list(self, params)
                return rig._get(self, name)

            def _send(self, status, body, ctype="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if not getattr(self, "_head", False):
                    self.wfile.write(body)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def _check(self, handler):
        auth = handler.headers.get("Authorization", "")
        assert 'headers="date (request-target) host"' in auth, \
            "signed header list is not Oracle's"
        assert 'version="1"' in auth, "missing version"
        signature = base64.b64decode(auth.split('signature="')[1].split('"')[0])
        target = f"{handler.command.lower()} {handler.path}"
        message = "\n".join([
            f"date: {handler.headers.get('Date', '')}",
            f"(request-target): {target}",
            f"host: {handler.headers.get('Host', '')}",
        ]).encode()
        recovered = pow(int.from_bytes(signature, "big"), 65537, self.modulus)
        expected = int.from_bytes(self.key._pad(_sha256(message)), "big")
        assert recovered == expected, "signature does not verify"

    def _list(self, handler, params):
        start = params.get("start", [""])[0]
        prefix = params.get("prefix", [""])[0]
        keys = [k for k in self.keys() if k.startswith(prefix) and k >= start]
        self.pages_served += 1
        if (self.fail_list_after is not None
                and self.pages_served > self.fail_list_after):
            return handler._send(404, b'{"code":"NotFound"}')
        page, rest = keys[:self.page_size], keys[self.page_size:]
        body = {"objects": [{"name": k} for k in page]}
        if rest:
            body["nextStartWith"] = rest[0]
        handler._send(200, json.dumps(body).encode())

    def _get(self, handler, name):
        if name in self.fail_keys:
            return handler._send(404, b'{"code":"ObjectNotFound"}')
        if not name.startswith(self.prefix):
            return handler._send(404, b'{"code":"ObjectNotFound"}')
        path = os.path.join(self.root, name[len(self.prefix):])
        if not os.path.isfile(path):
            return handler._send(404, b'{"code":"ObjectNotFound"}')
        with open(path, "rb") as fh:
            handler._send(200, fh.read(), "application/octet-stream")


def _sha256(message):
    import hashlib
    return hashlib.sha256(message).digest()


def _oci_credentials():
    with open(os.path.join(FIXTURES, "genchain-oci-signing-vector.json")) as fh:
        pem = json.load(fh)["private_key_pkcs8"]
    return OciCredentials(key_id="t/u/f",
                          private_key=RsaPrivateKey.from_pem(pem.encode()))


class Transports(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-transport-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = os.path.join(self.dir, "repo")
        fx.build_repository(self.root, HISTORY)
        self.expected = self.keys_from(LocalMirrorSource(self.root))

    @staticmethod
    def keys_from(source):
        return set(run_audit(source).keys)

    @staticmethod
    def fast_reader():
        """No real sleeping in tests, and no jitter to make one flaky."""
        return HttpReader(sleep=lambda _seconds: None, jitter=lambda: 0.0)

    def s3_source(self, rig, prefix=""):
        return S3CompatibleSource(
            endpoint=rig.endpoint, region=s3rig.TEST_REGION,
            bucket=rig.bucket, prefix=prefix,
            credentials=S3Credentials(s3rig.TEST_ACCESS_KEY,
                                      s3rig.TEST_SECRET_KEY),
            reader=self.fast_reader())

    def oci_source(self, rig, prefix=""):
        return OciNativeSource(endpoint=rig.endpoint, namespace=rig.namespace,
                               bucket=rig.bucket, prefix=prefix,
                               credentials=_oci_credentials(),
                               reader=self.fast_reader())

    def test_the_same_repository_read_three_ways_gives_one_manifest(self):
        # If a transport changes the answer, the transport is lying, and an
        # operator comparing a mirror rehearsal against a live run would be
        # comparing two different repositories without being told.
        with s3rig.S3Rig(root=self.root, prefix="base/path") as rig:
            over_s3 = self.keys_from(self.s3_source(rig, "base/path"))
        with _OciRig(self.root, prefix="base/path") as rig:
            over_oci = self.keys_from(self.oci_source(rig, "base/path"))
        self.assertEqual(over_s3, self.expected)
        self.assertEqual(over_oci, self.expected)
        self.assertIn("indices/iuuid-idx/0/__a", self.expected)

    def test_a_listing_that_breaks_part_way_explains_nothing(self):
        # Abuse case. A store that answers page one and then fails leaves a
        # partial listing that looks exactly like a smaller repository. On the
        # reachability sweeper's side of the fence that would manufacture
        # orphans; here it has to produce no manifest at all.
        with _OciRig(self.root, fail_list_after=1) as rig:
            result = run_audit(self.oci_source(rig))
        self.assertEqual(result.condemned, [])
        self.assertIsNotNone(result.coverage.refused)

    def test_a_shard_document_the_store_will_not_serve_shrinks_the_manifest(self):
        # Abuse case, applied to each transport rather than argued from the
        # local one. A 500 on one object is a file list the tool cannot
        # attribute, so the blobs behind it leave the manifest.
        blocked = "indices/iuuid-idx/0/index-sg-idx-0-1"
        with _OciRig(self.root, fail_keys={blocked}) as rig:
            over_oci = self.keys_from(self.oci_source(rig))
        self.assertTrue(over_oci.issubset(self.expected))
        self.assertNotIn("indices/iuuid-idx/0/__a", over_oci)

    def test_a_paged_listing_is_read_to_the_end(self):
        # A repository is thousands of objects and every store pages. A
        # transport that stopped at page one would hide most of the store,
        # and this tool would then report a coverage figure computed from
        # generations it never saw.
        with s3rig.S3Rig(root=self.root, page_size=3) as rig:
            keys = set(self.s3_source(rig).list_keys())
        self.assertEqual(keys, set(fx.read_keys(self.root)))

    def test_neither_endpoint_transport_can_issue_a_delete(self):
        # Version one has no delete path anywhere. The method allowlist is
        # what makes that true at runtime rather than by reading, and it is
        # what stops a later change adding one without touching the line that
        # says why not.
        with s3rig.S3Rig(root=self.root) as rig:
            source = self.s3_source(rig)
            with self.assertRaises(AssertionError):
                source._request("DELETE", "/es-snapshots/index-0", {})
        with _OciRig(self.root) as rig:
            source = self.oci_source(rig)
            with self.assertRaises(AssertionError):
                source._request("DELETE", "/n/ns/b/b/o/index-0")


class NoDeletePath(unittest.TestCase):
    """The audit path reads. The tripwire is what keeps that true later.

    `generation_chain/reclaim` is the one deliberate exception: a separate
    package that reads an approved manifest and deletes exactly what it names
    (issue #8). Its whole reason to be its own directory, rather than a
    function bolted onto the audit tool, is so a scan like this one can draw
    a line around it and mean it. This test excludes that one directory by
    name rather than dropping any forbidden word, so `derivation/`,
    `formats/`, `reporting/`, `sources/` and the audit `cli.py` stay
    corroborators rather than a second way to destroy data, and a delete call
    reappearing OUTSIDE `reclaim/` still fails this test.

    A static scan catches the name; the method allowlist above catches an
    attempt assembled at run time under any spelling, which is the half a
    grep cannot see. Both are needed, because the batch delete is exactly the
    call a contributor optimising throughput would reach for, and its failure
    looks identical to the fault this project exists to work around.
    """

    @staticmethod
    def _code_lines(path):
        """Line numbers where code lives, excluding comments and docstrings.

        This package explains at length why the batch delete must never
        appear. A tripwire that fired on its own explanation would be deleted
        rather than heeded.
        """
        source = path.read_text(encoding="utf-8")
        exempt = set()
        for node in ast.walk(ast.parse(source)):
            body = getattr(node, "body", None)
            if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)) or not body:
                continue
            first = body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                exempt.update(range(first.lineno,
                                    (first.end_lineno or first.lineno) + 1))
        with open(path, encoding="utf-8") as handle:
            for token in tokenize.generate_tokens(handle.readline):
                if token.type == tokenize.COMMENT:
                    exempt.add(token.start[0])
        return {n: line for n, line in enumerate(source.splitlines(), 1)
                if n not in exempt}

    def test_no_module_in_the_package_carries_a_delete(self):
        # The audit path is a corroborator rather than a second way to
        # destroy data, which is what keeps it out of the entire
        # delete-safety surface. If any of these appears as code OUTSIDE
        # `reclaim/`, that claim is no longer true and every safety argument
        # in the audit tool was written about a different tool.
        forbidden = ("Delete" + "Objects", '"DELETE"', "'DELETE'",
                     "--execute", "--approve")
        root = pathlib.Path(__file__).resolve().parent.parent / "generation_chain"
        reclaim = root / "reclaim"
        hits = []
        for path in sorted(root.rglob("*.py")):
            if reclaim in path.parents:
                continue
            for number, line in self._code_lines(path).items():
                for needle in forbidden:
                    if needle in line:
                        hits.append(f"{path.name}:{number}: {needle}")
        self.assertEqual(hits, [])

    def test_the_package_imports_nothing_from_the_reachability_sweepers(self):
        # Independence from the sweeper is the entire value here. Two
        # derivations that share a parser fail together and then agree with
        # each other, which is worse evidence than one derivation alone.
        root = pathlib.Path(__file__).resolve().parent.parent / "generation_chain"
        for path in sorted(root.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    self.assertNotIn("sweeper", name, f"{path.name}: {name}")


if __name__ == "__main__":
    unittest.main()


class TheReadOnlyGuaranteeSurvivesOptimisedPython(unittest.TestCase):
    """`python3 -O` strips assert, and the method allowlist was an assert.

    This package reads and never deletes, and that promise rested on one
    check. Written as an assert it vanished under `-O` or PYTHONOPTIMIZE=1, so
    the strongest claim the tool makes held only for people who happened not
    to use a flag that exists to make Python faster.

    Tested through a recording opener rather than a socket. An earlier version
    of this test pointed at a closed port and counted the connection refusal
    as the guard working, which it is not: that test passed under -O with the
    allowlist entirely stripped. What has to be shown is that the request is
    never handed to the transport at all.
    """

    SCRIPT = (
        "import sys; sys.path.insert(0, %r)\n"
        "from generation_chain.sources.http_reads import HttpReader\n"
        "sent = []\n"
        "def opener(request, timeout=None):\n"
        "    sent.append(request.get_method())\n"
        "    raise SystemExit('the transport was reached')\n"
        "try:\n"
        "    HttpReader(opener=opener).get('http://example.invalid/x', {},\n"
        "                                  method='DELETE')\n"
        "except BaseException as exc:\n"
        "    print('raised:', type(exc).__name__)\n"
        "print('reached transport:', sent)\n"
    )

    def _run(self, flags):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return subprocess.run(
            [sys.executable] + flags + ["-c", self.SCRIPT % root],
            capture_output=True, text=True, timeout=60).stdout

    def test_a_delete_never_reaches_the_transport(self):
        self.assertIn("reached transport: []", self._run([]))

    def test_a_delete_never_reaches_the_transport_under_dash_O(self):
        # The whole point. Optimised Python must not quietly widen what this
        # package is able to send.
        out = self._run(["-O"])
        self.assertIn("reached transport: []", out,
                      "the allowlist stopped enforcing under -O")

    def test_the_refusal_is_not_a_read_failure(self):
        # Three places in derivation/ catch SourceReadError to mean "this read
        # failed, drop what it would have told us". A forbidden method must
        # not be absorbed into that: it is not a read that failed, it is a
        # request this package must never send.
        self.assertNotIn("raised: SourceReadError", self._run([]))

    def test_the_allowlist_holds_only_read_methods(self):
        self.assertEqual(sorted(http_reads.ALLOWED_METHODS), ["GET", "HEAD"])
