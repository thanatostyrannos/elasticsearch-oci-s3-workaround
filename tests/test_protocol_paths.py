"""Which paths these two tools open, and which they refuse before opening.

Both files take directories and files from whoever runs the command:
`--out` and `--es-password-file` on the harness, `--out` on the packager.
A path is data. What is pinned here is that each one is checked and resolved
BEFORE it reaches the filesystem, and that the path checked is the path used,
so a refusal names the same place a write would have gone.

Also pinned here: the harness builds its subprocess commands as argument
lists and never hands a string to a shell. That is the property a shell
injection would have to break, so it is asserted rather than assumed.
"""

import ast
import os
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import package
import reclaim_test_protocol as harness

FILE_ROOT_ENV_VAR = "GENCHAIN_FILE_ROOT"
SECRET = "correct-horse-battery-staple"
FLAG = "--es-password-file"


class TheSecretFilePathIsCheckedBeforeAnythingIsOpened(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="secret-")
        self.addCleanup(__import__("shutil").rmtree, self.directory,
                        ignore_errors=True)
        self.secret_file = os.path.join(self.directory, "password")
        with open(self.secret_file, "w") as handle:
            handle.write(SECRET + "\n")

    def _refusal(self, path):
        with self.assertRaises(ValueError) as raised:
            harness.read_secret_file(path, FLAG)
        return str(raised.exception)

    def test_an_empty_path_is_refused(self):
        self.assertIn("empty path", self._refusal(""))

    def test_a_whitespace_only_path_is_refused(self):
        self.assertIn("empty path", self._refusal("   "))

    def test_a_path_holding_a_nul_byte_is_refused(self):
        self.assertIn("NUL byte", self._refusal("/tmp/pass\0word"))

    def test_the_refusal_names_the_flag_that_carried_the_path(self):
        self.assertIn(FLAG, self._refusal(""))

    def test_a_directory_is_refused_rather_than_raising_a_traceback(self):
        self.assertIn("IsADirectoryError", self._refusal(self.directory))

    def test_a_refusal_never_quotes_the_contents(self):
        self.assertNotIn(SECRET, self._refusal(self.directory))

    def test_the_secret_is_read_through_a_symlink(self):
        link = os.path.join(self.directory, "link-to-password")
        os.symlink(self.secret_file, link)
        self.assertEqual(harness.read_secret_file(link, FLAG), SECRET)

    def test_a_path_outside_the_confining_root_is_refused(self):
        confined = tempfile.mkdtemp(prefix="confined-")
        self.addCleanup(__import__("shutil").rmtree, confined,
                        ignore_errors=True)
        os.environ[FILE_ROOT_ENV_VAR] = confined
        self.addCleanup(os.environ.pop, FILE_ROOT_ENV_VAR, None)
        self.assertIn("outside", self._refusal(self.secret_file))

    def test_a_path_inside_the_confining_root_is_still_read(self):
        os.environ[FILE_ROOT_ENV_VAR] = self.directory
        self.addCleanup(os.environ.pop, FILE_ROOT_ENV_VAR, None)
        self.assertEqual(harness.read_secret_file(self.secret_file, FLAG),
                         SECRET)


def run_harness(argv):
    """The harness run to completion, with its two streams joined."""
    done = subprocess.run([sys.executable, "reclaim_test_protocol.py"] + argv,
                          cwd=ROOT, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=60)
    return done.returncode, done.stdout


def enough_arguments_to_reach_the_path_checks(out):
    """Every required flag, set to values no cycle will ever get to use."""
    return ["--mode", "metadata", "--endpoint", "https://example.invalid",
            "--region", "us-somewhere-1", "--bucket", "b", "--prefix", "p",
            "--credentials", "/nonexistent/credentials.json", "--out", out]


class TheHarnessRefusesABadPathAtTheCommandLine(unittest.TestCase):
    def test_an_empty_out_is_refused(self):
        argv = enough_arguments_to_reach_the_path_checks("")
        code, output = run_harness(argv)
        self.assertIn("--out", output)
        self.assertEqual(code, 2)

    def test_a_whitespace_only_secret_file_path_is_refused(self):
        # An empty value is argparse's own "not given"; a blank one is a path
        # the operator meant, and it names nothing.
        argv = enough_arguments_to_reach_the_path_checks("")
        code, output = run_harness(argv + [FLAG, " "])
        self.assertIn(FLAG, output)
        self.assertEqual(code, 2)


class TheOutDirectoryIsResolvedBeforeItIsCreated(unittest.TestCase):
    """The check has to run first or it checks a path the write did not use."""

    def setUp(self):
        with open(os.path.join(ROOT, "reclaim_test_protocol.py")) as handle:
            self.source = handle.read()

    def test_the_check_comes_before_the_directory_is_made(self):
        self.assertLess(self.source.index('checked_path(args.out'),
                        self.source.index('os.makedirs(args.out'))


class TheHarnessNeverHandsACommandToAShell(unittest.TestCase):
    """Evidence for the shell-injection question, asserted rather than argued.

    Nothing an operator types is interpolated into a command STRING. Each
    value is one element of an argument list, and the list is handed straight
    to `subprocess.run` with no shell, so a value holding `;` or `$(...)` is
    an argument that means nothing rather than a command that runs.
    """

    def setUp(self):
        self.args = types.SimpleNamespace(
            transport="s3", endpoint="https://example.invalid",
            region="us-somewhere-1", namespace="", bucket="b; rm -rf /",
            prefix="$(whoami)", credentials="/tmp/creds.json",
            elasticsearch="", repository="")

    def _subprocess_calls(self):
        with open(os.path.join(ROOT, "reclaim_test_protocol.py")) as handle:
            tree = ast.parse(handle.read())
        return [node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"]

    def test_no_subprocess_call_asks_for_a_shell(self):
        asked = [call for call in self._subprocess_calls()
                 for keyword in call.keywords if keyword.arg == "shell"]
        self.assertEqual(asked, [])

    def test_every_subprocess_call_passes_a_command_it_was_handed(self):
        names = [call.args[0].id for call in self._subprocess_calls()]
        self.assertEqual(names, ["cmd"])

    def test_the_command_starts_with_this_interpreter(self):
        command = harness.reclaim_command(self.args, "/tmp/manifest.tsv")
        self.assertEqual(command[0], sys.executable)

    def test_a_value_holding_shell_metacharacters_stays_one_argument(self):
        command = harness.reclaim_command(self.args, "/tmp/manifest.tsv")
        self.assertIn("b; rm -rf /", command)

    def test_no_argument_is_a_run_of_words_a_shell_would_have_split(self):
        command = harness.reclaim_command(self.args, "/tmp/manifest.tsv")
        self.assertEqual(command.count("$(whoami)"), 1)


class TheReleaseDirectoryIsCheckedBeforeItIsCreated(unittest.TestCase):
    def _refusal(self, destination):
        with self.assertRaises(package.ReleaseRefused) as raised:
            package.checked_directory(destination, "--out")
        return str(raised.exception)

    def test_an_empty_destination_is_refused(self):
        self.assertIn("empty path", self._refusal(""))

    def test_a_whitespace_only_destination_is_refused(self):
        self.assertIn("empty path", self._refusal("   "))

    def test_a_destination_holding_a_nul_byte_is_refused(self):
        self.assertIn("NUL byte", self._refusal("/tmp/dist\0evil"))

    def test_the_refusal_names_the_flag_that_carried_the_path(self):
        self.assertIn("--out", self._refusal(""))

    def test_a_home_relative_destination_is_expanded(self):
        self.assertEqual(package.checked_directory("~", "--out"),
                         os.path.realpath(os.path.expanduser("~")))


class TheArchiveLandsInTheResolvedDirectory(unittest.TestCase):
    """Built once, through a symlink, with the audit's confinement root set.

    The root belongs to the audit's own file access. The packager is not the
    audit, so a root set for one must not decide where a release lands, and a
    build into a temporary directory has to keep working with it set.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="release-paths-")
        cls.real = os.path.join(cls.tmp, "real-out")
        os.makedirs(cls.real)
        link = os.path.join(cls.tmp, "link-out")
        os.symlink(cls.real, link)
        os.environ[FILE_ROOT_ENV_VAR] = os.path.join(cls.tmp, "somewhere-else")
        try:
            cls.archive = package.build(link)
        finally:
            os.environ.pop(FILE_ROOT_ENV_VAR, None)

    @classmethod
    def tearDownClass(cls):
        __import__("shutil").rmtree(cls.tmp, ignore_errors=True)

    def test_the_archive_is_written_to_the_resolved_directory(self):
        self.assertEqual(os.path.dirname(self.archive),
                         os.path.realpath(self.real))

    def test_the_audit_confinement_root_does_not_reach_the_build(self):
        with zipfile.ZipFile(self.archive) as archive:
            self.assertTrue(archive.namelist())


if __name__ == "__main__":
    unittest.main()
