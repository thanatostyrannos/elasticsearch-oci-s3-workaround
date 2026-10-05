"""What ships is not what the repository holds, and the difference is the point.

A user reclaiming a leaking snapshot repository needs the audit engine, the
delete path, the harness that exercises both, and the documentation. They do
not need the test suite, the captured evidence, the Terraform that stands up a
probe tenancy, or the load generator that manufactures churn in a lab.

That distinction is also the security boundary. Every secret-shaped string a
scanner finds in this repository lives in `tests/`, and none of it ships.
"""

import hashlib
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import package


def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True)


def committed_copy(parent):
    """A temporary git repo committing the working-tree copy of every
    tracked file, so the build sees what the developer is about to commit
    whether or not their edits are committed yet."""
    listing = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True,
                             capture_output=True).stdout
    repo = os.path.join(parent, "repo")
    os.makedirs(repo)
    for relative in filter(None, listing.decode().split("\0")):
        source = os.path.join(ROOT, relative)
        if not os.path.isfile(source):
            continue
        target = os.path.join(repo, relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy(source, target)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def build_from_copy(parent, out):
    """Build an archive from a committed copy of the working tree."""
    repo = committed_copy(parent)
    with mock.patch.object(package, "ROOT", repo):
        return package.build(out)


class TheReleaseCarriesWhatAnOperatorNeeds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="release-")
        cls.archive = build_from_copy(cls.tmp, os.path.join(cls.tmp, "out"))
        with zipfile.ZipFile(cls.archive) as zf:
            cls.names = set(zf.namelist())

    @classmethod
    def tearDownClass(cls):
        __import__("shutil").rmtree(cls.tmp, ignore_errors=True)

    def _member(self, suffix):
        return {n for n in self.names if n.endswith(suffix)}

    def test_the_audit_engine_ships(self):
        self.assertTrue(self._member("generation_chain/derivation/audit.py"))
        self.assertTrue(self._member("generation_chain/cli.py"))

    def test_both_entry_points_ship(self):
        # `python3 -m generation_chain` and `python3 -m generation_chain.reclaim`
        # are the two commands the documentation tells an operator to run.
        self.assertTrue(self._member("generation_chain/__main__.py"))
        self.assertTrue(self._member("generation_chain/reclaim/__main__.py"))

    def test_the_delete_path_ships_with_its_approval_gate(self):
        # Shipping the deleter without the gate would be worse than shipping
        # neither.
        self.assertTrue(self._member("generation_chain/reclaim/cli.py"))
        self.assertTrue(self._member("generation_chain/reclaim/approval.py"))

    def test_the_harness_ships(self):
        self.assertTrue(self._member("reclaim_test_protocol.py"))

    def test_the_restore_check_ships(self):
        # The only thing that turns "we did not break Elasticsearch" into a
        # number, so an operator should have it.
        self.assertTrue(self._member("verify_restorable.py"))

    def test_scripts_unpack_executable_and_nothing_else_does(self):
        # The docs run `./scripts/run-test-cycle.sh` straight out of the
        # unpacked archive. Stored as 0644 they fail with "Permission denied" before
        # doing anything. The mode comes from the content, a leading `#!`,
        # so two builds of one commit still produce the same bytes.
        with zipfile.ZipFile(self.archive) as zf:
            executable = set()
            for info in zf.infolist():
                mode = (info.external_attr >> 16) & 0o777
                want = 0o755 if zf.read(info).startswith(b"#!") else 0o644
                self.assertEqual(oct(mode), oct(want), info.filename)
                if mode == 0o755:
                    executable.add(info.filename.split("/", 1)[1])
        self.assertIn("scripts/run-test-cycle.sh", executable)

    def test_the_loop_runner_ships(self):
        # Whoever runs this has a shell and may have nothing else. A test
        # procedure that only an agent can follow is not a test procedure.
        self.assertTrue(self._member("scripts/run-test-cycle.sh"))
        self.assertTrue(self._member("scripts/test-cycle.conf.example"))

    def test_every_tool_the_docs_tell_you_to_run_ships(self):
        # A shipped document naming a file the release does not carry is a
        # broken instruction, and that happened: the test-rig guide walks
        # through snapshot_churn_rig.py, which was excluded as lab tooling.
        import re
        with zipfile.ZipFile(self.archive) as zf:
            shipped = {n.split("/", 1)[1] for n in zf.namelist()}
            docs = [n for n in zf.namelist() if n.endswith(".md")]
            named = set()
            for doc in docs:
                body = zf.read(doc).decode("utf-8", "replace")
                named.update(re.findall(r"python3 ([a-z_]+\.py)", body))
                named.update(re.findall(r"(scripts/[a-z-]+\.sh)", body))
                # `./name.sh` in a code block runs a file beside the document,
                # which is how the Oracle service request's reproducers are
                # invoked. The two patterns above never saw that form.
                here = posixpath.dirname(doc.split("/", 1)[1])
                for name in re.findall(r"(?m)^\s*\./([A-Za-z0-9_.-]+\.(?:sh|py))\b", body):
                    named.add(posixpath.normpath(posixpath.join(here, name)))
        missing = {n for n in named if n not in shipped}
        self.assertEqual(missing, set(),
                         "documents tell the reader to run these, and they "
                         "are not in the release: %s" % missing)

    def test_the_license_ships(self):
        self.assertTrue(self._member("LICENSE"))

    def test_the_security_assessment_and_its_scans_ship(self):
        # A report nobody can check against the output it was built from is an
        # assertion rather than evidence.
        self.assertTrue(self._member("docs/security/evaluation-report.md"))
        self.assertTrue(self._member("docs/security/asd-stig-assessment.md"))
        self.assertTrue(self._member("docs/security/what-we-need-from-you.md"))
        self.assertTrue({n for n in self.names if "/security/scans/" in n},
                        "the raw scan artifacts did not ship")

    def test_both_operator_guides_ship(self):
        self.assertTrue(self._member("docs/running-it.md"))
        self.assertTrue(self._member("docs/testing-guide.md"))

    def test_the_documentation_ships(self):
        self.assertTrue(self._member("README.md"))
        self.assertTrue(self._member("FACTS.md"))


class TheReleaseLeavesTheLabBehind(TheReleaseCarriesWhatAnOperatorNeeds):
    def _prefixed(self, part):
        return {n for n in self.names if f"/{part}/" in n or n.startswith(part + "/")}

    def test_no_test_suite_ships(self):
        self.assertEqual(self._prefixed("tests"), set())

    def test_no_captured_evidence_ships(self):
        # The one write-up worth handing to an operator, what Oracle's S3
        # Compatibility API actually does, now lives in docs/ and ships from
        # there. What is left under evidence/ is captured run output and
        # campaign notes about tools that no longer exist.
        self.assertEqual(self._prefixed("evidence"), set())

    def test_no_terraform_ships(self):
        # It provisions a tenancy, a user and a customer secret key. Nothing an
        # operator reclaiming their own repository should be handed.
        self.assertEqual(self._prefixed("terraform"), set())

    def test_no_cluster_manifests_ship(self):
        self.assertEqual(self._prefixed("manifests"), set())

    def test_the_signing_vector_key_does_not_ship(self):
        # The one real-format private key in the repository. It authenticates
        # nothing, but a key in a distributed archive is a key in a
        # distributed archive.
        self.assertEqual(self._member("genchain-oci-signing-vector.json"), set())

    def test_no_pem_or_key_material_ships(self):
        with zipfile.ZipFile(self.archive) as zf:
            for name in zf.namelist():
                with self.subTest(member=name):
                    body = zf.read(name)
                    for marker in PEM_MARKERS:
                        self.assertNotIn(marker, body)


# The tools this repository was built with are not part of what it does, and a
# vendor name in a shipped file invites a reader to wonder whether the tool is
# tied to that vendor. It is not: `generation_chain` is standard library only.
# Assembled from parts so this file can still hold the pattern it looks for,
# the way the credential scanner does.
# Assembled rather than written out, so this file does not trip
# tests/test_no_credentials_committed.py, which scans every tracked file for
# exactly this shape and does not exempt this one.
_PEM_HEAD = "-----BE" + "GIN "
_PEM_TAIL = "PRIV" + "ATE KEY-----"
PEM_MARKERS = tuple((_PEM_HEAD + kind + _PEM_TAIL).encode()
                    for kind in ("", "RSA ", "EC ", "OPENSSH "))

VENDOR_WORDS = ("cla" + "ude", "anthro" + "pic", "son" + "net", "op" + "us",
                "hai" + "ku", "fa" + "ble")
VENDOR = re.compile("|".join(r"\b%s\b" % w for w in VENDOR_WORDS), re.I)


class TheReleaseNamesNoVendor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="release-vendor-")
        cls.archive = build_from_copy(cls.tmp, os.path.join(cls.tmp, "out"))

    @classmethod
    def tearDownClass(cls):
        __import__("shutil").rmtree(cls.tmp, ignore_errors=True)

    def test_no_shipped_file_names_the_vendor(self):
        findings = []
        with zipfile.ZipFile(self.archive) as zf:
            for name in zf.namelist():
                body = zf.read(name).decode("utf-8", "replace")
                for match in VENDOR.finditer(body):
                    line = body[:match.start()].count("\n") + 1
                    findings.append("%s:%d: %s" % (name, line, match.group(0)))
        self.assertEqual(findings, [], "\n".join(findings))

    def test_the_pattern_catches_what_it_claims_to(self):
        # Without this the regex could be quietly wrong and the check above
        # would pass by matching nothing, which is how a guard becomes
        # decoration.
        for word in VENDOR_WORDS:
            with self.subTest(word=word):
                self.assertRegex("built with %s, apparently" % word, VENDOR)

    def test_the_scan_reads_the_archive_rather_than_reporting_zero(self):
        # The other half: an empty archive would also produce no findings.
        with zipfile.ZipFile(self.archive) as zf:
            self.assertGreater(len(zf.namelist()), 20)


class TheReleaseIsReproducible(unittest.TestCase):
    def test_two_builds_produce_identical_bytes(self):
        # A release you cannot rebuild bit for bit is a release whose hash
        # means nothing, and the hash is what a recipient checks.
        with tempfile.TemporaryDirectory() as one, \
                tempfile.TemporaryDirectory() as two:
            first = build_from_copy(one, os.path.join(one, "out"))
            second = build_from_copy(two, os.path.join(two, "out"))
            self.assertEqual(hashlib.sha256(open(first, "rb").read()).hexdigest(),
                             hashlib.sha256(open(second, "rb").read()).hexdigest())

    def test_a_checksum_is_written_beside_the_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = build_from_copy(tmp, os.path.join(tmp, "out"))
            checksum = archive + ".sha256"
            self.assertTrue(os.path.exists(checksum))
            recorded = open(checksum).read().split()[0]
            self.assertEqual(
                recorded,
                hashlib.sha256(open(archive, "rb").read()).hexdigest())


class TheReleaseRefusesToCarryACredential(unittest.TestCase):
    def test_credential_material_in_a_packaged_file_stops_the_build(self):
        # The gate that makes the exclusions above load bearing rather than
        # merely tidy. Without it, a future file added to the shipped set
        # could carry a secret and nothing would notice. The file is
        # committed, because an untracked one never reaches the scan.
        with tempfile.TemporaryDirectory() as tmp:
            repo = committed_copy(tmp)
            planted = os.path.join(repo, "generation_chain", "_leak_probe.py")
            with open(planted, "w") as fh:
                fh.write('SECRET = "%s"\n' % PEM_MARKERS[1].decode())
            _git(repo, "add", "generation_chain/_leak_probe.py")
            _git(repo, "commit", "-q", "-m", "leak")
            with mock.patch.object(package, "ROOT", repo), \
                    self.assertRaises(package.ReleaseRefused) as raised:
                package.build(os.path.join(tmp, "out"))
        self.assertIn("_leak_probe.py", str(raised.exception))


class EveryPrivateKeyLabelIsRefused(unittest.TestCase):
    """The gate itself, on bodies the test assembles independently."""

    def test_each_private_key_armour_stops_the_build(self):
        # ENCRYPTED is what openssl writes for a passphrase-protected PKCS#8
        # key, DSA and PGP are the older and the GnuPG forms. A gate that
        # knew only four labels shipped any of these, and a key in a release
        # archive is a key handed to everyone who downloads it.
        for kind in ("", "RSA ", "EC ", "OPENSSH ", "ENCRYPTED ", "DSA ",
                     "PGP "):
            body = (_PEM_HEAD + kind + _PEM_TAIL[:-5] + " BLOCK-----\n"
                    if kind == "PGP " else _PEM_HEAD + kind + _PEM_TAIL)
            with self.subTest(kind=kind):
                with self.assertRaises(package.ReleaseRefused):
                    package._refuse_credentials("x.py", body.encode())

    def test_a_label_without_exact_armour_is_still_refused(self):
        # A key pasted into a document with its dashes turned into en
        # dashes still carries the key. The gate matches the label, not
        # the punctuation around it.
        body = "\u2013\u2013 BE" + "GIN RSA PRIV" + "ATE KEY \u2013\u2013\nMIIE"
        with self.assertRaises(package.ReleaseRefused):
            package._refuse_credentials("doc.md", body.encode())

    def test_ordinary_text_about_keys_ships(self):
        # The counterpart: the shipped docs talk about private keys in
        # prose. A gate that refused the words would refuse every release.
        package._refuse_credentials(
            "doc.md", b"Keep the private key out of the repository.")


class TheReleaseReflectsACommit(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = committed_copy(self._tmp.name)
        patcher = mock.patch.object(package, "ROOT", self.repo)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _build_names(self):
        archive = package.build(os.path.join(self._tmp.name, "out"))
        with zipfile.ZipFile(archive) as zf:
            return set(zf.namelist())

    def test_a_clean_commit_builds(self):
        # Guards the CI path: a fresh checkout is a clean tree and must still
        # produce an archive, or no release can ever be cut.
        self.assertTrue(any(n.endswith("README.md")
                            for n in self._build_names()))

    def test_an_untracked_file_under_a_shipped_directory_stays_out(self):
        # A local creds.json or scratch note beside tracked code used to be
        # swept into the archive by a directory walk, and then published.
        planted = os.path.join(self.repo, "generation_chain", "creds.json")
        with open(planted, "w") as fh:
            fh.write('{"api_key": "not-for-release"}')
        scratch = os.path.join(self.repo, "docs", "scratch.json")
        with open(scratch, "w") as fh:
            fh.write("{}")
        names = self._build_names()
        self.assertFalse([n for n in names if n.endswith("creds.json")])
        self.assertFalse([n for n in names if n.endswith("scratch.json")])

    def test_an_untracked_python_file_stays_out_too(self):
        # Extension filters alone let a stray .py through; only the tracked
        # check keeps an unreviewed script out of the release.
        with open(os.path.join(self.repo, "generation_chain", "stray.py"),
                  "w") as fh:
            fh.write("print('unreviewed')\n")
        self.assertFalse([n for n in self._build_names()
                          if n.endswith("stray.py")])

    def test_a_modified_tracked_file_refuses_the_build(self):
        # An archive hashed and attested as commit X must not carry edits
        # that exist in no commit.
        with open(os.path.join(self.repo, "FACTS.md"), "a") as fh:
            fh.write("edited after the commit\n")
        with self.assertRaises(package.ReleaseRefused) as raised:
            package.build(os.path.join(self._tmp.name, "out"))
        self.assertIn("FACTS.md", str(raised.exception))

    def test_the_shippable_list_ignores_uncommitted_edits(self):
        # The doc checks ask which files ship, not whether the tree matches
        # a commit. If this listing refused a dirty tree, every contributor
        # who runs the suite before committing, as CONTRIBUTING requires,
        # would see those checks error on their own edits.
        with open(os.path.join(self.repo, "FACTS.md"), "a") as fh:
            fh.write("edited after the commit\n")
        self.assertIn("FACTS.md", package.shippable())

    def test_members_still_refuses_what_shippable_allows(self):
        # Abuse case: the release path must not pick up shippable()'s
        # leniency. members() feeds the archive, so it alone refuses.
        with open(os.path.join(self.repo, "FACTS.md"), "a") as fh:
            fh.write("edited after the commit\n")
        with self.assertRaises(package.ReleaseRefused):
            package.members()

    def test_a_deleted_tracked_file_refuses_the_build(self):
        # A missing file would otherwise surface as a bare OSError, or ship
        # a release that differs from the commit.
        os.remove(os.path.join(self.repo, "FACTS.md"))
        with self.assertRaises(package.ReleaseRefused):
            package.build(os.path.join(self._tmp.name, "out"))

    def test_a_staged_but_uncommitted_change_refuses_the_build(self):
        # Staged is still not committed.
        with open(os.path.join(self.repo, "LICENSE"), "a") as fh:
            fh.write("x\n")
        _git(self.repo, "add", "LICENSE")
        with self.assertRaises(package.ReleaseRefused):
            package.build(os.path.join(self._tmp.name, "out"))

    def test_a_modified_file_that_does_not_ship_does_not_block(self):
        # Only what ships has to match the commit; a developer's edited test
        # must not stop a release build.
        outside = os.path.join(self.repo, "notes.txt")
        with open(outside, "w") as fh:
            fh.write("a")
        _git(self.repo, "add", "notes.txt")
        _git(self.repo, "commit", "-q", "-m", "notes")
        with open(outside, "w") as fh:
            fh.write("b")
        self.assertTrue(self._build_names())

    def test_a_named_file_that_is_untracked_refuses_the_build(self):
        # A file listed in PACKAGED_FILES that exists only on disk would ship
        # uncommitted content under a name the release promises.
        _git(self.repo, "rm", "-q", "--cached", "LICENSE")
        _git(self.repo, "commit", "-q", "-m", "untrack")
        with self.assertRaises(package.ReleaseRefused):
            package.build(os.path.join(self._tmp.name, "out"))

    def test_outside_a_git_work_tree_the_build_refuses(self):
        # With no commit to reflect, the archive cannot be tied to one.
        with tempfile.TemporaryDirectory() as bare:
            for relative in package.members():
                target = os.path.join(bare, relative)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy(os.path.join(self.repo, relative), target)
            with mock.patch.object(package, "ROOT", bare), \
                    mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES":
                                                 os.path.dirname(bare)}):
                with self.assertRaises(package.ReleaseRefused) as raised:
                    package.build(os.path.join(self._tmp.name, "out"))
        self.assertIn("git", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
