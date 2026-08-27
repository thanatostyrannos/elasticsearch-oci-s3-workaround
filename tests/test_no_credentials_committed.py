"""Nothing that authenticates anything reaches a tracked file.

The repository is public and its whole subject is a tool that needs bucket
credentials, so the failure mode is obvious and the defence has to be a test
rather than a habit. A .gitignore protects a path; this protects the content,
including a file someone adds under a name nobody thought to ignore.

Shapes, never values. A test that searched for a specific key would publish it.
"""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# A real OCID carries roughly sixty characters after the dots. The documented
# placeholders, `ocid1.tenancy.oc1..aaaa` and `ocid1.tenancy.oc1..aaaaexample`,
# are far shorter, so the length is what separates an example from a leak.
PATTERNS = {
    # Every OCID type, not a hand-listed few. The list used to name four and
    # missed `ocid1.credential.`, which is the id type Oracle gives a customer
    # secret key and therefore the access key id the S3 compatibility path
    # signs with. Naming types by hand means the one you forget is unguarded.
    "an OCID with a real tail":
        re.compile(r"ocid1\.[a-z0-9]+\.[a-z0-9-]+\.\.[a-z0-9]{25,}"),
    "a PEM private key":
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "an API key fingerprint":
        re.compile(r"\b(?:[0-9a-f]{2}:){15}[0-9a-f]{2}\b"),
    "an AWS-shaped access key id":
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # The secret half of an S3 compatibility credential. Oracle's is base64 and
    # carries no prefix to key on, so the field name is what makes this
    # findable without matching every base64 string in the repository. The
    # value is captured so a documented placeholder can be excused by value.
    "an object storage secret key":
        re.compile(r"(?:secret_access_key|aws_secret_access_key|secret_key)"
                   r"\"?\s*[:=]\s*\"?([A-Za-z0-9+/]{26,}={0,2})"),
    # A real address belongs to a person and this repository is public. The
    # reserved domains are exempt because documentation has to be able to show
    # what an address looks like: example.com, .org and .net are reserved by
    # RFC 2606, and so are the .invalid, .test and .example top level domains.
    "a personal email address":
        re.compile(r"\b[A-Za-z0-9._%+-]+@(?!example\.(?:com|org|net)\b)"
                   r"[A-Za-z0-9.-]+\.(?!invalid\b|test\b|example\b)"
                   r"[A-Za-z]{2,}\b"),
}

# An API key fingerprint is an MD5 of a PUBLIC key, so it authenticates nothing
# on its own. It is checked anyway because it identifies a tenancy, and the
# documentation has to be able to show the shape. These are the placeholders it
# shows: sequential hex, valid nowhere. Listed by value rather than matched by a
# cleverness rule, so a real fingerprint still fails.
PLACEHOLDER_FINGERPRINTS = frozenset({
    "a1:b2:c3:d4:e5:f6:07:18:29:3a:4b:5c:6d:7e:8f:90",
    "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99",
    "11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
})

# One secret is exempt, by value: AWS's own published signing example, which the
# SigV4 vectors have to use for the expected signature to mean anything. Listed
# by value, like the fingerprints, so a real secret sitting in the same field
# still fails. The documentation carries no example credential at all now, real
# or invented: a realistic-looking key in a public repository invites people to
# probe the environment it appears to belong to, so the docs say
# `<<<Redacted>>>` and describe the shape in words.
PLACEHOLDER_SECRETS = frozenset({
    "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
})

PLACEHOLDERS = PLACEHOLDER_FINGERPRINTS | PLACEHOLDER_SECRETS

# One file is exempt, by path, with a reason. It holds a throwaway RSA key whose
# only job is to pin the OCI request-signing implementation to a known signature,
# so the key has to be committed for the vector to mean anything. It was never
# uploaded to a tenancy and authenticates nothing. The exemption is one path
# rather than a pattern, so a second key file cannot inherit it quietly.
# This file is exempt from its own scan. It has to contain the shapes it looks
# for, both as the patterns themselves and as the samples that prove those
# patterns still match. Every string in it is synthetic and valid nowhere.
ALLOWED = (
    "tests/fixtures/genchain-oci-signing-vector.json",
    "tests/test_no_credentials_committed.py",
)


def tracked_text_files():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    for name in out.split("\0"):
        if not name or name in ALLOWED:
            continue
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            yield name, path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


def findings_in(name, text):
    """What the scan would report about one file's text.

    Extracted so the exemption below is reachable from a test. While this
    lived inline in the corpus test, nothing could show that a placeholder is
    excused BECAUSE the scan excuses it rather than by accident, and an
    exemption no test can see the removal of is the thing this file exists to
    prevent elsewhere.
    """
    findings = []
    for what, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            # A pattern that captures the secret is excused on the captured
            # value; one that does not is excused on the whole match.
            # Comparing only group(0) left a placeholder inside a longer
            # match unrecognised.
            candidates = {match.group(0)}
            candidates.update(group for group in match.groups() if group)
            if candidates & PLACEHOLDERS:
                continue
            line = text[:match.start()].count("\n") + 1
            findings.append(f"{name}:{line}: {what}")
    return findings


class TestNoCredentialsCommitted(unittest.TestCase):
    def test_no_tracked_file_carries_credential_material(self):
        findings = []
        for name, text in tracked_text_files():
            findings.extend(findings_in(name, text))
        self.assertEqual(findings, [], "\n".join(findings))

    def test_the_scan_reads_the_corpus_rather_than_reporting_zero(self):
        # A guard that found nothing because it read nothing would pass forever
        # and protect nothing. This is the tripwire for that.
        names = [n for n, _ in tracked_text_files()]
        self.assertGreater(len(names), 100, "the file walk stopped working")

    def test_the_patterns_catch_what_they_claim_to(self):
        # Synthetic, and not valid anywhere. Without this the patterns could be
        # quietly wrong and the suite would still be green.
        samples = {
            "an OCID with a real tail":
                "ocid1.tenancy.oc1..aaaaaaaa" + "b" * 30,
            "a PEM private key": "-----BEGIN RSA PRIVATE KEY-----",
            "an API key fingerprint": ":".join(["ab"] * 16),
            "an AWS-shaped access key id": "AKIA" + "A" * 16,
            "an object storage secret key":
                '"secret_key": "T7kQm2Xp9RzL4vB8nD1sW6yA3cF5hJ0gK2eU="',
            "a personal email address": "someone@a-real-domain.dev",
        }
        for what, sample in samples.items():
            with self.subTest(pattern=what):
                self.assertRegex(sample, PATTERNS[what])

    def test_every_ocid_type_is_caught_not_just_the_ones_first_thought_of(self):
        # The access key id for the S3 compatibility path is
        # `oci_identity_customer_secret_key.probe.id`, and that resource's id
        # is an `ocid1.credential.` OCID. Naming resource types by hand missed
        # it, because `customersecretkey` is the RESOURCE type and `credential`
        # is the ID type. Any type with a real tail is a leak.
        tail = "a" * 25
        for kind in ("credential", "compartment", "bucket", "tenancy",
                     "user", "customersecretkey"):
            with self.subTest(kind=kind):
                self.assertRegex(f"ocid1.{kind}.oc1..{tail}",
                                 PATTERNS["an OCID with a real tail"])

    def test_the_object_storage_secret_key_is_caught(self):
        # The other half of the S3 compatibility pair. It is the half that
        # actually authenticates, and until this pattern existed nothing
        # looked for it at all.
        for sample in (
                '"secret_access_key": "T7kQm2Xp9RzL4vB8nD1sW6yA3cF5hJ0gK2eU="',
                "secret_access_key = T7kQm2Xp9RzL4vB8nD1sW6yA3cF5hJ0gK2eU=",
                '"secret_key": "T7kQm2Xp9RzL4vB8nD1sW6yA3cF5hJ0gK2eU="'):
            with self.subTest(sample=sample[:24]):
                self.assertRegex(
                    sample, PATTERNS["an object storage secret key"])

    def test_the_documented_secret_placeholders_survive_the_scan(self):
        # Documentation and the signing self-test have to be able to show the
        # shape. Asserted through the scan rather than against the pattern,
        # because what matters is that the SCAN excuses them.
        for placeholder in PLACEHOLDER_SECRETS:
            with self.subTest(placeholder=placeholder[:12]):
                text = f'"secret_key": "{placeholder}"'
                self.assertRegex(text, PATTERNS["an object storage secret key"],
                                 "the shape should still match")
                self.assertEqual(findings_in("doc.md", text), [])

    def test_a_real_looking_secret_is_still_caught_by_the_scan(self):
        # The allowlist is by value, so it cannot grow into a blanket pass.
        # Also through the scan: this is the case that goes green if someone
        # widens the exemption from a value set to a shape rule.
        real_shaped = "Qz4Wn8Kv2Tb6Yr0Xm5Lp9Dc3Fh7Js1Ga="
        self.assertNotIn(real_shaped, PLACEHOLDER_SECRETS)
        self.assertEqual(
            findings_in("leak.json", f'"secret_key": "{real_shaped}"'),
            ["leak.json:1: an object storage secret key"])

    def test_a_real_ocid_is_caught_by_the_scan(self):
        # The same, for the half that identifies rather than authenticates.
        self.assertEqual(
            findings_in("leak.json", "ocid1.credential.oc1.." + "a" * 25),
            ["leak.json:1: an OCID with a real tail"])

    def test_a_real_looking_fingerprint_is_still_caught(self):
        # The allowlist is by value, so it cannot accidentally excuse a real
        # one. This is what stops it growing into a blanket exemption.
        real_shaped = "3f:0a:9c:d1:44:b7:22:e8:5a:16:cc:90:7d:31:ef:68"
        self.assertNotIn(real_shaped, PLACEHOLDER_FINGERPRINTS)
        self.assertRegex(real_shaped, PATTERNS["an API key fingerprint"])

    def test_reserved_example_domains_are_not_flagged(self):
        # Documentation has to be able to show the shape of an address without
        # the guard treating it as a leak.
        for allowed in ("esprobe-service-user@example.com",
                        "nobody@example.invalid",
                        "someone@example.org",
                        "test@host.test"):
            with self.subTest(address=allowed):
                self.assertIsNone(
                    PATTERNS["a personal email address"].search(allowed))

    def test_the_documented_placeholders_are_not_flagged(self):
        # The README has to be able to show the shape of an OCID.
        for placeholder in ("ocid1.tenancy.oc1..aaaa",
                            "ocid1.tenancy.oc1..aaaaexample",
                            "ocid1.user.oc1..aaaa"):
            with self.subTest(placeholder=placeholder):
                self.assertIsNone(
                    PATTERNS["an OCID with a real tail"].search(placeholder))


if __name__ == "__main__":
    unittest.main()
