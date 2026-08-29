"""Where TLS verification may be turned off, and where it may not.

No tool here has a flag that turns verification off. Verification is on for
every run, and a lab cluster serving a certificate it signed itself is
reached by naming the CA that signed it with `--ca-cert`. What replaced the
flag is pinned in test_tls_required.py for `snapshot_sizes.py` and
`verify_restorable.py`, and in test_churn_rig_tls.py for
`snapshot_churn_rig.py`.

What is left here is the shared ground: the lab-address rule the churn rig
uses to decide whose failed connection is worth a hint about the ECK CA, and
the standing guarantee that the S3 client never gets a relaxed context. The
store the rig reaches is the real Oracle endpoint on a live run, so nothing
about the cluster's TLS settings may touch it.
"""

import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import package
import reclaim_test_protocol as protocol
import snapshot_churn_rig as rig
import snapshot_sizes as sizes

LAB_ADDRESSES = [
    "localhost",
    "127.0.0.1",
    "127.0.0.53",
    "::1",
    "10.4.1.9",
    "172.16.0.1",
    "192.168.1.10",
    "169.254.10.1",
    "fd00::1",
    "rig-es-http.rig.svc",
    "rig-es-http.rig.svc.cluster.local",
    "elasticsearch",
    "es.lab.local",
    "es.internal",
]

ROUTABLE_ADDRESSES = [
    "cluster.example.com",
    "es.acme.co.uk",
    "8.8.8.8",
    "2001:4860:4860::8888",
    "namespace.compat.objectstorage.uk-london-1.oraclecloud.com",
]


HOST_RULES = {
    "snapshot_churn_rig": rig.is_lab_host,
}


class LabAddressesAreRecognised(unittest.TestCase):
    def test_every_lab_address_is_a_lab_host(self):
        for name, rule in HOST_RULES.items():
            for host in LAB_ADDRESSES:
                with self.subTest(tool=name, host=host):
                    self.assertTrue(rule(host))

    def test_a_trailing_root_dot_does_not_change_the_answer(self):
        for name, rule in HOST_RULES.items():
            with self.subTest(tool=name):
                self.assertTrue(rule("rig-es-http.rig.svc."))


class RoutableAddressesAreRefused(unittest.TestCase):
    def test_every_routable_address_is_not_a_lab_host(self):
        for name, rule in HOST_RULES.items():
            for host in ROUTABLE_ADDRESSES:
                with self.subTest(tool=name, host=host):
                    self.assertFalse(rule(host))

    def test_a_missing_host_is_not_a_lab_host(self):
        for name, rule in HOST_RULES.items():
            with self.subTest(tool=name):
                self.assertFalse(rule(None))

    def test_an_empty_host_is_not_a_lab_host(self):
        for name, rule in HOST_RULES.items():
            with self.subTest(tool=name):
                self.assertFalse(rule(""))


def run_tool(argv):
    """One tool, run to completion, with its two streams joined."""
    done = subprocess.run([sys.executable] + argv, cwd=ROOT, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=60)
    return done.returncode, done.stdout


class VerificationIsOnWhenNobodyAsked(unittest.TestCase):
    def test_the_default_context_verifies(self):
        import ssl
        made = rig.Es("https://cluster.example.com", "elastic", "p", None)
        self.assertEqual(made.ctx.verify_mode, ssl.CERT_REQUIRED)

    def test_the_size_report_builds_no_context_for_plain_http(self):
        import argparse
        args = argparse.Namespace(es="http://127.0.0.1:9200",
                                  ca_cert=None)
        self.assertIsNone(sizes.tls_context(args))


class TheObjectStoreClientNeverRelaxes(unittest.TestCase):
    """--ca-cert names the cluster's CA. The store the rig reaches is the
    real Oracle endpoint on a live run, and it verifies against the system
    trust store, so no cluster TLS setting may reach it."""

    def test_the_s3_client_takes_no_tls_context(self):
        made = rig.S3("https://s3.example.com", "us-east-1", "ak", "sk", "b")
        self.assertFalse(hasattr(made, "ctx"))

    def test_the_s3_client_passes_no_context_to_urlopen(self):
        with open(os.path.join(ROOT, "snapshot_churn_rig.py")) as handle:
            source = handle.read()
        body = source[source.index("class S3:"):source.index("def make_s3(")]
        self.assertIn("urlopen(r, timeout=60)", body)
        self.assertNotIn("context=", body)


class TheSizeReportCannotBeAimedElsewhere(unittest.TestCase):
    """http_get takes a path, not a URL, so a caller cannot move the request
    to a host the operator did not name."""

    def test_the_request_url_starts_at_the_named_endpoint(self):
        import argparse
        seen = {}
        args = argparse.Namespace(es="https://127.0.0.1:9200", user=None,
                                  api_key=None, ca_cert=None, tls=None)

        class Answer:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *_):
                return False

            def read(self_inner):
                return b"{}"

        def fake_urlopen(req, **_kwargs):
            seen["url"] = req.full_url
            return Answer()

        original = sizes.urllib.request.urlopen
        sizes.urllib.request.urlopen = fake_urlopen
        try:
            sizes.http_get("/_snapshot/backups", args)
        finally:
            sizes.urllib.request.urlopen = original
        self.assertEqual(seen["url"],
                         "https://127.0.0.1:9200/_snapshot/backups")

    def test_a_non_http_endpoint_is_refused(self):
        code, out = run_tool(["snapshot_sizes.py", "--es", "file:///etc/passwd",
                              "--repo", "backups"])
        self.assertNotEqual(code, 0)
        self.assertIn("only http and https", out)


class ReleaseNamesStayInsideTheOutputDirectory(unittest.TestCase):
    """--version is a path component twice over: part of the archive's name,
    and the directory every member unpacks into."""

    def test_a_traversing_version_is_refused(self):
        with self.assertRaises(package.ReleaseRefused):
            package.release_stem("../../evil")

    def test_a_separator_in_a_version_is_refused(self):
        with self.assertRaises(package.ReleaseRefused):
            package.release_stem("1.0/etc")

    def test_an_ordinary_version_is_accepted(self):
        self.assertTrue(package.release_stem("1.2.3-rc1").endswith("1.2.3-rc1"))

    def test_no_version_leaves_the_plain_name(self):
        self.assertEqual(package.release_stem(None), package.NAME)

    def test_an_archive_outside_the_output_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(package.ReleaseRefused):
                package.archive_path(out, os.path.join("..", "escaped"))


class RunArtifactsStayInsideTheOutputDirectory(unittest.TestCase):
    def test_an_ordinary_artifact_resolves_under_out(self):
        with tempfile.TemporaryDirectory() as out:
            self.assertEqual(protocol.artifact(out, "derive-1.txt"),
                             os.path.join(os.path.realpath(out),
                                          "derive-1.txt"))

    def test_an_escaping_name_is_refused(self):
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(ValueError):
                protocol.artifact(out, os.path.join("..", "cycles.tsv"))


class TheReclaimableLineIsStillRead(unittest.TestCase):
    """The pattern was rewritten so its two halves cannot both match the same
    spaces. It still has to read the line the audit actually writes."""

    def test_it_reads_the_indented_line_under_the_heading(self):
        report = ("Dispositions\n  ...\n\nReclaimable\n"
                  "  12.5 GiB across 4210 orphaned objects\n")
        found = protocol.RECLAIMABLE.search(report)
        self.assertEqual(found.group(1),
                         "12.5 GiB across 4210 orphaned objects")

    def test_it_does_not_reach_past_a_blank_line(self):
        report = "Reclaimable\n\n  something further down\n"
        self.assertIsNone(protocol.RECLAIMABLE.search(report))


class SecretFilesRefuseInsteadOfCrashing(unittest.TestCase):
    def test_the_harness_names_the_flag_and_the_path(self):
        with self.assertRaises(ValueError) as raised:
            protocol.read_secret_file("/nonexistent/pw", "--es-password-file")
        self.assertIn("--es-password-file", str(raised.exception))

    def test_the_message_does_not_carry_the_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError) as raised:
                protocol.read_secret_file(directory, "--es-password-file")
        self.assertIn("--es-password-file", str(raised.exception))

    def test_a_readable_file_gives_its_one_line(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt") as handle:
            handle.write("  secret-value\n")
            handle.flush()
            self.assertEqual(
                protocol.read_secret_file(handle.name, "--es-password-file"),
                "secret-value")


if __name__ == "__main__":
    unittest.main()
