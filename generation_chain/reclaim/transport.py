"""The one place in this project authorised to send something other than
GET or HEAD.

`sources/http_reads.py` asserts every request it sends is a GET or a HEAD,
and that stays true: this module does not import `HttpReader`, does not touch
`ALLOWED_METHODS`, and is never imported by anything under `derivation/`,
`sources/` or `reporting/`. It is a separate, small, independently reviewable
path that exists to send exactly one kind of request: a signed `POST
/<bucket>?delete` batch delete, built from a body `batch.py` already rendered
and a checksum `checksum.py` already computed over that same body.

Retried only on the statuses a retry can fix. `DeleteObjects` is idempotent,
deleting an already-deleted key answers success again, so retrying a batch
that timed out or hit a throttle is safe. A 400 means the request itself is
wrong, most often the checksum this store wants is not the one it was given,
and retrying it spends the backoff to reach the same 400. A 403 is a
credential, and retrying it does not fix a credential either.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping, Tuple

from ..errors import GenerationChainError
from ..sources.s3 import S3Credentials
from ..sources.signing import sigv4

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# The only schemes this module speaks. urllib opens an ftp:// or a file:// URL
# through the same call, so an endpoint naming one is either a typo or
# something steering this process at a target it was never pointed at.
# Checked here as well as where the endpoint is parsed, because this is the
# request that deletes and it is the last place before the send.
ALLOWED_SCHEMES = frozenset({"https", "http"})


class TransportError(GenerationChainError):
    """The batch delete request could not be completed against the store."""


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_seconds: float = 1.0
    growth_factor: float = 2.0
    max_sleep_seconds: float = 20.0


def _refuse_unsendable_target(scheme: str, host: str) -> None:
    """Refuse a scheme or a host this module will not send a delete to.

    The host is copied into the `Host` header and signed, so whitespace or a
    control character in it would let the rest of the string be read as
    another header. Userinfo is refused for a quieter reason: urllib strips
    `user@` before connecting and this module would sign a Host the store
    never sees, which fails as a bare 403 that reads like a bad credential.
    """
    if scheme not in ALLOWED_SCHEMES:
        raise TransportError(
            f"{scheme!r} is not a scheme this tool sends a delete over; "
            f"expected {' or '.join(sorted(ALLOWED_SCHEMES))}. Nothing was "
            "sent")
    if not host:
        raise TransportError("the endpoint names no host. Nothing was sent")
    if "@" in host:
        raise TransportError(
            f"the endpoint host {host!r} carries userinfo, which would be "
            "signed and then dropped before the connection. Nothing was sent")
    if any(character.isspace() or ord(character) < 0x20 for character in host):
        raise TransportError(
            f"the endpoint host {host!r} holds whitespace or a control "
            "character, which would let the rest of it be read as another "
            "header. Nothing was sent")


def _signed_headers(host: str, amz_date: str, payload_sha256: str,
                    credentials: S3Credentials, region: str, canonical_uri: str,
                    canonical_query: str) -> Mapping[str, str]:
    headers = {
        "Host": host,
        "X-Amz-Date": amz_date,
        "X-Amz-Content-Sha256": payload_sha256,
    }
    headers["Authorization"] = sigv4.authorization(
        access_key=credentials.access_key,
        secret_key=credentials.secret_key.reveal(), method="POST",
        canonical_uri=canonical_uri, canonical_query=canonical_query,
        headers=headers, payload_sha256=payload_sha256, region=region,
        service="s3", amz_date=amz_date)
    return headers


def send_batch_delete(*, scheme: str, host: str, region: str, bucket: str,
                      credentials: S3Credentials, body: bytes,
                      checksum: Tuple[str, str], timeout: float,
                      policy: RetryPolicy = RetryPolicy(),
                      opener: Callable = urllib.request.urlopen,
                      sleep: Callable[[float], None] = time.sleep,
                      jitter: Callable[[], float] = random.random) -> bytes:
    """POST the batch delete `body` and return the store's response bytes.

    `checksum` is `(header_name, header_value)` from `checksum.checksum_header`,
    computed by the caller over this exact `body`. This function only attaches
    it; it never recomputes a checksum from `body`, which would reopen the
    same scan-versus-send gap a checksum is supposed to close.
    """
    _refuse_unsendable_target(scheme, host)
    canonical_uri = f"/{sigv4.quote_path(bucket)}"
    canonical_query = sigv4.canonical_query({"delete": ""})
    payload_sha256 = hashlib.sha256(body).hexdigest()
    url = f"{scheme}://{host}{canonical_uri}?{canonical_query}"

    last_detail = ""
    for attempt in range(policy.max_attempts):
        now = dt.datetime.now(dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        headers = dict(_signed_headers(
            host, amz_date, payload_sha256, credentials, region,
            canonical_uri, canonical_query))
        headers["Content-Type"] = "application/xml"
        headers[checksum[0]] = checksum[1]
        request = urllib.request.Request(url, data=body, headers=headers,
                                         method="POST")
        try:
            with opener(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = _detail(exc)
            last_detail = f"{exc.code} from {url}: {detail}"
            if exc.code not in RETRY_STATUSES:
                raise TransportError(last_detail) from exc
        except Exception as exc:
            # Deliberately broad, for the same reason http_reads.py catches
            # broadly: a truncated response or a socket error must become a
            # TransportError, never a bare traceback that skips the retry
            # loop and leaves the caller unsure whether anything was sent.
            last_detail = f"cannot reach {url}: {type(exc).__name__}: {exc}"
        if attempt + 1 >= policy.max_attempts:
            break
        sleep(jitter() * min(policy.max_sleep_seconds,
                             policy.base_seconds
                             * policy.growth_factor ** attempt))
    raise TransportError(last_detail or f"no answer from {url}")


def _detail(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read()[:400].decode("utf-8", "replace")
    except (OSError, AttributeError):
        return ""
