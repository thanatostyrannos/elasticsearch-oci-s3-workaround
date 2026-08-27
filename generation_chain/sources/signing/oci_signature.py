"""Oracle's request signature, version 1, for a body-less request.

Three headers get signed, in Oracle's order rather than alphabetically, and
the order is part of the signature rather than a presentation detail. Every
request this package sends is a GET with no body, so there is no
`x-content-sha256` and no `content-length` to sign.
"""

from __future__ import annotations

import base64
import urllib.parse
from typing import Mapping, Optional, Sequence

SIGNED_HEADERS: Sequence[str] = ("date", "(request-target)", "host")
AUTHORIZATION_TEMPLATE = (
    'Signature algorithm="rsa-sha256",headers="{headers}",keyId="{key_id}",'
    'signature="{signature}",version="1"'
)


def signing_string(method: str, path_and_query: str, host: str,
                   date_header: str) -> bytes:
    """The exact bytes that get signed."""
    values = {
        "date": date_header,
        "(request-target)": f"{method.lower()} {path_and_query}",
        "host": host,
    }
    return "\n".join(f"{name}: {values[name]}"
                     for name in SIGNED_HEADERS).encode("ascii")


def authorization_header(key_id: str, signature: bytes) -> str:
    return AUTHORIZATION_TEMPLATE.format(
        headers=" ".join(SIGNED_HEADERS), key_id=key_id,
        signature=base64.b64encode(signature).decode("ascii"))


def quote_segment(value: str) -> str:
    """Percent-encode one path segment, leaving separators alone.

    Oracle's SDK quotes with a default safe set of "/", so a slash inside an
    object name stays a slash on the wire and `indices/abc/0/__1` arrives as
    four path segments. Matching that is not a style choice: encoding the
    slash reads a different object.
    """
    return urllib.parse.quote(value, safe="/")


def query_string(params: Mapping[str, Optional[str]]) -> str:
    """A space becomes %20, never a plus. A page token can hold both."""
    pairs = [(name, value) for name, value in params.items() if value is not None]
    if not pairs:
        return ""
    return urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote, safe="")
