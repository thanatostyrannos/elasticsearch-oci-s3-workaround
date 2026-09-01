#!/usr/bin/env python3
"""Minimal reproduction: OCI S3 Compatibility rejects x-amz-checksum-crc32.

Sends the SAME DeleteObjects request four times against the same bucket, with
the same credentials, varying only the integrity header:

    Content-MD5             expected 200
    x-amz-checksum-crc32c   expected 200
    x-amz-checksum-sha256   expected 200
    x-amz-checksum-crc32    expected 400  <-- the defect

Amazon S3 accepts all four. OCI accepts three and rejects crc32, whose only
difference from crc32c is the polynomial.

The default is the AWS SDK's, not any one application's. Since v2.30.0 the AWS
SDK for Java sends a flexible checksum in place of Content-MD5 on operations
that require one, and it defaults to CRC32. Anything built on that SDK inherits
it. Elasticsearch is one such client and does not override the default, so
every batch delete its snapshot repositories issue against OCI fails and the
objects are never reclaimed.

The keys named below do not exist. DeleteObjects on an absent key is a success
on S3 and on OCI, so a run that reaches the store deletes nothing: the request
is rejected before OCI looks at the keys at all.

Standard library only. Reads credentials from a JSON file, never from argv.

    ./oci-deleteobjects-checksum-repro.py --endpoint https://<ns>.compat.objectstorage.<region>.oraclecloud.com \
        --region <region> --bucket <bucket> --credentials creds.json
"""

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import sys
import hmac
import urllib.error
import urllib.parse
import urllib.request
import zlib


# --- SigV4, inlined so this file runs anywhere with nothing installed -------

ALGORITHM = "AWS4-HMAC-SHA256"


def _hmac(key, message):
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def authorization(access_key, secret_key, method, canonical_uri,
                  canonical_query, headers, payload_sha256, region,
                  service, amz_date):
    lowered = {k.lower().strip(): " ".join(str(v).split())
               for k, v in headers.items()}
    signed = ";".join(sorted(lowered))
    canonical_headers = "".join(f"{n}:{lowered[n]}\n" for n in sorted(lowered))
    canonical_request = "\n".join([method, canonical_uri, canonical_query,
                                   canonical_headers, signed, payload_sha256])
    datestamp = amz_date[:8]
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    to_sign = "\n".join([ALGORITHM, amz_date, scope,
                         hashlib.sha256(canonical_request.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode(), datestamp),
                            region), service), "aws4_request")
    signature = hmac.new(key, to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    return (f"{ALGORITHM} Credential={access_key}/{scope}, "
            f"SignedHeaders={signed}, Signature={signature}")


CRC32C_POLY = 0x82F63B78


def crc32c(data: bytes) -> int:
    """CRC-32C, the Castagnoli polynomial. zlib.crc32 is the other one."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (CRC32C_POLY if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


def body_for(keys):
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<Delete xmlns="http://s3.amazonaws.com/doc/2006-03-01/">']
    for key in keys:
        parts.append(f"<Object><Key>{key}</Key></Object>")
    parts.append("<Quiet>false</Quiet></Delete>")
    return "".join(parts).encode("utf-8")


def variants(body):
    """The four integrity headers, keyed by the name reported to Oracle."""
    return {
        "Content-MD5":
            ("Content-MD5", base64.b64encode(hashlib.md5(body).digest()).decode()),
        "x-amz-checksum-crc32c":
            ("x-amz-checksum-crc32c",
             base64.b64encode(crc32c(body).to_bytes(4, "big")).decode()),
        "x-amz-checksum-sha256":
            ("x-amz-checksum-sha256",
             base64.b64encode(hashlib.sha256(body).digest()).decode()),
        "x-amz-checksum-crc32":
            ("x-amz-checksum-crc32",
             base64.b64encode(zlib.crc32(body).to_bytes(4, "big")).decode()),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    p.add_argument("--endpoint", required=True)
    p.add_argument("--region", required=True)
    p.add_argument("--bucket", required=True)
    p.add_argument("--credentials", required=True,
                   help="JSON holding s3.access_key_id and s3.secret_access_key")
    args = p.parse_args()

    with open(args.credentials, encoding="utf-8") as handle:
        creds = json.load(handle)["s3"]

    body = body_for(["does-not-exist/probe-a", "does-not-exist/probe-b"])
    scheme, _, host = args.endpoint.partition("://")
    host = host.rstrip("/")
    canonical_uri = "/" + urllib.parse.quote(args.bucket, safe="")
    canonical_query = "delete="
    payload_sha256 = hashlib.sha256(body).hexdigest()

    print(f"endpoint : {args.endpoint}")
    print(f"bucket   : {args.bucket}")
    print(f"region   : {args.region}")
    print(f"body     : {len(body)} bytes, sha256 {payload_sha256}")
    print()

    for label, (header_name, header_value) in variants(body).items():
        now = dt.datetime.now(dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        headers = {
            "host": host,
            "x-amz-content-sha256": payload_sha256,
            "x-amz-date": amz_date,
            "content-type": "application/xml",
            header_name.lower(): header_value,
        }
        headers["Authorization"] = authorization(
            creds["access_key_id"], creds["secret_access_key"], "POST",
            canonical_uri, canonical_query,
            {k: v for k, v in headers.items() if k != "Authorization"},
            payload_sha256, args.region, "s3", amz_date)

        url = f"{scheme}://{host}{canonical_uri}?{canonical_query}"
        request = urllib.request.Request(url, data=body, headers=headers,
                                         method="POST")
        sent_utc = dt.datetime.now(dt.timezone.utc).isoformat()
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status = response.status
                request_id = (response.headers.get("opc-request-id")
                              or response.headers.get("x-amz-request-id") or "")
                detail = ""
        except urllib.error.HTTPError as problem:
            status = problem.code
            request_id = (problem.headers.get("opc-request-id")
                          or problem.headers.get("x-amz-request-id") or "")
            detail = problem.read()[:400].decode("utf-8", "replace")

        verdict = "ACCEPTED" if status == 200 else "REJECTED"
        print(f"{label:<24} {status} {verdict}")
        print(f"  sent (UTC)      : {sent_utc}")
        print(f"  opc-request-id  : {request_id}")
        if detail:
            print(f"  response        : {detail.strip()[:300]}")
        print()


if __name__ == "__main__":
    main()
