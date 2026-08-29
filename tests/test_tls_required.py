"""TLS verification is not optional for the size report or the restore check.

Both tools used to take `--insecure`, scoped to lab addresses. Scoping it was
not enough: the branch that turned verification off still existed, and a
branch that exists gets reached. The flag is gone. A lab cluster serving a
certificate it signed itself is reached with `--ca-cert`, which verifies the
connection instead of abandoning it.

That trade only holds if `--ca-cert` is pleasant to use, because a flag that
is painful gets patched back out. So these tests cover both halves: the
removed behaviour stays removed, and the replacement refuses clearly, names
the path it tried, and tells the operator where an ECK cluster keeps its CA.
"""

import argparse
import ast
import contextlib
import io
import os
import ssl
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import snapshot_sizes as sizes

ECK_CA_SECRET = "-es-http-certs-public"

# Somewhere nothing is listening, so a run that gets past argument checking
# fails on the connection rather than reaching a real cluster.
CLOSED_LOOPBACK = "https://127.0.0.1:1"


def load_verify_restorable_helpers():
    """The helper half of verify_restorable.py, without running the script.

    The file parses its arguments at import time, which is right for a script
    and unusable from a test, so only the definitions above that point are
    executed here.
    """
    with open(os.path.join(ROOT, "verify_restorable.py")) as handle:
        source = handle.read()
    tree = ast.parse(source)
    cut = next(node.lineno for node in tree.body
               if isinstance(node, ast.Assign)
               and getattr(node.targets[0], "id", "") == "_p")
    namespace = {}
    exec(compile("\n".join(source.split("\n")[:cut - 1]),  # nosec B102
                 "verify_restorable.py", "exec"), namespace)
    return namespace


VERIFY = load_verify_restorable_helpers()


def run_tool(argv):
    """One tool, run to completion, with its two streams joined."""
    done = subprocess.run([sys.executable] + argv, cwd=ROOT, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=60)
    return done.returncode, done.stdout


class ToolInvocation:
    """A valid command line for one tool, with pieces swapped in per test."""

    def __init__(self, script, fixed):
        self.script = script
        self.fixed = fixed

    def run(self, *extra):
        return run_tool([self.script] + self.fixed + list(extra))


class ToolsUnderTest(unittest.TestCase):
    """Both tools, run as real subprocesses the way an operator runs them."""

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        password_file = os.path.join(self.workspace.name, "pw.txt")
        with open(password_file, "w") as handle:
            handle.write("not-a-real-password\n")
        self.tools = (
            ToolInvocation("snapshot_sizes.py",
                           ["--es", CLOSED_LOOPBACK, "--repo", "backups"]),
            ToolInvocation("verify_restorable.py",
                           ["--elasticsearch", CLOSED_LOOPBACK,
                            "--repository", "backups",
                            "--password-file", password_file]),
        )

    def real_ca_file(self):
        """A PEM the ssl module will actually load, written to a temp file.

        Taken from the machine's own trust store rather than committed here,
        because a certificate in a repository expires and starts failing this
        test for a reason that has nothing to do with the code.
        """
        certs = ssl.create_default_context().get_ca_certs(binary_form=True)
        if not certs:
            self.skipTest("no system trust store to borrow a certificate from")
        path = os.path.join(self.workspace.name, "ca.crt")
        with open(path, "w") as handle:
            handle.write(ssl.DER_cert_to_PEM_cert(certs[0]))
        return path


class TheFlagIsGone(ToolsUnderTest):

    def test_neither_tool_accepts_insecure(self):
        for tool in self.tools:
            with self.subTest(tool=tool.script):
                code, out = tool.run("--insecure")
                self.assertNotEqual(code, 0)
                self.assertIn("unrecognized arguments: --insecure", out)

    def test_neither_tool_still_carries_a_lab_host_exemption(self):
        # The exemption existed only to decide where --insecure was allowed.
        # Left behind it reads as a door waiting to be reopened.
        self.assertFalse(hasattr(sizes, "is_lab_host"))
        self.assertNotIn("is_lab_host", VERIFY)


class TheContextVerifies(unittest.TestCase):
    """Built once per run, and the same settings carry every request."""

    def context(self):
        return sizes.tls_context(
            argparse.Namespace(es=CLOSED_LOOPBACK, ca_cert=None))

    def test_the_certificate_is_validated(self):
        self.assertEqual(self.context().verify_mode, ssl.CERT_REQUIRED)

    def test_the_hostname_is_checked(self):
        self.assertTrue(self.context().check_hostname)

    def test_the_protocol_floor_is_pinned(self):
        # create_default_context leaves this at MINIMUM_SUPPORTED before
        # Python 3.10, which lets the host's OpenSSL build decide. That is a
        # different answer on every machine the tool runs on.
        self.assertEqual(self.context().minimum_version,
                         ssl.TLSVersion.TLSv1_2)


class TheReplacementRefusesClearly(ToolsUnderTest):

    def test_a_missing_ca_file_is_refused_by_both_tools(self):
        missing = os.path.join(self.workspace.name, "absent.pem")
        for tool in self.tools:
            with self.subTest(tool=tool.script):
                code, _out = tool.run("--ca-cert", missing)
                self.assertNotEqual(code, 0)

    def test_the_refusal_names_the_path_that_failed(self):
        # Two flags here take a path. A message that does not say which file
        # it could not open costs an hour of guessing.
        missing = os.path.join(self.workspace.name, "absent.pem")
        for tool in self.tools:
            with self.subTest(tool=tool.script):
                _code, out = tool.run("--ca-cert", missing)
                self.assertIn(missing, out)

    def test_a_ca_file_that_is_a_directory_is_refused(self):
        for tool in self.tools:
            with self.subTest(tool=tool.script):
                code, _out = tool.run("--ca-cert", self.workspace.name)
                self.assertNotEqual(code, 0)

    def test_a_ca_file_that_is_not_a_certificate_is_refused(self):
        # An empty or truncated file loads as nothing, and verification then
        # fails at the first connection as a cluster that looks broken.
        junk = os.path.join(self.workspace.name, "junk.pem")
        with open(junk, "w") as handle:
            handle.write("this is not a certificate\n")
        for tool in self.tools:
            with self.subTest(tool=tool.script):
                code, _out = tool.run("--ca-cert", junk)
                self.assertNotEqual(code, 0)

    def test_the_refusal_says_where_an_eck_cluster_keeps_its_ca(self):
        missing = os.path.join(self.workspace.name, "absent.pem")
        for tool in self.tools:
            with self.subTest(tool=tool.script):
                _code, out = tool.run("--ca-cert", missing)
                self.assertIn(ECK_CA_SECRET, out)

    def test_the_help_says_where_an_eck_cluster_keeps_its_ca(self):
        # The other half of not being painful: the answer is in --help, so
        # nobody has to fail once to find out where to look.
        for tool in self.tools:
            with self.subTest(tool=tool.script):
                _code, out = tool.run("--help")
                self.assertIn(ECK_CA_SECRET, out)

    def test_a_real_ca_file_gets_past_argument_checking(self):
        # The half that matters more: a legitimate invocation is not made
        # harder by any of the above. Nothing is listening on the port, so
        # the run still fails, further down and for a different reason.
        ca_file = self.real_ca_file()
        for tool in self.tools:
            with self.subTest(tool=tool.script):
                _code, out = tool.run("--ca-cert", ca_file)
                self.assertNotIn("could not be read", out)


class NamesCannotAimTheRequestSomewhereElse(unittest.TestCase):
    """A repository name is typed on the command line, then lands in a path."""

    def test_a_slash_in_a_name_stays_one_path_segment(self):
        self.assertEqual(sizes.path_segment("backups/../_cluster"),
                         "backups%2F..%2F_cluster")

    def test_a_question_mark_in_a_name_cannot_start_a_query(self):
        self.assertEqual(sizes.path_segment("backups?pretty"),
                         "backups%3Fpretty")

    def test_an_ordinary_name_is_unchanged(self):
        self.assertEqual(sizes.path_segment("daily-backups"), "daily-backups")

    def test_the_restore_check_encodes_names_the_same_way(self):
        self.assertEqual(VERIFY["path_segment"]("backups/../_cluster"),
                         sizes.path_segment("backups/../_cluster"))


class TheEndpointIsFixedOnce(unittest.TestCase):
    """Every request is built on what --es passed, and on nothing else."""

    def parse(self, endpoint):
        parser = sizes.build_parser()
        return sizes.checked_endpoint(parser, endpoint)

    def test_a_trailing_slash_is_dropped(self):
        self.assertEqual(self.parse("https://es.invalid:9200/"),
                         "https://es.invalid:9200")

    def test_a_query_string_typed_into_the_endpoint_is_dropped(self):
        # Left in place it would reappear in the middle of every request
        # path, ahead of the filter_path the code thinks it is sending.
        self.assertEqual(self.parse("https://es.invalid:9200/?pretty"),
                         "https://es.invalid:9200")

    def test_a_fragment_typed_into_the_endpoint_is_dropped(self):
        self.assertEqual(self.parse("https://es.invalid:9200/#frag"),
                         "https://es.invalid:9200")

    def refuse(self, endpoint):
        """The parser prints usage to stderr on its way out; this test is
        about the exit, not about what the terminal saw."""
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse(endpoint)

    def test_an_endpoint_naming_no_host_is_refused(self):
        self.refuse("https:///")

    def test_a_file_endpoint_is_refused(self):
        self.refuse("file:///etc/passwd")


class TheSecretFileIsCheckedBeforeItIsRead(unittest.TestCase):
    """--password-file is opened by its resolved name, so the file that was
    checked is the file that is read."""

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)

    def read(self, path):
        return VERIFY["read_secret"](path, "--password-file")

    def test_a_directory_is_refused(self):
        with self.assertRaises(SystemExit):
            self.read(self.workspace.name)

    def test_the_refusal_names_the_flag(self):
        with self.assertRaises(SystemExit) as raised:
            self.read(self.workspace.name)
        self.assertIn("--password-file", str(raised.exception))

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(SystemExit):
            self.read(os.path.join(self.workspace.name, "absent.txt"))

    def test_the_refusal_never_carries_the_contents(self):
        secret = os.path.join(self.workspace.name, "pw.txt")
        with open(secret, "w") as handle:
            handle.write("hunter2\n")
        os.chmod(secret, 0)
        self.addCleanup(os.chmod, secret, 0o600)
        try:
            with self.assertRaises(SystemExit) as raised:
                self.read(secret)
        except AssertionError:
            self.skipTest("this user can read a mode-000 file")
        self.assertNotIn("hunter2", str(raised.exception))

    def test_a_symlink_is_read_through_to_its_target(self):
        target = os.path.join(self.workspace.name, "real.txt")
        with open(target, "w") as handle:
            handle.write("  secret-value\n")
        link = os.path.join(self.workspace.name, "link.txt")
        os.symlink(target, link)
        self.assertEqual(self.read(link), "secret-value")

    def test_a_dangling_symlink_is_refused(self):
        link = os.path.join(self.workspace.name, "dangling.txt")
        os.symlink(os.path.join(self.workspace.name, "gone.txt"), link)
        with self.assertRaises(SystemExit):
            self.read(link)

    def test_an_ordinary_file_still_gives_its_one_line(self):
        target = os.path.join(self.workspace.name, "pw.txt")
        with open(target, "w") as handle:
            handle.write("  secret-value\n")
        self.assertEqual(self.read(target), "secret-value")


if __name__ == "__main__":
    unittest.main()
