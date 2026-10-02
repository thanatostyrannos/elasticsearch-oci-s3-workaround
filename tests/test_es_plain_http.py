"""The Elasticsearch endpoint obeys the same plain-http rule as the store.

The veto sends an API key or a Basic password with every request. Without
this rule an operator who typed http:// for the cluster handed that secret to
anything on the path, while the store transport refused the same mistake.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import genchain_fixtures as fx
import s3rig
from generation_chain import cli as audit_cli
from generation_chain.reclaim import cli as reclaim_cli
from test_reclaim_cli import write_manifest

HISTORY = [{"s1": {"idx": ["__a"]}}]
# Every client opens through a redirect-refusing OpenerDirector, so patching
# its open method sees every request the tools could send, store or cluster.
URLOPEN = "urllib.request.OpenerDirector.open"


class _Workspace(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="es-plain-http-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.credentials = os.path.join(self.dir, "creds.json")
        with open(self.credentials, "w", encoding="utf-8") as handle:
            json.dump({"elasticsearch": {"api_key": "SECRET-API-KEY"},
                       "s3": {"access_key_id": s3rig.TEST_ACCESS_KEY,
                              "secret_access_key": s3rig.TEST_SECRET_KEY}},
                      handle)
        os.chmod(self.credentials, 0o600)


class AuditCliElasticsearchScheme(_Workspace):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.dir, "repo")
        fx.build_repository(self.root, HISTORY)

    def run_cli(self, url, *extra):
        argv = ["--local-repo", self.root, "--elasticsearch", url,
                "--es-repository", "r", "--credentials", self.credentials,
                "--quiet", *extra]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch(URLOPEN, side_effect=OSError("no cluster")) as opened:
            code = audit_cli.main(argv, stdin=io.StringIO(), stdout=out,
                                  stderr=err)
        return code, err.getvalue(), opened

    def test_a_remote_http_cluster_is_refused_before_any_request(self):
        # If this stopped passing, the cluster API key would cross the
        # network in the clear on a typo of http for https.
        code, err, opened = self.run_cli("http://es.example:9200")
        self.assertEqual(code, audit_cli.EXIT_USAGE)
        opened.assert_not_called()
        self.assertNotIn("SECRET-API-KEY", err)

    def test_the_operator_can_accept_plain_http_for_a_lab_cluster(self):
        # If this stopped passing, a lab cluster on a trusted network would
        # have no way to be consulted.
        code, _, opened = self.run_cli("http://es.example:9200",
                                       "--insecure-http")
        self.assertNotEqual(code, audit_cli.EXIT_USAGE)
        opened.assert_called()

    def test_https_is_accepted(self):
        # If this stopped passing, the normal production setup would be
        # refused by the new check.
        code, _, opened = self.run_cli("https://es.example:9200")
        self.assertNotEqual(code, audit_cli.EXIT_USAGE)
        opened.assert_called()

    def test_loopback_http_is_accepted(self):
        # If this stopped passing, the offline suite and a port-forwarded
        # cluster would be refused although no network path exists.
        code, _, opened = self.run_cli("http://127.0.0.1:9200")
        self.assertNotEqual(code, audit_cli.EXIT_USAGE)
        opened.assert_called()

    def test_credentials_in_the_url_do_not_disguise_a_remote_host(self):
        # If this stopped passing, http://localhost@es.example would pass as
        # loopback and send the key to es.example.
        code, _, opened = self.run_cli("http://localhost@es.example:9200")
        self.assertEqual(code, audit_cli.EXIT_USAGE)
        opened.assert_not_called()


class ReclaimCliElasticsearchScheme(_Workspace):
    def run_cli(self, url, *extra):
        manifest = os.path.join(self.dir, "manifest.tsv")
        write_manifest(manifest, ["indices/x/0/__a"])
        argv = ["--manifest", manifest, "--endpoint", "https://store.example",
                "--region", s3rig.TEST_REGION, "--bucket", "b",
                "--credentials", self.credentials, "--elasticsearch", url,
                "--es-repository", "r", *extra]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch(URLOPEN, side_effect=OSError("no cluster")) as opened:
            code = reclaim_cli.main(argv, stdout=out, stderr=err)
        return code, err.getvalue(), opened

    def test_a_remote_http_cluster_is_refused_before_any_request(self):
        # If this stopped passing, the execute-time re-check would send the
        # cluster credential in the clear right before a delete.
        for extra in ((), ("--execute",)):
            with self.subTest(extra=extra):
                code, err, opened = self.run_cli("http://es.example:9200",
                                                 *extra)
                self.assertEqual(code, reclaim_cli.EXIT_USAGE)
                opened.assert_not_called()
                self.assertNotIn("SECRET-API-KEY", err)

    def test_the_operator_can_accept_plain_http_for_a_lab_cluster(self):
        # If this stopped passing, a lab cluster could not be re-checked.
        code, _, _ = self.run_cli("http://es.example:9200", "--insecure-http")
        self.assertEqual(code, reclaim_cli.EXIT_OK)

    def test_https_and_loopback_http_are_accepted(self):
        # If this stopped passing, the normal setups would be refused.
        for url in ("https://es.example:9200", "http://127.0.0.1:9200"):
            with self.subTest(url=url):
                code, _, _ = self.run_cli(url)
                self.assertEqual(code, reclaim_cli.EXIT_OK)


if __name__ == "__main__":
    unittest.main()
