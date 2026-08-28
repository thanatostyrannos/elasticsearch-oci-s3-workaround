"""Paths and endpoints are data, and these pin what happens when they are bad.

Every path this tool opens was typed by whoever ran the command, and the one
endpoint it sends a delete to was typed the same way. Both are checked before
they are used. These tests hold the checks to two promises: a refusal happens
BEFORE anything is opened or sent, and a legitimate invocation is not made
harder by any of it.
"""

import os
import ssl
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generation_chain import corroboration
from generation_chain import cli
from generation_chain.credentials import CredentialFile
from generation_chain.paths import (FILE_ROOT_ENV_VAR, PathRefused,
                                    checked_path, is_inside)
from generation_chain.reclaim import cli as reclaim_cli
from generation_chain.reclaim.manifest import ManifestError, load_manifest
from generation_chain.reclaim.transport import TransportError, send_batch_delete
from generation_chain.sources.s3 import S3Credentials


class _Recorder:
    """An opener that fails the test by having been reached at all."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("a request was sent that should have been refused")


class PathShape(unittest.TestCase):
    """The checks that hold whether or not a root was configured."""

    def test_an_empty_path_is_refused(self):
        with self.assertRaises(PathRefused):
            checked_path("", "--manifest")

    def test_a_whitespace_only_path_is_refused(self):
        # `--manifest " "` is a typo, not a request to write to a file named
        # with a space. open() would happily create one.
        with self.assertRaises(PathRefused):
            checked_path("   ", "--manifest")

    def test_a_path_holding_a_nul_byte_is_refused(self):
        # Without this, open() raises a bare ValueError from inside a write
        # that has already announced its target, which reads as a crash
        # rather than as a refusal.
        with self.assertRaises(PathRefused):
            checked_path("orphans\0.tsv", "--manifest")

    def test_the_flag_that_supplied_the_path_is_named_in_the_refusal(self):
        # Three flags here take a path. A message that does not say which one
        # was wrong costs an hour of guessing.
        with self.assertRaises(PathRefused) as caught:
            checked_path("", "--coverage-json")
        self.assertIn("--coverage-json", str(caught.exception))

    def test_a_relative_path_comes_back_absolute(self):
        self.assertTrue(os.path.isabs(checked_path("orphans.tsv", "--manifest")))


class ConfinedRoot(unittest.TestCase):
    """GENCHAIN_FILE_ROOT, for a run driven by something other than a person."""

    def setUp(self):
        self.dir = os.path.realpath(
            tempfile.mkdtemp(prefix="genchain-paths-"))
        self.addCleanup(self._forget_root)

    def _forget_root(self):
        os.environ.pop(FILE_ROOT_ENV_VAR, None)

    def confine(self, root):
        os.environ[FILE_ROOT_ENV_VAR] = root

    def test_nothing_is_confined_when_nobody_named_a_root(self):
        # The default, and the case an operator running the audit by hand is
        # in. A root hardcoded here would refuse every real invocation.
        self._forget_root()
        self.assertEqual(checked_path("/etc/hosts", "--manifest"), "/etc/hosts")

    def test_a_path_inside_the_root_is_allowed(self):
        self.confine(self.dir)
        target = os.path.join(self.dir, "orphans.tsv")
        self.assertEqual(checked_path(target, "--manifest"), target)

    def test_a_path_outside_the_root_is_refused(self):
        self.confine(self.dir)
        with self.assertRaises(PathRefused):
            checked_path("/etc/hosts", "--manifest")

    def test_climbing_out_of_the_root_with_dot_dot_is_refused(self):
        # The shape the rule is actually about: a path that reads as though
        # it is inside the root and resolves somewhere else.
        self.confine(self.dir)
        with self.assertRaises(PathRefused):
            checked_path(os.path.join(self.dir, "..", "elsewhere"),
                         "--manifest")

    def test_a_symlink_that_leaves_the_root_is_refused(self):
        # Judged on where the link lands rather than on how it is spelled.
        # Tidying the text alone would let this through and write outside the
        # root anyway.
        self.confine(self.dir)
        link = os.path.join(self.dir, "out")
        os.symlink("/etc", link)
        with self.assertRaises(PathRefused):
            checked_path(os.path.join(link, "hosts"), "--manifest")

    def test_a_sibling_whose_name_starts_with_the_root_is_refused(self):
        # /var/tmp-evil starts with /var/tmp and is not inside it. Compared
        # component by component rather than as a string prefix.
        self.assertFalse(is_inside("/var/tmp-evil/x", "/var/tmp"))

    def test_the_root_itself_is_inside_the_root(self):
        self.assertTrue(is_inside("/var/tmp", "/var/tmp"))


class RefusalsReachTheCallersOwnError(unittest.TestCase):
    """A refusal must arrive as the error each caller already handles."""

    def setUp(self):
        self.dir = os.path.realpath(
            tempfile.mkdtemp(prefix="genchain-paths-callers-"))
        os.environ[FILE_ROOT_ENV_VAR] = self.dir
        self.addCleanup(os.environ.pop, FILE_ROOT_ENV_VAR, None)

    def test_the_reclaim_manifest_reader_raises_manifest_error(self):
        # reclaim's main catches ManifestError. A PathRefused escaping raw
        # would be a traceback where a refusal belongs.
        with self.assertRaises(ManifestError):
            load_manifest("/etc/hosts")

    def test_a_credentials_file_outside_the_root_is_refused(self):
        # Reached through the audit CLI's `except GenerationChainError`, so
        # this is EXIT_USAGE rather than a traceback.
        with self.assertRaises(PathRefused):
            CredentialFile.read("/etc/hosts")

    def test_the_reclaim_report_file_is_checked_before_it_is_opened(self):
        with self.assertRaises(PathRefused):
            reclaim_cli._open_report("/etc/genchain-report.jsonl")

    def test_no_report_file_asked_for_is_not_an_error(self):
        self.assertIsNone(reclaim_cli._open_report(None))


class AtomicWriteChecksItsTarget(unittest.TestCase):

    def setUp(self):
        self.dir = os.path.realpath(
            tempfile.mkdtemp(prefix="genchain-paths-write-"))

    def test_a_bad_path_leaves_no_temporary_file_behind(self):
        # The refusal happens before the temporary file is created, so an
        # interrupted-looking `.genchain-` part file cannot be left in the
        # directory an operator is about to read.
        def render(_handle):
            raise AssertionError("nothing should have been rendered")

        with self.assertRaises(PathRefused):
            cli._write_atomically(os.path.join(self.dir, "bad\0.tsv"), render)
        self.assertEqual(os.listdir(self.dir), [])

    def test_an_ordinary_path_still_writes(self):
        # The other half, and the one that matters more: a legitimate
        # invocation is not made harder by any of the above.
        target = os.path.join(self.dir, "orphans.tsv")
        cli._write_atomically(target, lambda handle: handle.write("rows\n"))
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "rows\n")


class DeleteTargets(unittest.TestCase):
    """The endpoint the one deleting request goes to, checked before the send."""

    def setUp(self):
        self.opener = _Recorder()
        self.credentials = S3Credentials("AKIAEXAMPLE", "secret")

    def send(self, scheme, host):
        return send_batch_delete(
            scheme=scheme, host=host, region="us-east-1", bucket="bucket",
            credentials=self.credentials, body=b"<Delete/>",
            checksum=("x-amz-checksum-crc32", "AAAAAA=="), timeout=1.0,
            opener=self.opener, sleep=lambda _s: None, jitter=lambda: 0.0)

    def test_an_ftp_endpoint_is_refused_and_nothing_is_sent(self):
        # urllib opens ftp:// through the same call. An endpoint naming it is
        # a typo or something steering this process somewhere it was never
        # pointed at, and this is the request that deletes.
        with self.assertRaises(TransportError):
            self.send("ftp", "evil.example.com")
        self.assertEqual(self.opener.calls, 0)

    def test_a_file_endpoint_is_refused(self):
        with self.assertRaises(TransportError):
            self.send("file", "localhost")

    def test_an_empty_host_is_refused(self):
        with self.assertRaises(TransportError):
            self.send("https", "")

    def test_a_host_carrying_userinfo_is_refused(self):
        # urllib strips `user@` before connecting, so this module would sign
        # a Host the store never sees. That fails as a bare 403 and reads
        # like a bad credential.
        # .invalid rather than .example.com: the credential guard reads a
        # name with an at sign and a dotted domain as an address, and RFC
        # 2606 reserves .invalid for exactly this, a name that cannot resolve.
        with self.assertRaises(TransportError):
            self.send("https", "user:pass@store.example.invalid")

    def test_a_host_holding_a_newline_is_refused(self):
        # Everything after the newline would be read as another header.
        with self.assertRaises(TransportError):
            self.send("https", "store.example.com\r\nX-Injected: 1")

    def test_the_refusal_says_nothing_was_sent(self):
        with self.assertRaises(TransportError) as caught:
            self.send("gopher", "store.example.com")
        self.assertIn("Nothing was sent", str(caught.exception))


class TlsFloor(unittest.TestCase):
    """The cluster credential travels over this connection either way."""

    def test_the_minimum_protocol_is_pinned_without_a_ca_file(self):
        # ssl.create_default_context leaves minimum_version at
        # MINIMUM_SUPPORTED on the Python this project supports, which lets
        # the host's OpenSSL build decide. That is a different answer on
        # every machine.
        context = corroboration._tls_context(None)
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_verification_is_still_on(self):
        context = corroboration._tls_context(None)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_a_run_with_no_ca_file_still_gets_a_context(self):
        # Without one urllib falls back to its own default, which carries the
        # same unpinned floor.
        veto = corroboration.ElasticsearchVeto(
            "https://es.invalid", "repo", corroboration.Credentials())
        self.assertIsNotNone(veto._context)


if __name__ == "__main__":
    unittest.main()
