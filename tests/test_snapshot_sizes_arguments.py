"""snapshot_sizes.py refuses a --batch that cannot fetch snapshot status."""

import base64
import contextlib
import io
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import snapshot_sizes as sizes


def check(*batch):
    """Run the tool's own parser and argument check, return the exit code."""
    parser = sizes.build_parser()
    args = parser.parse_args(
        ["--es", "https://es.example:9200", "--repo", "r", *batch])
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            sizes.check_arguments(parser, args)
        except SystemExit as exc:
            return exc.code
    return 0


class BatchMustFetchSomething(unittest.TestCase):

    def test_a_batch_of_one_is_accepted(self):
        # One snapshot per _status request is the smallest batch that still
        # fetches everything. An operator lowers --batch this far when a
        # cluster times out on bigger requests, so the floor must allow it.
        self.assertEqual(check("--batch", "1"), 0)

    def test_the_default_batch_is_accepted(self):
        # Every run that does not pass --batch goes through this path. A
        # check that rejected the default would break the tool for everyone.
        self.assertEqual(check(), 0)

    def test_a_batch_of_zero_is_refused(self):
        # Abuse: a wrapper that computes --batch from a snapshot count can
        # produce 0. The tool then crashed with a bare traceback partway
        # through the run instead of refusing before it started.
        self.assertEqual(check("--batch", "0"), 2)

    def test_a_negative_batch_is_refused(self):
        # Abuse: a negative batch fetched no snapshot status at all and the
        # run still exited 0, printing an empty sizing report that reads as
        # a repository with no snapshots. Capacity decisions get made from
        # that report.
        self.assertEqual(check("--batch", "-5"), 2)


class Credentials(unittest.TestCase):
    """Cluster secrets arrive by file or environment, never on argv."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        env = {k: v for k, v in os.environ.items()
               if k not in ("ES_PASSWORD", "GENCHAIN_ES_API_KEY")}
        # Most cases test credential handling, not confinement, so the
        # secret root is the directory their secrets are written to.
        env["GENCHAIN_SECRET_ROOT"] = self.dir.name
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def secret(self, text, mode=0o600):
        path = os.path.join(self.dir.name, "secret-%d" % len(os.listdir(
            self.dir.name)))
        with open(path, "w") as handle:
            handle.write(text)
        os.chmod(path, mode)
        return path

    def test_a_secret_in_the_current_directory_is_accepted(self):
        # The default for an operator running from the directory that holds
        # the file; refusing it would need a variable for every manual run.
        path = self.secret("s3cret")
        del os.environ["GENCHAIN_SECRET_ROOT"]
        with mock.patch("os.getcwd", return_value=self.dir.name):
            code, _, _ = self.run_check("--user", "bob",
                                        "--password-file", path)
        self.assertEqual(code, 0)

    def test_a_secret_elsewhere_is_refused_unread(self):
        # Abuse case: an agent that builds the command line from untrusted
        # text passes a 0600 private key as --password-file. Only the root
        # check stops it being sent to the cluster as the password.
        path = self.secret("s3cret")
        del os.environ["GENCHAIN_SECRET_ROOT"]
        with tempfile.TemporaryDirectory() as elsewhere, \
                mock.patch("os.getcwd", return_value=elsewhere):
            code, err, _ = self.run_check("--user", "bob",
                                          "--password-file", path)
        self.assertEqual(code, 2)
        self.assertIn("GENCHAIN_SECRET_ROOT", err)
        self.assertNotIn("s3cret", err)

    def test_the_secret_root_variable_makes_its_directory_readable(self):
        # A scheduled job keeps its secrets in a mounted directory while its
        # working directory is elsewhere; it must still start.
        path = self.secret("s3cret")
        os.environ["GENCHAIN_SECRET_ROOT"] = self.dir.name
        with tempfile.TemporaryDirectory() as elsewhere, \
                mock.patch("os.getcwd", return_value=elsewhere):
            code, _, _ = self.run_check("--user", "bob",
                                        "--password-file", path)
        self.assertEqual(code, 0)

    def test_a_link_inside_the_root_to_a_file_outside_is_refused(self):
        # Abuse case: a symlink placed inside the root must not carry the
        # read out to a file the root was meant to keep out of reach.
        with tempfile.TemporaryDirectory() as elsewhere:
            target = os.path.join(elsewhere, "key")
            with open(target, "w") as handle:
                handle.write("s3cret")
            os.chmod(target, 0o600)
            link = os.path.join(self.dir.name, "pw")
            os.symlink(target, link)
            os.environ["GENCHAIN_SECRET_ROOT"] = self.dir.name
            code, err, _ = self.run_check("--user", "bob",
                                          "--password-file", link)
        self.assertEqual(code, 2)
        self.assertNotIn("s3cret", err)

    def run_check(self, *extra):
        """Parse and check, return (exit code, stderr, parsed args)."""
        parser = sizes.build_parser()
        args = parser.parse_args(
            ["--es", "https://es.example:9200", "--repo", "r", *extra])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                sizes.check_arguments(parser, args)
            except SystemExit as exc:
                return exc.code, err.getvalue(), args
        return 0, err.getvalue(), args

    def header(self, args):
        """The Authorization header http_get would send, no network."""
        seen = {}

        class Answer:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *_):
                return False

            def read(self_inner):
                return b"{}"

        def fake_open(_opener, req, **_kwargs):
            seen["auth"] = req.get_header("Authorization")
            return Answer()

        args.es = "https://es.example:9200"
        args.tls = None
        with mock.patch.object(sizes.urllib.request.OpenerDirector,
                               "open", fake_open):
            sizes.http_get("/_cat", args)
        return seen["auth"]

    def test_a_password_file_authenticates_the_named_user(self):
        # If the file were ignored, the tool would query the cluster without
        # credentials and every operator on a secured cluster would be
        # pushed back to putting the password on the command line.
        path = self.secret("s3cret\n")
        code, _, args = self.run_check("--user", "bob",
                                       "--password-file", path)
        self.assertEqual(code, 0)
        want = base64.b64encode(b"bob:s3cret").decode()
        self.assertEqual(self.header(args), "Basic " + want)

    def test_the_password_environment_variable_authenticates(self):
        # CI has no private file to hand over. Losing the variable sends it
        # back to argv, which is the leak this change closes.
        os.environ["ES_PASSWORD"] = "fromenv"
        code, _, args = self.run_check("--user", "bob")
        self.assertEqual(code, 0)
        want = base64.b64encode(b"bob:fromenv").decode()
        self.assertEqual(self.header(args), "Basic " + want)

    def test_an_api_key_file_authenticates(self):
        # Clusters that issue API keys only would otherwise have no way to
        # run the tool without a key on argv.
        path = self.secret("id:key\n")
        code, _, args = self.run_check("--api-key-file", path)
        self.assertEqual(code, 0)
        self.assertEqual(self.header(args), "ApiKey id:key")

    def test_the_api_key_environment_variable_authenticates(self):
        os.environ["GENCHAIN_ES_API_KEY"] = "envid:envkey"
        code, _, args = self.run_check()
        self.assertEqual(code, 0)
        self.assertEqual(self.header(args), "ApiKey envid:envkey")

    def test_a_run_with_no_credentials_sends_no_authorization(self):
        # Abuse of the new defaults: an open lab cluster must still work,
        # and a stray header must not appear.
        code, _, args = self.run_check()
        self.assertEqual(code, 0)
        self.assertIsNone(self.header(args))

    def test_a_password_on_argv_is_refused_naming_the_replacement(self):
        # A user:password value on argv shows in ps and shell history for
        # every user on the host. It must stop before any request is made.
        code, err, _ = self.run_check("--user", "elastic:changeme")
        self.assertEqual(code, 2)
        self.assertIn("--password-file", err)
        self.assertNotIn("changeme", err)

    def test_an_api_key_on_argv_is_refused_naming_the_replacement(self):
        code, err, _ = self.run_check("--api-key", "abc123")
        self.assertEqual(code, 2)
        self.assertIn("--api-key-file", err)
        self.assertNotIn("abc123", err)

    def test_an_argv_secret_refuses_before_any_network_call(self):
        # The refusal has to precede the first request, or the secret has
        # already crossed the wire by the time the operator sees the error.
        with mock.patch.object(sizes.urllib.request.OpenerDirector,
                               "open") as opened:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    with mock.patch.object(sys, "argv", [
                            "snapshot_sizes.py", "--es",
                            "https://es.example:9200", "--repo", "r",
                            "--api-key", "abc123"]):
                        sizes.main()
        self.assertEqual(raised.exception.code, 2)
        opened.assert_not_called()

    def test_a_world_readable_secret_file_is_refused(self):
        # A file copied in with scp lands at 0644. Reading it would quietly
        # accept a credential every local user can read.
        path = self.secret("s3cret", mode=0o644)
        code, err, _ = self.run_check("--user", "bob",
                                      "--password-file", path)
        self.assertEqual(code, 2)
        self.assertIn("chmod 600", err)
        self.assertNotIn("s3cret", err)

    def test_a_group_readable_api_key_file_is_refused(self):
        path = self.secret("id:key", mode=0o640)
        code, _, _ = self.run_check("--api-key-file", path)
        self.assertEqual(code, 2)

    def test_a_read_only_0400_secret_file_is_accepted(self):
        # The refusal text offers 0400 as a fix. Refusing it would make the
        # advice wrong.
        path = self.secret("s3cret", mode=stat.S_IRUSR)
        code, _, _ = self.run_check("--user", "bob", "--password-file", path)
        self.assertEqual(code, 0)

    def test_a_missing_secret_file_is_refused(self):
        code, _, _ = self.run_check(
            "--user", "bob", "--password-file",
            os.path.join(self.dir.name, "absent"))
        self.assertEqual(code, 2)

    def test_an_empty_secret_file_is_refused(self):
        # An empty password authenticates as nothing and reads as a
        # cluster fault to whoever is on call.
        code, _, _ = self.run_check("--user", "bob",
                                    "--password-file", self.secret("\n"))
        self.assertEqual(code, 2)

    def test_a_user_without_any_password_is_refused(self):
        # A bare name used to be sent as the whole credential and fail as a
        # 401 that points at the cluster instead of the flags.
        code, err, _ = self.run_check("--user", "bob")
        self.assertEqual(code, 2)
        self.assertIn("--password-file", err)

    def test_a_password_and_an_api_key_together_are_refused(self):
        # Two credentials make the winner depend on code order, so the
        # identity a report was run as is a guess.
        path = self.secret("s3cret")
        key = self.secret("id:key")
        code, _, _ = self.run_check("--user", "bob", "--password-file", path,
                                    "--api-key-file", key)
        self.assertEqual(code, 2)

    def test_a_password_file_wins_over_the_environment_only_by_refusal(self):
        # Abuse: both file and variable set. Silently preferring one hides
        # which secret was used.
        os.environ["ES_PASSWORD"] = "fromenv"
        path = self.secret("s3cret")
        code, _, _ = self.run_check("--user", "bob", "--password-file", path)
        self.assertEqual(code, 2)



def run_main(*argv, env=None):
    """main() on a command line, with no request allowed out.

    Returns (exit code, stderr, whether a request was attempted).
    """
    clean = {k: v for k, v in os.environ.items()
             if k not in ("ES_PASSWORD", "GENCHAIN_ES_API_KEY")}
    clean.update(env or {})
    err = io.StringIO()
    with mock.patch.dict(os.environ, clean, clear=True), \
            mock.patch.object(sys, "argv", ["snapshot_sizes.py", *argv]), \
            mock.patch.object(
                sizes.urllib.request.OpenerDirector, "open",
                side_effect=sizes.urllib.error.URLError("blocked")) as opened, \
            contextlib.redirect_stderr(err), \
            contextlib.redirect_stdout(io.StringIO()):
        try:
            code = sizes.main()
        except SystemExit as exc:
            code = exc.code
    return code, err.getvalue(), opened.called


class ACredentialInTheEndpointIsRefused(unittest.TestCase):
    """--es is a host and a port. A password in it is a password on argv."""

    def test_a_user_and_password_in_es_are_refused_unechoed(self):
        # The password shows in ps and shell history, is never used to
        # authenticate, and was printed again in every failure line.
        for es in ("https://bob:hunter2@127.0.0.1:1",
                   "ftp://bob:hunter2@127.0.0.1:1"):
            with self.subTest(es=es):
                code, err, sent = run_main("--es", es, "--repo", "r")
                self.assertEqual(code, 2)
                self.assertNotIn("hunter2", err)
                self.assertFalse(sent)

    def test_a_user_name_alone_in_es_is_refused(self):
        # A name without a password still marks a URL someone meant to carry
        # a credential, and it is never the --user the run authenticates as.
        code, _, sent = run_main("--es", "https://bob@127.0.0.1:1",
                                 "--repo", "r")
        self.assertEqual(code, 2)
        self.assertFalse(sent)

    def test_a_plain_host_and_port_is_accepted(self):
        # The counterpart: an ordinary endpoint must get as far as a request.
        code, _, sent = run_main("--es", "https://127.0.0.1:1", "--repo", "r")
        self.assertTrue(sent)



class PlainHttpCarriesNoCredential(unittest.TestCase):
    """A credential goes to a remote cluster over TLS or not at all."""

    def test_a_password_over_plain_http_to_a_remote_host_is_refused(self):
        # Basic auth over http hands the cluster password to every host on
        # the path. The run has to stop before the first request carries it.
        code, _, sent = run_main(
            "--es", "http://es.example:9200", "--repo", "r", "--user", "bob",
            env={"ES_PASSWORD": "hunter2"})
        self.assertEqual(code, 2)
        self.assertFalse(sent)

    def test_an_api_key_over_plain_http_to_a_remote_host_is_refused(self):
        code, _, sent = run_main(
            "--es", "http://es.example:9200", "--repo", "r",
            env={"GENCHAIN_ES_API_KEY": "id:key"})
        self.assertEqual(code, 2)
        self.assertFalse(sent)

    def test_a_credential_over_plain_http_to_loopback_is_accepted(self):
        # kubectl port-forward serves the cluster on localhost, and the
        # credential never leaves the host. Refusing it would break the
        # documented lab invocation.
        for host in ("127.0.0.1", "localhost", "[::1]"):
            with self.subTest(host=host):
                _, _, sent = run_main(
                    "--es", f"http://{host}:1", "--repo", "r",
                    "--user", "bob", env={"ES_PASSWORD": "hunter2"})
                self.assertTrue(sent)

    def test_plain_http_without_a_credential_is_accepted(self):
        # An open lab cluster has nothing to leak and must still be read.
        _, _, sent = run_main("--es", "http://es.example:9200", "--repo", "r")
        self.assertTrue(sent)


if __name__ == "__main__":
    unittest.main()
