"""AWS Signature Version 4, for any S3-compatible endpoint.

Oracle's Amazon S3 Compatibility API takes these signatures, and so does
MinIO, so one implementation covers both. Every request this package sends is
a GET or a HEAD with an empty body, which is why there is no payload streaming
here and why the payload hash is a constant.
"""

from __future__ import annotations

import hashlib
import hmac
import urllib.parse
from typing import Dict, Mapping, Optional

ALGORITHM = "AWS4-HMAC-SHA256"
EMPTY_PAYLOAD_SHA256 = hashlib.sha256(b"").hexdigest()

# RFC 3986 unreserved characters. S3 percent-encodes everything else in a key,
# and leaves the tilde alone, which several standard library helpers do not.
UNRESERVED_KEY = "-_.~"


def quote_path(key: str) -> str:
    """Percent-encode an object key for the path, keeping separators.

    Encoded ONCE. A key already holding a percent sign encodes to `%25`, and a
    signer that encoded the result again would sign a request for a different
    object from the one it fetches.
    """
    return urllib.parse.quote(key, safe="/" + UNRESERVED_KEY)


def quote_strict(value: str) -> str:
    """Percent-encode a query component, slashes included."""
    return urllib.parse.quote(value, safe=UNRESERVED_KEY)


def canonical_query(params: Mapping[str, Optional[str]]) -> str:
    """Sorted by encoded name, every value encoded, empty values kept."""
    pairs = [(quote_strict(name), quote_strict(value or ""))
             for name, value in params.items() if value is not None]
    return "&".join(f"{name}={value}" for name, value in sorted(pairs))


def _normalise(headers: Mapping[str, str]) -> Dict[str, str]:
    """Lowercase names, collapse whitespace runs in values, trim the edges."""
    return {name.lower(): " ".join(str(value).split())
            for name, value in headers.items()}


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key: str, datestamp: str, region: str,
                service: str) -> bytes:
    key = _hmac(("AWS4" + secret_key).encode("utf-8"), datestamp)
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, "aws4_request")


def authorization(access_key: str, secret_key: str, method: str,
                  canonical_uri: str, canonical_query: str,
                  headers: Mapping[str, str], payload_sha256: str,
                  region: str, service: str, amz_date: str) -> str:
    """The complete Authorization header value for one request."""
    lowered = _normalise(headers or {})
    signed = ";".join(sorted(lowered))
    canonical_headers = "".join(f"{name}:{lowered[name]}\n"
                                for name in sorted(lowered))
    canonical_request = "\n".join([
        method, canonical_uri, canonical_query, canonical_headers,
        signed, payload_sha256,
    ])
    datestamp = amz_date[:8]
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    to_sign = "\n".join([
        ALGORITHM, amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(signing_key(secret_key, datestamp, region, service),
                         to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return (f"{ALGORITHM} Credential={access_key}/{scope}, "
            f"SignedHeaders={signed}, Signature={signature}")
