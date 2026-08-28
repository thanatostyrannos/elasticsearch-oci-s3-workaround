"""The Amazon S3 compatibility path, for MinIO, AWS and Oracle alike.

Oracle publishes two hostnames for this API and this module derives neither.
Picking the wrong one fails as a connection error or a bare 403, which reads
like a network problem or a credential problem, so the endpoint is always
named by the operator and the command line prompt says both forms out loud.
"""

from __future__ import annotations

import datetime as dt
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..credentials import Secret, as_secret
from ..errors import SourceReadError
from .http_reads import ALLOWED_METHODS, DEFAULT_TIMEOUT_SECONDS, HttpReader
from .signing import sigv4

LIST_NAMESPACE = "{http://s3.amazonaws.com/doc/2006-03-01/}"
MAX_KEYS_PER_PAGE = 1000
# A repository big enough to need this many pages is bigger than any this
# project has seen, and an endpoint that pages forever is a fault rather than
# a large bucket.
MAX_PAGES = 100_000

STANDARD_ORACLE_ENDPOINT = (
    "https://<namespace>.compat.objectstorage.<region>.oraclecloud.com")
DEDICATED_ORACLE_ENDPOINT = (
    "https://<namespace>.compat.objectstorage.<region>.oci.customer-oci.com")


@dataclass(frozen=True)
class S3Credentials:
    access_key: str
    secret_key: Secret

    def __post_init__(self) -> None:
        # Coerced rather than merely annotated, so no caller can hand this a
        # bare string that then renders itself in an error message.
        object.__setattr__(self, "secret_key", as_secret(self.secret_key))



# Loopback is exempt because there is no network path to intercept, and the
# offline suite serves plain HTTP there. Everything else has to be asked for.
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _refuse_plain_http(parsed, endpoint: str, allowed: bool) -> None:
    if parsed.scheme == "https" or allowed:
        return
    host = parsed.netloc.rsplit("@", 1)[-1]
    if host.rsplit(":", 1)[0] in _LOOPBACK or host in _LOOPBACK:
        return
    raise SourceReadError(
        f"the endpoint {endpoint!r} is plain {parsed.scheme}. A manifest names "
        "exactly which production objects are about to be deleted, and this "
        "would send it, and the signed request carrying it, in the clear. Use "
        "https, or pass --insecure-http if you meant a lab store on a network "
        "you trust.")


# A legitimate S3 listing or delete response never declares a DOCTYPE. stdlib
# ElementTree expands internal entities, measured on Python 3.12: a short
# billion-laughs body reaching 30,000 characters. This parser feeds the
# enumeration that decides what gets condemned, so a response able to hang it
# sits on the one path into the delete pipeline.
#
# Refused rather than parsed with limits, and refused before parsing rather
# than after, because there is nothing to weigh up: a store that answers with
# a DOCTYPE is answering something a store does not send.
#
# External entities are NOT the concern here. ElementTree resolves none, tested
# on this runtime, so a rule that flags this as an XXE file read is overstating
# it. The denial of service is real; the disclosure is not.
_DOCTYPE = b"<!DOCTYPE"


def refuse_doctype(body: bytes, what: str) -> None:
    if _DOCTYPE in body[:2048].lstrip():
        raise SourceReadError(
            f"the {what} declares a DOCTYPE. A store does not send one, and "
            "entity expansion inside it can be made to exhaust this process, "
            "so it is refused rather than parsed")


def parse_listing_body(body: bytes):
    """Parse a listing response, refusing one that carries a DOCTYPE."""
    refuse_doctype(body, "listing")
    try:
        return ET.fromstring(body)
    except ET.ParseError as exc:
        raise SourceReadError(f"the listing is not XML: {exc}") from exc


def _entry_size(contents: ET.Element) -> Optional[int]:
    """Stored bytes for one listing entry, or None when the store did not say.

    A missing or unparseable Size is left out rather than guessed. The report
    counts what it could not size and calls its total a floor, which is the
    honest direction for a number an operator quotes upward.
    """
    raw = contents.findtext(f"{LIST_NAMESPACE}Size")
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _continuation_token(tree: ET.Element) -> Optional[str]:
    """The token for the next page, or None when this page was the last.

    A store that says it is truncated and names no token has ended the
    listing early. Reading that as the end returns a repository smaller than
    it is, and every generation and blob past that point silently does not
    exist as far as the run is concerned.
    """
    truncated = tree.findtext(f"{LIST_NAMESPACE}IsTruncated", "false")
    if truncated.strip().lower() != "true":
        return None
    token = tree.findtext(f"{LIST_NAMESPACE}NextContinuationToken")
    if not token:
        raise SourceReadError(
            "the listing says it is truncated and names no continuation "
            "token")
    return token


class S3CompatibleSource:
    """Reads one repository over the S3 compatibility API, path style."""

    def __init__(self, endpoint: str, region: str, bucket: str,
                 credentials: S3Credentials, prefix: str = "",
                 timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 reader: Optional[HttpReader] = None,
                 allow_plain_http: bool = False) -> None:
        parsed = urllib.parse.urlsplit(endpoint)
        if not parsed.scheme or not parsed.netloc:
            raise SourceReadError(
                f"the endpoint {endpoint!r} is not a URL; Oracle publishes "
                f"{STANDARD_ORACLE_ENDPOINT} and {DEDICATED_ORACLE_ENDPOINT}")
        _refuse_plain_http(parsed, endpoint, allow_plain_http)
        self.scheme = parsed.scheme
        self.host = parsed.netloc
        self.region = region
        self.bucket = bucket
        self.prefix = (prefix.strip("/") + "/") if prefix.strip("/") else ""
        self.credentials = credentials
        self.timeout = timeout
        self.reader = reader or HttpReader()
        # Filled by list_keys from the same response the keys come from.
        self._sizes: Dict[str, int] = {}

    def describe(self) -> str:
        return (f"S3 compatibility API at {self.scheme}://{self.host}, bucket "
                f"{self.bucket}, prefix {self.prefix or '(none)'}, region "
                f"{self.region}")

    # -- transport --------------------------------------------------------

    def _request(self, method: str, canonical_uri: str,
                 params: Dict[str, Optional[str]]) -> bytes:
        assert method in ALLOWED_METHODS, (
            f"{method} is not a method this package may send; version one "
            "reads and never deletes")
        now = dt.datetime.now(dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        query = sigv4.canonical_query(params)
        headers = {
            "Host": self.host,
            "X-Amz-Date": amz_date,
            "X-Amz-Content-Sha256": sigv4.EMPTY_PAYLOAD_SHA256,
        }
        headers["Authorization"] = sigv4.authorization(
            access_key=self.credentials.access_key,
            secret_key=self.credentials.secret_key.reveal(),
            method=method, canonical_uri=canonical_uri,
            canonical_query=query, headers=headers,
            payload_sha256=sigv4.EMPTY_PAYLOAD_SHA256,
            region=self.region, service="s3", amz_date=amz_date)
        url = f"{self.scheme}://{self.host}{canonical_uri}"
        if query:
            url += "?" + query
        return self.reader.get(url, headers, method=method,
                               timeout=self.timeout).body

    # -- the source interface ---------------------------------------------

    def sizes(self) -> Dict[str, int]:
        """Stored bytes per key, as the listing reported them.

        Populated by `list_keys`, because `Size` is a sibling of `Key` in every
        ListObjectsV2 entry and costs nothing extra to read. Empty before a
        listing has run. A HEAD per object would answer the same question at
        one request per key, which is the shape of the fault this tool exists
        to work around.
        """
        return dict(self._sizes)

    def list_keys(self) -> List[str]:
        keys: List[str] = []
        self._sizes = {}
        token: Optional[str] = None
        for _ in range(MAX_PAGES):
            body = self._request("GET", f"/{self.bucket}", {
                "list-type": "2",
                "prefix": self.prefix or None,
                "max-keys": str(MAX_KEYS_PER_PAGE),
                "encoding-type": "url",
                "continuation-token": token,
            })
            page, token = self._page(body)
            keys.extend(page)
            if token is None:
                return sorted(keys)
        raise SourceReadError(
            f"the listing did not finish in {MAX_PAGES} pages")

    def _page(self, body: bytes):
        """One listing page: its keys under this prefix, and the next token."""
        tree = parse_listing_body(body)
        return self._page_keys(tree), _continuation_token(tree)

    def _page_keys(self, tree: ET.Element) -> List[str]:
        """The keys on this page that belong to this repository.

        Sizes are recorded on the way past, from the same entry the key came
        from. A key outside the prefix belongs to another repository sharing
        the bucket, and it is dropped here rather than carried as a None.
        """
        decode = self._decoder(tree)
        keys: List[str] = []
        for contents in tree.findall(f"{LIST_NAMESPACE}Contents"):
            relative = self._entry_key(contents, decode)
            if relative is None:
                continue
            keys.append(relative)
            size = _entry_size(contents)
            if size is not None:
                self._sizes[relative] = size
        return keys

    def _entry_key(self, contents: ET.Element, decode) -> Optional[str]:
        """One entry's key relative to the prefix, or None if it is outside."""
        element = contents.find(f"{LIST_NAMESPACE}Key")
        if element is None or element.text is None:
            raise SourceReadError("a listing entry carries no key")
        return self._relative(decode(element.text))

    @staticmethod
    def _decoder(tree: ET.Element):
        """Decode keys only when the store says it encoded them.

        Assuming url encoding on a store that ignored the parameter turns a
        key holding a plus sign into a different key. Assuming plain text on a
        store that honoured it does the same in the other direction, so this
        follows what the response says rather than what was asked for.
        """
        echoed = tree.findtext(f"{LIST_NAMESPACE}EncodingType", "")
        if echoed.strip().lower() == "url":
            return lambda value: urllib.parse.unquote_plus(value)
        return lambda value: value

    def _relative(self, key: str) -> Optional[str]:
        if not key.startswith(self.prefix):
            return None
        relative = key[len(self.prefix):]
        return relative or None

    def fetch(self, key: str) -> bytes:
        path = sigv4.quote_path(self.prefix + key)
        return self._request("GET", f"/{self.bucket}/{path}", {})

    def exists(self, key: str) -> bool:
        path = sigv4.quote_path(self.prefix + key)
        try:
            self._request("HEAD", f"/{self.bucket}/{path}", {})
        except SourceReadError as exc:
            if " 404 " in f" {exc} ":
                return False
            raise
        return True
