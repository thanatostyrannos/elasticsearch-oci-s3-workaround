"""Put objects into a MinIO bucket, fast enough to reach page depth.

Signing reuses the tool's own sigv4 module, so the bucket the listing bench
reads was written by the same signer the listing bench uses. PUT never goes
near the tool's HttpReader, which allows GET and HEAD only.
"""
from __future__ import annotations

import concurrent.futures as futures
import datetime as dt
import hashlib
import os
import sys
import time
import urllib.error
import urllib.request

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import use_tool
use_tool()
from generation_chain.sources.signing import sigv4

ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://127.0.0.1:19000")
ACCESS = os.environ.get("MINIO_ACCESS", "minioadmin")
SECRET = os.environ.get("MINIO_SECRET", "minioadmin123")
REGION = os.environ.get("MINIO_REGION", "us-east-1")


def _send(method: str, path: str, body: bytes = b"", query: str = ""):
    host = ENDPOINT.split("//", 1)[1]
    now = dt.datetime.now(dt.timezone.utc)
    amz = now.strftime("%Y%m%dT%H%M%SZ")
    payload = hashlib.sha256(body).hexdigest()
    headers = {"Host": host, "X-Amz-Date": amz, "X-Amz-Content-Sha256": payload}
    headers["Authorization"] = sigv4.authorization(
        access_key=ACCESS, secret_key=SECRET, method=method,
        canonical_uri=path, canonical_query=query, headers=headers,
        payload_sha256=payload, region=REGION, service="s3", amz_date=amz)
    url = ENDPOINT + path + (("?" + query) if query else "")
    request = urllib.request.Request(url, data=body or None, method=method)
    for name, value in headers.items():
        request.add_header(name, value)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.status, response.read()


def make_bucket(bucket: str) -> None:
    try:
        _send("PUT", f"/{bucket}")
    except urllib.error.HTTPError as exc:
        if exc.code not in (409,):
            body = exc.read()[:200]
            if b"BucketAlreadyOwnedByYou" not in body and b"BucketAlreadyExists" not in body:
                raise


def put_many(bucket: str, objects, workers: int = 48) -> float:
    """Upload {key: bytes}. Returns seconds. Skips nothing; overwrite is fine."""
    started = time.perf_counter()
    def one(item):
        key, data = item
        _send("PUT", f"/{bucket}/{sigv4.quote_path(key)}", data)
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in pool.map(one, objects.items()):
            pass
    return time.perf_counter() - started


def delete_bucket_contents(bucket: str, keys) -> None:
    def one(key):
        try:
            _send("DELETE", f"/{bucket}/{sigv4.quote_path(key)}")
        except urllib.error.HTTPError:
            pass
    with futures.ThreadPoolExecutor(max_workers=48) as pool:
        for _ in pool.map(one, keys):
            pass


if __name__ == "__main__":
    make_bucket(sys.argv[1] if len(sys.argv) > 1 else "scale-limits")
    print("bucket ready")
