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
import re
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import package


class TheReleaseCarriesWhatAnOperatorNeeds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="release-")
        cls.archive = package.build(cls.tmp)
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

    def test_the_loop_runner_ships(self):
        # Whoever runs this has a shell and may have nothing else. A test
        # procedure that only an agent can follow is not a test procedure.
        self.assertTrue(self._member("scripts/run-test-cycle.sh"))
        self.assertTrue(self._member("scripts/test-cycle.conf.example"))

    def test_every_tool_the_docs_tell_you_to_run_ships(self):
        # A shipped document naming a file the release does not carry is a
        # broken instruction, and that happened: quickstart-test-rig.md walks
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

    def test_both_quickstarts_ship(self):
        self.assertTrue(self._member("docs/quickstart-read-only.md"))
        self.assertTrue(self._member("docs/quickstart-test-rig.md"))

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
        cls.archive = package.build(cls.tmp)

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
            first = package.build(one)
            second = package.build(two)
            self.assertEqual(hashlib.sha256(open(first, "rb").read()).hexdigest(),
                             hashlib.sha256(open(second, "rb").read()).hexdigest())

    def test_a_checksum_is_written_beside_the_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = package.build(tmp)
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
        # could carry a secret and nothing would notice.
        planted = os.path.join(ROOT, "generation_chain", "_leak_probe.py")
        with open(planted, "w") as fh:
            fh.write('SECRET = "%s"\n' % PEM_MARKERS[1].decode())
        self.addCleanup(os.remove, planted)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(package.ReleaseRefused) as raised:
                package.build(tmp)
        self.assertIn("_leak_probe.py", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
