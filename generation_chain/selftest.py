"""Checks an operator can run on a jump host before pointing this at a bucket.

Deliberately narrow. It covers the parts that are invisible until they fail
expensively and that no amount of reading catches: the SigV4 canonical
request, which fails as a bare 403 naming no component, object key
percent-encoding, which fails the same way, Oracle's signing string and
Authorization parameter order, which fail as a bare 401, and the PKCS#1 v1.5
padding, which fails as the same 401 from a different cause.

Repository behaviour is not tested here. That belongs in this project's own
test suite, against captured Elasticsearch state, where a fixture can be
regenerated when a guard gets stricter.

No private key is shipped. The padding is checked against the DigestInfo
prefix RFC 8017 publishes for SHA-256, and the full private-key round trip
against an openssl-produced signature lives in the test suite instead.
"""

from __future__ import annotations

import hashlib
from typing import List, TextIO

from .formats.codec import CODEC_MAGIC, unwrap
from .formats.latest import parse_index_latest
from .sources.http_reads import ALLOWED_METHODS
from .sources.signing import oci_signature, sigv4
from .sources.signing.rsa import SHA256_DIGEST_INFO, RsaPrivateKey

# The get-vanilla case from the AWS Signature Version 4 test suite. Its inputs
# and its expected signature are published, so this is a known-answer test
# rather than an assertion about this package's own output.
SIGV4_VECTOR = {
    "access_key": "AKIDEXAMPLE",
    "secret_key": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
    "region": "us-east-1",
    "service": "service",
    "amz_date": "20150830T123600Z",
    "host": "example.amazonaws.com",
    "signature": "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31",
}
OCI_SIGNING_STRING = (
    b"date: Thu, 05 Jan 2014 21:31:40 GMT\n"
    b"(request-target): get /n/ns/b/bkt/o/index.latest\n"
    b"host: objectstorage.us-ashburn-1.oraclecloud.com")


def run(stream: TextIO) -> int:
    """Returns the number of failures, and prints each one."""
    failures: List[str] = []

    def check(label: str, got, want) -> None:
        if got != want:
            failures.append(f"{label}: got {got!r}, expected {want!r}")

    vector = SIGV4_VECTOR
    authorization = sigv4.authorization(
        access_key=vector["access_key"], secret_key=vector["secret_key"],
        method="GET", canonical_uri="/", canonical_query="",
        headers={"Host": vector["host"], "X-Amz-Date": vector["amz_date"]},
        payload_sha256=sigv4.EMPTY_PAYLOAD_SHA256, region=vector["region"],
        service=vector["service"], amz_date=vector["amz_date"])
    check("sigv4 known-answer signature",
          authorization.split("Signature=")[-1], vector["signature"])
    check("sigv4 signed header list",
          authorization.split("SignedHeaders=")[1].split(",")[0],
          "host;x-amz-date")
    check("sigv4 header normalisation", sigv4.authorization(
        access_key=vector["access_key"], secret_key=vector["secret_key"],
        method="GET", canonical_uri="/", canonical_query="",
        headers={"HOST": vector["host"],
                 "x-amz-date": "  " + vector["amz_date"] + " "},
        payload_sha256=sigv4.EMPTY_PAYLOAD_SHA256, region=vector["region"],
        service=vector["service"], amz_date=vector["amz_date"]), authorization)

    for raw, want in (("has space.dat", "has%20space.dat"),
                      ("has+plus.dat", "has%2Bplus.dat"),
                      ("has%25already", "has%2525already"),
                      ("~tilde-_.dat", "~tilde-_.dat"),
                      ("indices/a/0/__b", "indices/a/0/__b")):
        check(f"s3 key encoding {raw!r}", sigv4.quote_path(raw), want)
    check("s3 canonical query", sigv4.canonical_query(
        {"prefix": "a/b", "list-type": "2", "continuation-token": "x y"}),
        "continuation-token=x%20y&list-type=2&prefix=a%2Fb")

    check("oci signing string", oci_signature.signing_string(
        "GET", "/n/ns/b/bkt/o/index.latest",
        "objectstorage.us-ashburn-1.oraclecloud.com",
        "Thu, 05 Jan 2014 21:31:40 GMT"), OCI_SIGNING_STRING)
    check("oci authorization shape",
          oci_signature.authorization_header("t/u/f", b"\x01\x02"),
          'Signature algorithm="rsa-sha256",headers="date (request-target) '
          'host",keyId="t/u/f",signature="AQI=",version="1"')
    for raw, want in (("indices/Abc/0/__x", "indices/Abc/0/__x"),
                      ("a b/c", "a%20b/c"), ("a+b/c", "a%2Bb/c"),
                      ("50%25/x", "50%2525/x")):
        check(f"oci name encoding {raw!r}", oci_signature.quote_segment(raw), want)
    check("oci query encoding",
          oci_signature.query_string({"start": "a b/c"}), "start=a%20b%2Fc")

    # EMSA-PKCS1-v1_5 against the DigestInfo RFC 8017 publishes for SHA-256.
    padded = RsaPrivateKey(modulus=(1 << 2047), private_exponent=1)._pad(
        hashlib.sha256(b"generation-chain").digest())
    check("pkcs1 v1.5 leader", padded[:2], b"\x00\x01")
    check("pkcs1 v1.5 digest info",
          padded[padded.index(b"\x00", 2) + 1:][:len(SHA256_DIGEST_INFO)],
          SHA256_DIGEST_INFO)
    check("pkcs1 v1.5 block length", len(padded), 256)

    check("index.latest decode", parse_index_latest(b"\x00" * 7 + b"\x05"), 5)
    check("codec magic", CODEC_MAGIC, 0x3FD76C17)
    try:
        unwrap(b"\x00" * 40)
    except Exception:
        pass
    else:
        failures.append("a blob with no codec header was accepted")

    if ALLOWED_METHODS - {"GET", "HEAD"}:
        failures.append(
            f"this package may send {sorted(ALLOWED_METHODS)}; version one "
            "reads and never deletes")

    for failure in failures:
        stream.write(f"FAIL {failure}\n")
    if failures:
        stream.write(f"{len(failures)} self-test failure(s)\n")
    else:
        stream.write(
            "self-test OK: SigV4 known-answer vector, header normalisation, "
            "S3 key and query encoding, Oracle signing string and "
            "Authorization order, Oracle name and query encoding, PKCS#1 v1.5 "
            "padding, index.latest and codec framing, read-only method set\n")
    return len(failures)
