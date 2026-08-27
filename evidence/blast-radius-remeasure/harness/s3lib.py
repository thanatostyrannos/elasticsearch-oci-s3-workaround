#!/usr/bin/env python3
"""Stdlib SigV4 S3 client for the blast-radius campaign.

Independent of the project's own sweeper code on purpose: an oracle that shares
code with the thing under test is not an oracle. Reads credentials from
env/ next to the harness so no key is ever typed into a command line.
"""
import datetime
import hashlib
import hmac
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(os.path.dirname(HERE), "env")

EP = os.environ.get("BLASTRM_S3_ENDPOINT", "http://127.0.0.1:19045")
REGION = "us-east-1"
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def _cred(name):
    with open(os.path.join(ENV, name)) as fh:
        return fh.read().strip()


AK = _cred("s3_access")
SK = _cred("s3_secret")


def _sign(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def request(method, bucket, key="", query=None, body=b"", extra=None):
    query = query or {}
    extra = extra or {}
    host = EP.split("://", 1)[1]
    path = "/" + bucket + ("/" + key if key else "")
    canon_uri = urllib.parse.quote(path, safe="/")
    items = sorted(
        (urllib.parse.quote(k, safe="-_.~"), urllib.parse.quote(v, safe="-_.~"))
        for k, v in query.items()
    )
    canon_q = "&".join(f"{k}={v}" for k, v in items)
    t = datetime.datetime.now(datetime.timezone.utc)
    amzdate = t.strftime("%Y%m%dT%H%M%SZ")
    datestamp = t.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amzdate}
    for k, v in extra.items():
        headers[k.lower()] = v
    signed = ";".join(sorted(headers))
    canon_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    creq = "\n".join([method, canon_uri, canon_q, canon_headers, signed, payload_hash])
    scope = f"{datestamp}/{REGION}/s3/aws4_request"
    sts = "\n".join(
        ["AWS4-HMAC-SHA256", amzdate, scope, hashlib.sha256(creq.encode()).hexdigest()]
    )
    k = _sign(("AWS4" + SK).encode(), datestamp)
    k = _sign(k, REGION)
    k = _sign(k, "s3")
    k = _sign(k, "aws4_request")
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={AK}/{scope}, "
        f"SignedHeaders={signed}, Signature={sig}"
    )
    url = EP + canon_uri + (("?" + canon_q) if canon_q else "")
    req = urllib.request.Request(
        url, data=body if body else None, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def list_all(bucket, prefix=""):
    out, token = [], None
    while True:
        q = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if token:
            q["continuation-token"] = token
        st, body = request("GET", bucket, "", q)
        if st != 200:
            raise SystemExit(f"list {bucket}/{prefix} -> {st}: {body[:400]}")
        root = ET.fromstring(body)
        for c in root.findall(NS + "Contents"):
            out.append(
                (
                    c.find(NS + "Key").text,
                    int(c.find(NS + "Size").text),
                    c.find(NS + "ETag").text.strip('"'),
                )
            )
        trunc = root.find(NS + "IsTruncated")
        if trunc is not None and trunc.text == "true":
            token = root.find(NS + "NextContinuationToken").text
        else:
            break
    return sorted(out)


def get(bucket, key):
    st, body = request("GET", bucket, key)
    if st != 200:
        raise SystemExit(f"get {key} -> {st}: {body[:300]}")
    return body


def put(bucket, key, data):
    return request("PUT", bucket, key, body=data)


def delete(bucket, key):
    return request("DELETE", bucket, key)


def mkbucket(b):
    return request("PUT", b, "")


def clone_prefix(bucket, src, dst):
    """Byte-for-byte copy of one prefix onto another. Every experiment starts
    from the same bytes, so a difference in outcome is the delete and nothing
    else."""
    n = 0
    for k, _, _ in list_all(bucket, src):
        rel = k[len(src):].lstrip("/")
        st, body = request("PUT", bucket, dst.rstrip("/") + "/" + rel, body=get(bucket, k))
        if st not in (200, 204):
            raise SystemExit(f"clone put {rel} -> {st}: {body[:300]}")
        n += 1
    return n


def purge(bucket, prefix):
    n = 0
    for k, _, _ in list_all(bucket, prefix):
        delete(bucket, k)
        n += 1
    return n


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "ls":
        for k, s, e in list_all(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ""):
            print(f"{k}\t{s}\t{e}")
    elif cmd == "get":
        sys.stdout.buffer.write(get(sys.argv[2], sys.argv[3]))
    elif cmd == "rm":
        print(delete(sys.argv[2], sys.argv[3])[0])
    elif cmd == "mkbucket":
        print(mkbucket(sys.argv[2])[0])
    elif cmd == "clone":
        print(clone_prefix(sys.argv[2], sys.argv[3], sys.argv[4]))
    elif cmd == "purge":
        print(purge(sys.argv[2], sys.argv[3]))
    elif cmd == "buckets":
        st, body = request("GET", "", "")
        print("\n".join(b.find(NS + "Name").text for b in ET.fromstring(body).iter(NS + "Bucket")))
