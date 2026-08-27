"""Request signing, measured against answers this package did not produce.

A signing mistake fails as a bare 403 that names no component, so reading the
code proves nothing and a test that compares the code against itself proves
less. The SigV4 case uses the vector AWS publishes. The OCI case uses a
signature openssl produced over the same bytes, with a throwaway key kept in
tests/fixtures alongside it.
"""

import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generation_chain.errors import GenerationChainError
from generation_chain.sources.signing import oci_signature, sigv4
from generation_chain.sources.signing.rsa import RsaPrivateKey

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
VECTOR = os.path.join(FIXTURES, "genchain-oci-signing-vector.json")

# The get-vanilla case from the AWS Signature Version 4 test suite.
AWS = {
    "access_key": "AKIDEXAMPLE",
    "secret_key": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
    "region": "us-east-1",
    "service": "service",
    "amzdate": "20150830T123600Z",
    "host": "example.amazonaws.com",
    "signature": "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31",
}


class SignatureVersion4(unittest.TestCase):

    def authorization(self, headers):
        return sigv4.authorization(
            access_key=AWS["access_key"], secret_key=AWS["secret_key"],
            method="GET", canonical_uri="/", canonical_query="",
            headers=headers, payload_sha256=sigv4.EMPTY_PAYLOAD_SHA256,
            region=AWS["region"], service=AWS["service"],
            amz_date=AWS["amzdate"])

    def test_the_published_vector_reproduces_its_published_signature(self):
        # This is the only check that separates "my canonical request" from
        # "the canonical request". Every other test in this file would still
        # pass with a consistently wrong one, and the store answers a wrong
        # one with a 403 that names nothing.
        auth = self.authorization({"Host": AWS["host"],
                                   "X-Amz-Date": AWS["amzdate"]})
        self.assertTrue(auth.endswith("Signature=" + AWS["signature"]))
        self.assertIn("SignedHeaders=host;x-amz-date", auth)

    def test_header_case_and_whitespace_do_not_change_the_signature(self):
        # Abuse case, and the mistake is easy to make: SigV4 lowercases names,
        # collapses runs of whitespace in values and sorts the list. A signer
        # that skipped any of the three signs a different request from the one
        # it sends, on some hosts and not others.
        loose = self.authorization({"HOST": AWS["host"],
                                    "x-amz-date": "  " + AWS["amzdate"] + " "})
        tight = self.authorization({"Host": AWS["host"],
                                    "X-Amz-Date": AWS["amzdate"]})
        self.assertEqual(loose, tight)

    def test_object_keys_are_percent_encoded_once_and_slashes_survive(self):
        # A snapshot repository holds keys with plus signs and percent signs
        # in them. Encoding a key twice, or not encoding it, reads a different
        # object from the one the listing named, and the derivation then
        # attributes one blob's file list to another blob's name.
        for raw, want in (("simple.dat", "simple.dat"),
                          ("indices/a/0/__b", "indices/a/0/__b"),
                          ("has space.dat", "has%20space.dat"),
                          ("has+plus.dat", "has%2Bplus.dat"),
                          ("has%25already", "has%2525already"),
                          ("~tilde-_.dat", "~tilde-_.dat")):
            self.assertEqual(sigv4.quote_path(raw), want, raw)

    def test_query_parameters_are_sorted_and_slashes_are_encoded(self):
        # Listing a repository means paging with a continuation token, which
        # is an opaque string that can hold anything. A query canonicalised
        # differently from the one sent fails on page two of a long listing
        # and looks like a permissions problem.
        self.assertEqual(
            sigv4.canonical_query({"prefix": "a/b", "list-type": "2",
                                   "continuation-token": "x y"}),
            "continuation-token=x%20y&list-type=2&prefix=a%2Fb")


class OciRequestSigning(unittest.TestCase):

    def setUp(self):
        with open(VECTOR, encoding="utf-8") as fh:
            self.vector = json.load(fh)

    def test_openssl_and_this_package_sign_the_same_bytes_alike(self):
        # No OCI endpoint exists on this machine, so this vector is the only
        # thing standing between a signing bug and an operator discovering it
        # against a production tenancy. openssl produced the expected value.
        for form in ("private_key_pkcs8", "private_key_pkcs1"):
            key = RsaPrivateKey.from_pem(self.vector[form].encode())
            signature = key.sign_sha256(self.vector["signing_string"].encode())
            self.assertEqual(base64.b64encode(signature).decode(),
                             self.vector["signature_base64"], form)

    def test_the_signing_string_is_the_three_headers_in_oracle_s_order(self):
        # Oracle signs date, then (request-target), then host, and the order
        # is part of the signature rather than a presentation detail. A signer
        # that sorted them alphabetically produces a valid signature over the
        # wrong string and gets a 401 naming nothing.
        built = oci_signature.signing_string(
            "GET", "/n/ns/b/bucket/o?prefix=x",
            "objectstorage.us-ashburn-1.oraclecloud.com",
            "Thu, 05 Jan 2014 21:31:40 GMT")
        self.assertEqual(built.decode(), self.vector["signing_string"])

    def test_the_authorization_header_carries_oracle_s_parameter_order(self):
        # Read off the SDK rather than guessed. Oracle accepts this header and
        # a reordered one is the kind of thing that works against one gateway
        # and not another.
        header = oci_signature.authorization_header(
            key_id="tenancy/user/fingerprint", signature=b"\x01\x02")
        self.assertEqual(
            header,
            'Signature algorithm="rsa-sha256",headers="date (request-target) '
            'host",keyId="tenancy/user/fingerprint",signature="AQI=",'
            'version="1"')

    def test_a_passphrase_protected_key_is_refused_rather_than_guessed(self):
        # Abuse case. An encrypted key looks like a PEM and decodes to
        # nothing useful. Refusing by name tells the operator to add a
        # pass_phrase line; carrying on produces a signature over garbage and
        # a 401 that sends them to the wrong page of Oracle's documentation.
        with self.assertRaises(GenerationChainError):
            RsaPrivateKey.from_pem(
                b"-----BEGIN ENCRYPTED PRIVATE KEY-----\nAAAA\n"
                b"-----END ENCRYPTED PRIVATE KEY-----\n")

    def test_a_public_key_where_a_private_key_belongs_is_refused(self):
        # Abuse case, and the commonest one: an operator hands over the key
        # they uploaded to the Console rather than the one they kept.
        with self.assertRaises(GenerationChainError):
            RsaPrivateKey.from_pem(
                b"-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n")


if __name__ == "__main__":
    unittest.main()
