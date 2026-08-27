"""Where secrets come from, and the two things that must never happen.

A secret in argv is visible in `ps` to every user on the host, lands in shell
history, gets copied into container specs and turns up in CI logs. A secret in
an error message reaches a ticket, a screenshot and a chat channel. Both are
one careless line away at all times, so both are pinned here.
"""

import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generation_chain import cli
from generation_chain.corroboration import (Credentials, CorroborationUnavailable,
                                            ElasticsearchVeto)
from generation_chain.credentials import (CredentialError, Secret,
                                          load_elasticsearch, load_s3,
                                          require_private)
from generation_chain.sources.s3 import S3Credentials

PASSWORD = "hunter2-do-not-print-me"
API_KEY = "VnVhQ2ZHY0JDZGJrUW0tZTVhT3g6dWkybHAyYXhUTm1zeWFrdzl0dk5udw=="


def write(path, document, mode=0o600):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    os.chmod(path, mode)
    return path


class SecretsDoNotPrint(unittest.TestCase):

    def test_a_secret_renders_as_a_placeholder_everywhere_but_reveal(self):
        # The structural half. A rule people remember is one line away from
        # being forgotten, so every path out of the object except `reveal()`
        # produces the placeholder: an f-string in an error message, a repr in
        # a debugger, a dataclass repr and a str() all render it.
        secret = Secret(PASSWORD)
        self.assertNotIn(PASSWORD, f"{secret}")
        self.assertNotIn(PASSWORD, repr(secret))
        self.assertNotIn(PASSWORD, str(secret))
        self.assertNotIn(PASSWORD, f"{secret!r}")
        self.assertNotIn(PASSWORD, repr(S3Credentials("AKIA", secret)))
        self.assertNotIn(PASSWORD, repr(Credentials(username="u",
                                                    password=PASSWORD)))
        self.assertEqual(secret.reveal(), PASSWORD)

    def test_an_authentication_failure_prints_no_secret_anywhere(self):
        # The behavioural half, on the path an operator actually meets. A 403
        # from a cluster is the commonest reason this tool refuses, so its
        # message is the one most likely to be pasted into a ticket.
        def deny(request, timeout=None, **kwargs):
            raise urllib.error.HTTPError("u", 403, "forbidden", {}, None)

        veto = ElasticsearchVeto("http://es.invalid", "repo",
                                 Credentials(api_key=API_KEY), opener=deny)
        try:
            veto.fetch()
        except CorroborationUnavailable as exc:
            message = f"{exc} {exc!r} {exc.args}"
        else:
            self.fail("a 403 must refuse")
        self.assertNotIn(API_KEY, message)
        self.assertNotIn(API_KEY[:20], message)


class WhereCredentialsComeFrom(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="genchain-creds-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_the_json_file_supplies_both_kinds(self):
        # The use case. One file, one copy of each secret, and a path on the
        # command line rather than a value.
        path = write(os.path.join(self.dir, "creds.json"), {
            "s3": {"access_key_id": "AKIA", "secret_access_key": PASSWORD},
            "elasticsearch": {"api_key": API_KEY}})
        store = load_s3(path)
        self.assertEqual(store.access_key, "AKIA")
        self.assertEqual(store.secret_key.reveal(), PASSWORD)
        self.assertEqual(load_elasticsearch(path).api_key.reveal(), API_KEY)

    def test_a_credentials_file_others_can_read_is_refused(self):
        # Abuse case, and it is the ordinary one rather than an exotic attack:
        # a file that arrived by scp lands at 0644 by default, which is how a
        # credential leaks on a shared jump host.
        path = write(os.path.join(self.dir, "loose.json"),
                     {"s3": {"access_key_id": "A", "secret_access_key": "B"}},
                     mode=0o644)
        with self.assertRaises(CredentialError):
            load_s3(path)
        os.chmod(path, 0o600)
        self.assertEqual(load_s3(path).access_key, "A")

    def test_a_missing_section_is_a_refusal_not_anonymous_access(self):
        # Abuse case. A credentials file without the section for the transport
        # in use must not degrade into an unauthenticated request, which then
        # fails somewhere less obvious and reads as a missing bucket rather
        # than as a missing credential.
        path = write(os.path.join(self.dir, "partial.json"),
                     {"elasticsearch": {"api_key": API_KEY}})
        with self.assertRaises(CredentialError):
            load_s3(path)

    def test_a_section_with_an_empty_value_is_a_refusal(self):
        # Abuse case for a template somebody forgot to fill in. An empty
        # secret signs a request that a store rejects with a bare 403, which
        # sends the operator hunting for a permissions problem they do not
        # have.
        for body in ({"access_key_id": "A", "secret_access_key": ""},
                     {"access_key_id": "", "secret_access_key": "B"},
                     {"access_key_id": "A"}):
            path = write(os.path.join(self.dir, "empty.json"), {"s3": body})
            with self.assertRaises(CredentialError):
                load_s3(path)

    def test_a_private_file_passes_the_permission_check(self):
        # The use case for the check itself, so it cannot pass vacuously by
        # refusing everything.
        path = write(os.path.join(self.dir, "ok.json"), {})
        require_private(path)


class NoSecretsInArgv(unittest.TestCase):

    def test_no_flag_takes_a_secret_value(self):
        # argv reaches the process table, the shell history, container specs
        # and CI logs. The flag that names a credential takes a path.
        parser = cli.build_parser()
        flags = {option for action in parser._actions
                 for option in action.option_strings}
        for forbidden in ("--secret-key", "--access-key", "--password",
                          "--es-password", "--es-api-key", "--es-user",
                          "--es-username", "--key", "--api-key"):
            self.assertNotIn(forbidden, flags)
        self.assertIn("--credentials", flags)

    def test_the_zero_credential_path_still_runs(self):
        # A mirror on disk with no corroboration requested needs no credential
        # of any kind, and that is the offline and jump-host case. A
        # credential refactor must not quietly make a config file mandatory
        # for it.
        import genchain_fixtures as fx
        root = os.path.join(tempfile.mkdtemp(prefix="genchain-zero-"), "repo")
        self.addCleanup(shutil.rmtree, os.path.dirname(root), ignore_errors=True)
        fx.build_repository(root, [
            {"s1": {"idx": ["__a"]}},
            {"s1": {"idx": ["__a"]}, "s2": {"idx": ["__b"]}},
            {"s2": {"idx": ["__b"]}}])
        out, err = io.StringIO(), io.StringIO()
        environment = dict(os.environ)
        for name in list(environment):
            if name.startswith(("AWS_", "GENCHAIN_", "OCI_")):
                del os.environ[name]
        self.addCleanup(os.environ.update, environment)
        code = cli.main(["--local-repo", root], stdin=io.StringIO(),
                        stdout=out, stderr=err)
        self.assertEqual(code, 0)
        self.assertIn("indices/iuuid-idx/0/__a",
                      [line.split("\t")[0] for line in out.getvalue().splitlines()])


if __name__ == "__main__":
    unittest.main()
