"""What signing costs per request, on each endpoint transport.

The OCI native path signs every request with a pure-python RSA private key
operation. On a chain deep enough to need tens of thousands of requests, that
is CPU the tool spends before any byte leaves the host, and it is spent on the
jump host rather than in the store.
"""
from __future__ import annotations
import json, os, sys, time
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import base_parser, emit, table, use_tool
parser = base_parser(__doc__)
parser.add_argument("--samples", type=int, default=200)
parser.add_argument("--out", default="signing")
args = parser.parse_args()
use_tool(args.tool_root)

import ocistub
from generation_chain.sources.signing import sigv4, oci_signature

credentials = ocistub.credentials()
message = oci_signature.signing_string(
    "GET", "/n/ns/b/b/o/index-3", "127.0.0.1:1", "Mon, 01 Jan 2026 00:00:00 GMT")

t0 = time.perf_counter()
for _ in range(args.samples):
    credentials.private_key.sign_sha256(message)
oci_ms = (time.perf_counter() - t0) / args.samples * 1000

headers = {"Host": "h", "X-Amz-Date": "20260101T000000Z",
           "X-Amz-Content-Sha256": sigv4.EMPTY_PAYLOAD_SHA256}
t0 = time.perf_counter()
for _ in range(args.samples * 20):
    sigv4.authorization(access_key="a", secret_key="b", method="GET",
                        canonical_uri="/bucket/index-3", canonical_query="",
                        headers=headers,
                        payload_sha256=sigv4.EMPTY_PAYLOAD_SHA256,
                        region="us-east-1", service="s3",
                        amz_date="20260101T000000Z")
s3_ms = (time.perf_counter() - t0) / (args.samples * 20) * 1000

rows = [
    {"transport": "oci native (RSA-2048, pure python)",
     "ms_per_request_signature": round(oci_ms, 3),
     "cpu_minutes_at_50k_requests": round(oci_ms * 50000 / 1000 / 60, 2)},
    {"transport": "s3 compatibility (sigv4 HMAC)",
     "ms_per_request_signature": round(s3_ms, 4),
     "cpu_minutes_at_50k_requests": round(s3_ms * 50000 / 1000 / 60, 3)},
]
print(table(rows, ["transport", "ms_per_request_signature",
                   "cpu_minutes_at_50k_requests"]))
print(emit(args.out, rows))
