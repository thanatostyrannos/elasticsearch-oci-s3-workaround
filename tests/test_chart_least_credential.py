"""Each pod the chart renders mounts only the credentials its command uses.

The static checks read template text and need nothing installed. The rendered
checks run `helm template` and skip when helm is absent, so the standard-library
suite still runs everywhere.
"""

import pathlib
import re
import shutil
import subprocess
import unittest

CHART = (
    pathlib.Path(__file__).resolve().parent.parent
    / "gitlab" / "kubernetes-test-rig" / "chart"
)
TEMPLATES = {p.name: p.read_text() for p in (CHART / "templates").iterdir()}
AUDIT = TEMPLATES["audit-cronjob.yaml"]
HELM = shutil.which("helm")

IN_CLUSTER = ["--set", "elasticsearch.external=false"]
WITH_KEY = ["--set-string", "credentials.elasticsearch.apiKey=ro-key-value"]
ASK = ["--set", "auditCronJob.askElasticsearch=true",
       "--set", "auditCronJob.esRepository=r"]
TLS_ON = ["--set", "elasticsearch.eck.disableTls=false"]


def render(*flags, show=None):
    cmd = [HELM, "template", "r", str(CHART), *flags]
    if show:
        cmd += ["-s", f"templates/{show}"]
    return subprocess.run(cmd, capture_output=True, text=True)


def secret_keys(doc):
    return set(re.findall(r'^\s*- key: "?([^"\s]+)"?$', doc, re.M))


class StaticTemplates(unittest.TestCase):

    def test_no_template_turns_certificate_verification_off(self):
        # wait-for-elasticsearch once set CERT_NONE, so a man in the middle
        # on the pod network could answer "Elasticsearch is up" for any host.
        # If CERT_NONE returns anywhere in the chart, every readiness check
        # trusts whoever answers.
        for name, text in TEMPLATES.items():
            self.assertNotIn("CERT_NONE", text, name)
            self.assertNotIn("check_hostname = False", text, name)

    def test_audit_does_not_name_the_harness_login_or_the_s3_key_file(self):
        # The audit is documented as read-only. If its template references
        # the harness password file or the standalone S3 secret file, a
        # shell in the audit pod reaches a login that can delete snapshots.
        self.assertNotIn("keys.esPassword", AUDIT)
        self.assertNotIn("keys.s3SecretAccessKey", AUDIT)
        self.assertNotIn("harnessEsUser", AUDIT)

    def test_no_template_writes_the_elastic_superuser_into_creds_json(self):
        # Rewriting creds.json to the ECK elastic user hands the audit's
        # veto the cluster superuser over plain http. If this returns, the
        # read-only audit authenticates as an account that can delete.
        helpers = TEMPLATES["_helpers.tpl"]
        self.assertNotIn('section["username"]', helpers)
        self.assertNotIn("section.pop", helpers)

    def test_abuse_a_superuser_rewrite_is_detected(self):
        # Models the old staging script; the check above must see it.
        bad = 'section["username"], section["password"] = "elastic", password'
        self.assertIn('section["username"]', bad)


@unittest.skipUnless(HELM, "helm is not installed")
class RenderedPods(unittest.TestCase):

    def test_default_audit_mounts_only_creds_json(self):
        # A shell in the audit pod must find the store read credential and
        # nothing that can delete. Any extra key here is a wider blast radius
        # for a pod that runs unattended on a timer.
        out = render(show="audit-cronjob.yaml")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(secret_keys(out.stdout), {"creds.json"})

    def test_default_churn_rig_does_not_mount_creds_json(self):
        # The load generator logs in with the harness password and reads no
        # creds.json; mounting it hands it the audit's keys for nothing.
        out = render(show="churn-rig-job.yaml")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(secret_keys(out.stdout), {"es-password"})

    def test_listing_adds_the_s3_secret_to_churn_rig_only_when_enabled(self):
        # The store secret reaches the pod only when the listing that uses it
        # is switched on. Always mounting it leaks it to runs that never read it.
        out = render("--set", "churnRig.listing.enabled=true",
                     show="churn-rig-job.yaml")
        self.assertEqual(secret_keys(out.stdout),
                         {"es-password", "s3-secret-access-key"})

    def test_qualify_mounts_creds_and_harness_login_only(self):
        # The loop needs creds.json for its veto and the harness login for
        # its own cluster calls; the S3 secret file is for the load generator.
        out = render(show="qualify-job.yaml")
        self.assertEqual(secret_keys(out.stdout),
                         {"creds.json", "es-password"})

    def test_teardown_mounts_harness_login_only(self):
        # Teardown deletes the rig's own objects and needs the harness login.
        # It has no use for the audit's credential file.
        out = render("--set", "teardown.standalone.enabled=true",
                     show="teardown-manual-job.yaml")
        self.assertEqual(secret_keys(out.stdout), {"es-password"})

    def test_in_cluster_audit_has_no_eck_superuser_secret(self):
        # The ECK elastic-user Secret holds the superuser password. The audit
        # pod must never mount it, in an init container or anywhere else.
        out = render(*IN_CLUSTER, *WITH_KEY, *ASK, *TLS_ON,
                     show="audit-cronjob.yaml")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("elastic-user", out.stdout)
        self.assertNotIn("elastic", out.stdout.replace("elasticsearch", "")
                         .replace("Elasticsearch", "").replace("elastic.co", ""))
        # ca.crt is the public operator CA, not a credential.
        self.assertEqual(secret_keys(out.stdout), {"creds.json", "ca.crt"})

    def test_in_cluster_churn_rig_still_takes_the_eck_password(self):
        # The harness needs an admin login to set up ILM and SLM. If the ECK
        # password stops reaching it, every in-cluster run authenticates with
        # a password nothing ever set.
        out = render(*IN_CLUSTER, *WITH_KEY, *TLS_ON, show="churn-rig-job.yaml")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("elastic-user", out.stdout)

    def test_in_cluster_without_a_read_only_key_refuses_to_render(self):
        # Abuse case: in-cluster ES with the audit asking it questions and
        # only the placeholder key. Rendering anyway would run the audit
        # with a key that fails, or tempt a fallback to the superuser. The
        # message must name the value to set.
        out = render(*IN_CLUSTER, *ASK, *TLS_ON)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("credentials.elasticsearch.apiKey", out.stderr)

    def test_in_cluster_username_elastic_is_refused(self):
        # Abuse case: the operator sets the audit's login to the superuser
        # by hand. The chart must not bless that for a read-only job.
        out = render(*IN_CLUSTER, *ASK, *TLS_ON,
                     "--set", "credentials.elasticsearch.authMethod=usernamePassword",
                     "--set-string", "credentials.elasticsearch.password=pw")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("credentials.elasticsearch.username", out.stderr)

    def test_in_cluster_audit_over_plain_http_is_refused(self):
        # The read-only key would cross the pod network in the clear. The
        # refusal names the switch that turns ECK's TLS on.
        out = render(*IN_CLUSTER, *WITH_KEY, *ASK)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("elasticsearch.eck.disableTls", out.stderr)

    def test_in_cluster_tls_uses_https_and_the_operator_ca(self):
        # With ECK TLS on, tools must verify the operator's CA. Pointing at
        # http, or at https without the CA, sends credentials unverified.
        out = render(*IN_CLUSTER, *WITH_KEY, *ASK, *TLS_ON,
                     show="audit-cronjob.yaml")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("--elasticsearch https://", out.stdout)
        self.assertNotIn("http://r-es-rig-es-http", out.stdout)
        self.assertIn("--es-ca-cert /es-ca-cert/ca.crt", out.stdout)
        self.assertIn("r-es-rig-es-http-certs-public", out.stdout)

    def test_qualify_gets_the_ca_so_its_password_never_crosses_plain_http(self):
        # qualify sends the harness password to the cluster. With ECK TLS on
        # it must verify the operator CA; without --es-ca-cert it would either
        # fail every call or push the password over http.
        out = render(*IN_CLUSTER, *WITH_KEY, *TLS_ON, show="qualify-job.yaml")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("--elasticsearch https://", out.stdout)
        self.assertIn("--es-ca-cert /es-ca-cert/ca.crt", out.stdout)
        self.assertIn("r-es-rig-es-http-certs-public", out.stdout)

    def test_external_ca_reaches_qualify_too(self):
        # An external cluster with a private CA needs the same flag on the
        # loop, or its cluster calls fail verification.
        out = render("--set-string", "elasticsearch.caCert=PEM",
                     show="qualify-job.yaml")
        self.assertIn("--es-ca-cert /es-ca-cert/ca.crt", out.stdout)

    def test_every_credential_sender_refuses_plain_http_in_cluster(self):
        # Abuse case: each pod that sends an Elasticsearch credential, with
        # everything else off, must stop the render on its own. If one slips
        # through, that pod's password crosses the pod network in clear.
        off = ["--set", "auditCronJob.enabled=false",
               "--set", "churnRig.enabled=false",
               "--set", "qualify.enabled=false",
               "--set", "teardown.hook.enabled=false"]
        cases = {
            "audit": ["--set", "auditCronJob.enabled=true", *ASK, *WITH_KEY],
            "qualify": ["--set", "qualify.enabled=true", *WITH_KEY],
            "churn rig": ["--set", "churnRig.enabled=true"],
            "teardown hook": ["--set", "teardown.hook.enabled=true"],
            "teardown standalone": ["--set", "teardown.standalone.enabled=true"],
        }
        for name, extra in cases.items():
            with self.subTest(name):
                out = render(*IN_CLUSTER, *off, *extra)
                self.assertNotEqual(out.returncode, 0)
                self.assertIn("elasticsearch.eck.disableTls", out.stderr)

    def test_existing_secret_skips_the_key_value_check(self):
        # With a Secret the operator manages, the chart cannot read the key,
        # so it must trust it instead of refusing a valid setup.
        out = render(*IN_CLUSTER, *ASK, *TLS_ON,
                     "--set-string", "credentials.existingSecret=mine")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_wait_for_elasticsearch_verifies_with_the_pod_ca(self):
        # The readiness probe must verify the same CA the tool uses, or it
        # reports "up" for an impostor.
        out = render("--set-string", "elasticsearch.caCert=PEM",
                     show="churn-rig-job.yaml")
        self.assertIn("cafile", out.stdout)
        self.assertIn("/es-ca-cert/ca.crt", out.stdout)

    def test_wait_for_elasticsearch_is_tcp_only_when_tls_is_unverifiable(self):
        # Abuse case: insecureTls with no CA. The probe must not fall back to
        # an unverified HTTPS request; it checks only that the port accepts a
        # connection. The churn Job and the teardown hook refuse insecureTls
        # outright, so only a release without them reaches this probe.
        out = render("--set", "elasticsearch.insecureTls=true",
                     "--set", "churnRig.enabled=false",
                     "--set", "teardown.hook.enabled=false",
                     show="audit-cronjob.yaml")
        self.assertIn("create_connection", out.stdout)
        self.assertNotIn("urlopen", out.stdout)

    def test_every_pod_that_waits_mounts_the_ca_it_verifies_with(self):
        # The qualify pod runs the wait step too. If the CA volume is missing
        # there, the probe cannot read the file and the pod never starts.
        out = render("--set-string", "elasticsearch.caCert=PEM",
                     show="qualify-job.yaml")
        self.assertIn("es-ca-cert", out.stdout)


if __name__ == "__main__":
    unittest.main()
