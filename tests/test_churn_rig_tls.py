"""What snapshot_churn_rig.py does instead of skipping TLS verification.

The rig drives a lab Elasticsearch cluster and, on a live run, the real
object store. It has no flag that turns certificate checking off. A cluster
under ECK serves a certificate signed by a CA that lives only in the
cluster, and the way to reach it is to name that CA with --ca-cert, so these
tests pin three things: that the context the rig builds always verifies and
never offers TLS 1.0 or 1.1, that --ca-cert refuses clearly rather than
raising from inside ssl, and that the help an operator reads says where the
CA comes from.

They also pin the two boundaries that make the endpoint safe to build URLs
from. Every request path is written in the rig, and --es contributes a
scheme and a host and nothing else, so a value with a path in it is refused
at the flag rather than silently prefixed onto every later request. And
every file the rig opens is resolved first, with the resolved path being the
one it opens, so the file that was checked is the file that is used.
"""

import argparse
import inspect
import os
import ssl
import subprocess
import sys
import tempfile
import unittest
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import snapshot_churn_rig as rig

RIG = os.path.join(ROOT, "snapshot_churn_rig.py")


def run_rig(argv):
    """The rig, run to completion, with its two streams joined."""
    done = subprocess.run([sys.executable, RIG] + argv, cwd=ROOT, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=60)
    return done.returncode, done.stdout


class TheClusterContextAlwaysVerifies(unittest.TestCase):
    def test_a_certificate_is_required(self):
        context = rig.tls_context("https://es.lab.local:9200", None)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_the_hostname_is_checked(self):
        context = rig.tls_context("https://es.lab.local:9200", None)
        self.assertTrue(context.check_hostname)

    def test_plain_http_builds_no_context(self):
        self.assertIsNone(rig.tls_context("http://127.0.0.1:9200", None))

    def test_the_client_carries_the_same_context(self):
        made = rig.Es("https://es.lab.local:9200", "elastic", "p", None)
        self.assertEqual(made.ctx.verify_mode, ssl.CERT_REQUIRED)


class TlsOneDotZeroAndOneAreRefused(unittest.TestCase):
    """Python before 3.10 still offers them, and the rig runs on a 3.9
    floor, so the floor is set rather than inherited."""

    def test_the_floor_is_tls_one_two(self):
        context = rig.tls_context("https://es.lab.local:9200", None)
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_the_ceiling_is_left_where_openssl_put_it(self):
        # A cluster that speaks only 1.2 is ordinary, so pinning 1.3 would
        # lock the rig out of clusters it is meant to drive.
        context = rig.tls_context("https://es.lab.local:9200", None)
        self.assertNotEqual(context.maximum_version, ssl.TLSVersion.TLSv1_3)


class NoArgumentCanTurnVerificationOff(unittest.TestCase):
    def test_the_client_takes_no_flag_for_it(self):
        taken = inspect.signature(rig.Es.__init__).parameters
        self.assertEqual(list(taken),
                         ["self", "base", "user", "password", "ca_cert"])

    def test_the_source_never_reaches_cert_none(self):
        with open(RIG) as handle:
            self.assertNotIn("CERT_NONE", handle.read())

    def test_the_command_line_has_no_insecure_flag(self):
        code, out = run_rig(["status", "--es", "https://127.0.0.1:1",
                             "--insecure"])
        self.assertNotEqual(code, 0)
        self.assertIn("unrecognized arguments: --insecure", out)

    def test_that_refusal_points_at_the_flag_to_use_instead(self):
        _code, out = run_rig(["status", "--es", "https://127.0.0.1:1",
                              "--insecure"])
        self.assertIn("--ca-cert", out)


class TheCaCertRefusesClearly(unittest.TestCase):
    def test_a_missing_file_names_the_flag(self):
        with self.assertRaises(SystemExit):
            rig.tls_context("https://es.lab.local:9200", "/nonexistent/ca.crt")

    def test_a_missing_file_is_refused_without_a_traceback(self):
        code, out = run_rig(["status", "--es", "https://127.0.0.1:1",
                             "--ca-cert", "/nonexistent/ca.crt"])
        self.assertNotEqual(code, 0)
        self.assertNotIn("Traceback", out)

    def test_that_refusal_names_the_flag_and_the_path(self):
        _code, out = run_rig(["status", "--es", "https://127.0.0.1:1",
                              "--ca-cert", "/nonexistent/ca.crt"])
        self.assertIn("--ca-cert '/nonexistent/ca.crt'", out)

    def test_a_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit):
                rig.tls_context("https://es.lab.local:9200", directory)

    def test_a_file_that_is_not_a_pem_bundle_is_refused(self):
        with tempfile.NamedTemporaryFile("w", suffix=".crt") as handle:
            handle.write("this-is-not-a-certificate\n")
            handle.flush()
            with self.assertRaises(SystemExit):
                rig.tls_context("https://es.lab.local:9200", handle.name)

    def test_that_refusal_does_not_quote_the_file(self):
        code, out = run_rig(["status", "--es", "https://127.0.0.1:1",
                             "--ca-cert", os.path.join(ROOT, "LICENSE")])
        self.assertNotEqual(code, 0)
        self.assertNotIn("Permission is hereby granted", out)

    def test_a_real_bundle_is_accepted(self):
        bundle = ssl.get_default_verify_paths().cafile
        if not bundle or not os.path.isfile(bundle):
            self.skipTest("this machine has no PEM CA bundle on disk")
        context = rig.tls_context("https://es.lab.local:9200", bundle)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)


class TheHelpSaysWhereTheCaComesFrom(unittest.TestCase):
    """An operator who cannot find the extraction command goes looking for a
    way to skip verification instead, so it is in the help and in the file's
    own docstring."""

    def test_the_help_names_the_secret_the_ca_lives_in(self):
        _code, out = run_rig(["status", "--help"])
        self.assertIn("es-http-certs-public", out)

    def test_the_help_shows_the_whole_command(self):
        _code, out = run_rig(["status", "--help"])
        self.assertIn("base64 -d", out)

    def test_the_module_docstring_carries_it_too(self):
        self.assertIn("es-http-certs-public", rig.__doc__)


class TheEndpointContributesNoPath(unittest.TestCase):
    """--es fixes a scheme and a host once. Every path comes from the rig,
    so nothing on the command line can hang a prefix in front of them."""

    def test_a_host_and_port_is_accepted(self):
        self.assertEqual(rig.endpoint_origin("https://es.lab.local:9200",
                                             "--es"),
                         "https://es.lab.local:9200")

    def test_a_trailing_slash_is_not_a_path(self):
        self.assertEqual(rig.endpoint_origin("https://127.0.0.1:1/", "--es"),
                         "https://127.0.0.1:1")

    def test_an_ipv6_literal_is_accepted(self):
        self.assertEqual(rig.endpoint_origin("https://[::1]:9200", "--es"),
                         "https://[::1]:9200")

    def test_a_path_is_refused(self):
        with self.assertRaises(SystemExit):
            rig.endpoint_origin("https://127.0.0.1:9200/es", "--es")

    def test_a_query_is_refused(self):
        with self.assertRaises(SystemExit):
            rig.endpoint_origin("https://127.0.0.1:9200/?x=1", "--es")

    def test_credentials_in_the_authority_are_refused(self):
        with self.assertRaises(SystemExit):
            rig.endpoint_origin("https://user:pw@127.0.0.1:9200", "--es")

    def test_a_non_http_scheme_is_refused(self):
        with self.assertRaises(SystemExit):
            rig.endpoint_origin("file:///etc/passwd", "--es")

    def test_a_scheme_less_value_is_refused(self):
        with self.assertRaises(SystemExit):
            rig.endpoint_origin("127.0.0.1:9200", "--es")


class RequestUrlsStartAtTheNamedHost(unittest.TestCase):
    def _client(self):
        return rig.Es("https://es.lab.local:9200", "elastic", "p", None)

    def test_a_plain_path_lands_on_the_endpoint(self):
        self.assertEqual(self._client().url_for("/_cluster/settings"),
                         "https://es.lab.local:9200/_cluster/settings")

    def test_a_query_string_survives_unchanged(self):
        self.assertEqual(
            self._client().url_for("/_cluster/settings?flat_settings=true"),
            "https://es.lab.local:9200/_cluster/settings?flat_settings=true")

    def test_a_whole_url_is_refused_as_a_path(self):
        with self.assertRaises(SystemExit):
            self._client().url_for("https://evil.example.com/_search")

    def test_a_path_that_looks_like_a_host_still_reaches_the_endpoint(self):
        # "//evil.example.com/x" starts with a slash and reads like an
        # authority, so the host of the result is what gets checked rather
        # than whether the name appears in the string.
        built = self._client().url_for("//evil.example.com/x")
        self.assertEqual(urllib.parse.urlsplit(built).netloc,
                         "es.lab.local:9200")


class FilesAreResolvedBeforeTheyAreOpened(unittest.TestCase):
    def test_an_input_file_resolves_to_its_real_path(self):
        with tempfile.TemporaryDirectory() as directory:
            real = os.path.join(directory, "state.json")
            open(real, "w").close()
            link = os.path.join(directory, "link.json")
            os.symlink(real, link)
            self.assertEqual(rig.resolve_input_file(link, "--state-file"),
                             os.path.realpath(real))

    def test_a_missing_input_file_is_refused(self):
        with self.assertRaises(SystemExit):
            rig.resolve_input_file("/nonexistent/state.json", "--state-file")

    def test_a_directory_is_not_an_input_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit):
                rig.resolve_input_file(directory, "--state-file")

    def test_an_output_file_may_not_exist_yet(self):
        with tempfile.TemporaryDirectory() as directory:
            wanted = os.path.join(directory, "reports.jsonl")
            self.assertEqual(rig.resolve_output_file(wanted, "--report-file"),
                             os.path.realpath(wanted))

    def test_an_output_file_in_a_missing_directory_is_refused(self):
        with self.assertRaises(SystemExit):
            rig.resolve_output_file("/nonexistent/dir/reports.jsonl",
                                    "--report-file")

    def test_an_output_file_over_a_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit):
                rig.resolve_output_file(directory, "--report-file")


class PathRefusalsNameTheFlagAndNeverTheContents(unittest.TestCase):
    def test_a_secret_file_refusal_names_the_flag(self):
        code, out = run_rig(["status", "--es", "http://127.0.0.1:1",
                             "--password-file", "/nonexistent/pw"])
        self.assertNotEqual(code, 0)
        self.assertIn("--password-file", out)

    def test_a_secret_file_refusal_is_not_a_traceback(self):
        _code, out = run_rig(["status", "--es", "http://127.0.0.1:1",
                              "--password-file", "/nonexistent/pw"])
        self.assertNotIn("Traceback", out)

    def test_a_readable_secret_file_gives_its_one_line(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt") as handle:
            handle.write("  secret-value\n")
            handle.flush()
            self.assertEqual(
                rig.read_secret_file(handle.name, "--password-file"),
                "secret-value")

    def test_a_secret_file_that_is_a_directory_shows_no_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            code, out = run_rig(["status", "--es", "http://127.0.0.1:1",
                                 "--password-file", directory])
        self.assertNotEqual(code, 0)
        self.assertIn("is a directory", out)

    def test_an_unwritable_state_file_is_refused_before_the_run(self):
        code, out = run_rig(["status", "--es", "http://127.0.0.1:1",
                             "--state-file", "/nonexistent/dir/state.json"])
        self.assertNotEqual(code, 0)
        self.assertNotIn("Traceback", out)


class TheObjectStoreClientIsUntouched(unittest.TestCase):
    """The store is Oracle's real endpoint on a live run. It verifies against
    the system trust store, and no cluster TLS setting reaches it."""

    def test_the_s3_client_builds_no_context(self):
        made = rig.S3("https://s3.example.com", "us-east-1", "ak", "sk", "b")
        self.assertFalse(hasattr(made, "ctx"))

    def test_the_s3_class_never_mentions_ssl(self):
        with open(RIG) as handle:
            source = handle.read()
        body = source[source.index("class S3:"):source.index("def make_s3(")]
        self.assertNotIn("ssl", body)

    def test_the_s3_class_passes_no_context_to_urlopen(self):
        with open(RIG) as handle:
            source = handle.read()
        body = source[source.index("class S3:"):source.index("def make_s3(")]
        self.assertNotIn("context=", body)


class TheHintPointsAtTheCaAndOnlyForLabHosts(unittest.TestCase):
    """A failed connection to a lab cluster is the moment the CA is needed.
    A routable cluster gets nothing, because its certificate should already
    chain to a public root."""

    def test_a_lab_host_without_a_ca_gets_the_command(self):
        args = argparse.Namespace(es="https://127.0.0.1:9200", ca_cert=None)
        self.assertIn("es-http-certs-public", rig.missing_ca_hint(args))

    def test_a_routable_host_gets_nothing(self):
        args = argparse.Namespace(es="https://cluster.example.com:9200",
                                  ca_cert=None)
        self.assertEqual(rig.missing_ca_hint(args), "")

    def test_a_supplied_ca_gets_nothing(self):
        args = argparse.Namespace(es="https://127.0.0.1:9200",
                                  ca_cert="ca.crt")
        self.assertEqual(rig.missing_ca_hint(args), "")

    def test_plain_http_gets_nothing(self):
        args = argparse.Namespace(es="http://127.0.0.1:9200", ca_cert=None)
        self.assertEqual(rig.missing_ca_hint(args), "")


if __name__ == "__main__":
    unittest.main()
