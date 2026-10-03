"""The Amazon S3 compatibility path, for MinIO, AWS and Oracle alike.

Oracle publishes two hostnames for this API and this module derives neither.
Picking the wrong one fails as a connection error or a bare 403, which reads
like a network problem or a credential problem, so the endpoint is always
named by the operator and the command line prompt says both forms out loud.
"""

from __future__ import annotations

import datetime as dt
import re
import urllib.parse
import xml.etree.ElementTree as ET
from xml.parsers import expat
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..body_limits import MAX_BLOB_BYTES, MAX_XML_BODY_BYTES
from ..credentials import Secret, as_secret
from ..errors import ForbiddenMethod, RunRefused, SourceReadError
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


_STORE_EXPOSURE = ("A manifest names exactly which production objects are "
                   "about to be deleted, and this would send it, and the "
                   "signed request carrying it, in the clear.")
_CLUSTER_EXPOSURE = ("Every request to the cluster carries its API key or "
                     "password, and this would send that credential in the "
                     "clear.")


def _refuse_plain_http(parsed, endpoint: str, allowed: bool,
                       exposure: str = _STORE_EXPOSURE) -> None:
    if parsed.scheme == "https" or allowed:
        return
    host = parsed.netloc.rsplit("@", 1)[-1]
    if host.rsplit(":", 1)[0] in _LOOPBACK or host in _LOOPBACK:
        return
    raise SourceReadError(
        f"the endpoint {endpoint!r} is plain {parsed.scheme}. {exposure} Use "
        "https, or pass --insecure-http if you meant a lab endpoint on a "
        "network you trust.")


def refuse_plain_http_cluster(endpoint: str, allowed: bool) -> None:
    """Apply the store's plain-http rule to the Elasticsearch endpoint."""
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme and parsed.netloc:
        _refuse_plain_http(parsed, endpoint, allowed, _CLUSTER_EXPOSURE)


# A legitimate S3 listing or delete response never declares a DOCTYPE. stdlib
# ElementTree expands internal entities, measured on Python 3.12: 3.3 KB of
# nested entity declarations reached 1,000,000 characters, and a 1 MB body
# reached 505 MB of resident memory before expat's amplification limit fired.
# This parser feeds the enumeration that decides what gets condemned, so a
# response able to hang it sits on the one path into the delete pipeline.
#
# Refused rather than parsed with limits, and refused before parsing rather
# than after, because there is nothing to weigh up: a store that answers with
# a DOCTYPE is answering something a store does not send.
#
# Two layers, because either one alone has failed before. refuse_doctype()
# scans the whole decoded body. parse_xml_body() then installs expat handlers
# that raise on the first DOCTYPE or entity declaration, so a spelling the
# scan misses still stops before any entity is stored.
#
# External entities are NOT the concern here. ElementTree resolves none, tested
# on this runtime, so a rule that flags this as an XXE file read is overstating
# it. The denial of service is real; the disclosure is not.


_DOCTYPE = "<!DOCTYPE"
_ENTITY = "<!ENTITY"
_XML_DECLARATION = re.compile(
    r"\A\ufeff?\s*<\?xml\s(?:(?!\?>).)*?encoding\s*=\s*(?P<quote>[\"'])(?P<name>.+?)"
    r"(?P=quote)", re.DOTALL)
_ACCEPTED_ENCODINGS = frozenset({"utf-8", "utf8", "us-ascii", "ascii"})


def refuse_doctype(body: bytes, what: str) -> None:
    """Refuse a body that is too large, not UTF-8, or declares a DOCTYPE.

    The scan runs on the decoded text, over the whole body, so a long comment
    or a different encoding cannot move a declaration out of view.
    """
    if len(body) > MAX_XML_BODY_BYTES:
        raise SourceReadError(
            f"the {what} is {len(body)} bytes, larger than the "
            f"{MAX_XML_BODY_BYTES} this tool reads; refused rather than "
            "parsed")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceReadError(
            f"the {what} is not UTF-8, and S3 and OCI answer UTF-8; refused "
            "rather than parsed") from exc
    declared = _XML_DECLARATION.match(text)
    if "\x00" in text or (
            declared and declared.group("name").lower() not in _ACCEPTED_ENCODINGS):
        raise SourceReadError(
            f"the {what} declares an encoding other than UTF-8, and S3 and "
            "OCI answer UTF-8; refused rather than parsed")
    if _DOCTYPE in text or _ENTITY in text:
        raise SourceReadError(
            f"the {what} declares a DOCTYPE or an entity. A store does not "
            "send one, and entity expansion inside it can be made to exhaust "
            "this process, so it is refused rather than parsed")


def parse_xml_body(body: bytes, what: str) -> ET.Element:
    """Parse a store response, refusing a DOCTYPE, entity or non-UTF-8 body.

    Raises SourceReadError for a refusal and for a body that is not XML.
    """
    refuse_doctype(body, what)

    def refuse_declaration(*_args):
        raise SourceReadError(
            f"the {what} declares a DOCTYPE or an entity, which a store does "
            "not send; refused rather than parsed")

    def qualified(name):
        # expat joins a namespace and a local name with the separator below;
        # ElementTree spells the same name {namespace}local.
        return "{" + name if "}" in name else name

    tree = ET.TreeBuilder()
    # stdlib's C XMLParser does not expose the expat object, so the handlers
    # are installed on a pyexpat parser that feeds a TreeBuilder directly.
    parser = expat.ParserCreate("utf-8", "}")
    parser.buffer_text = True
    parser.StartDoctypeDeclHandler = refuse_declaration
    parser.EntityDeclHandler = refuse_declaration
    parser.StartElementHandler = lambda name, attrs: tree.start(
        qualified(name), {qualified(k): v for k, v in attrs.items()})
    parser.EndElementHandler = lambda name: tree.end(qualified(name))
    parser.CharacterDataHandler = tree.data
    try:
        parser.Parse(body, True)
    except expat.ExpatError as exc:
        raise SourceReadError(f"the {what} is not XML: {exc}") from exc
    return tree.close()


def parse_listing_body(body: bytes):
    """Parse a listing response, refusing one that carries a DOCTYPE."""
    return parse_xml_body(body, "listing")


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
    truncated = tree.findtext(f"{LIST_NAMESPACE}IsTruncated")
    if truncated not in ("true", "false"):
        raise RunRefused(
            f"the listing page carries IsTruncated {truncated!r}, not true "
            "or false, so this run cannot tell whether the listing is "
            "complete")
    if truncated == "false":
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
                 params: Dict[str, Optional[str]],
                 critical: bool = False,
                 max_bytes: int = MAX_BLOB_BYTES) -> bytes:
        if method not in ALLOWED_METHODS:
            raise ForbiddenMethod(
                f"{method} is not a method this package may send; it reads "
                "and never deletes")
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
                               timeout=self.timeout, critical=critical,
                               max_bytes=max_bytes).body

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

    def list_keys(self, on_page=None) -> List[str]:
        """Every key under the prefix, sorted.

        `on_page`, when given, is called with the running key count after each
        page and may raise to stop the listing early.
        """
        keys: List[str] = []
        self._sizes = {}
        token: Optional[str] = None
        seen_tokens = set()
        for _ in range(MAX_PAGES):
            body = self._request("GET", f"/{self.bucket}", {
                "list-type": "2",
                "prefix": self.prefix or None,
                "max-keys": str(MAX_KEYS_PER_PAGE),
                "encoding-type": "url",
                "continuation-token": token,
            }, critical=True, max_bytes=MAX_XML_BODY_BYTES)
            page, token = self._page(body)
            keys.extend(page)
            if on_page is not None:
                on_page(len(keys))
            if token is None:
                return sorted(keys)
            if token in seen_tokens:
                raise SourceReadError(
                    "the listing repeated a continuation token")
            seen_tokens.add(token)
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

    def fetch_critical(self, key: str) -> bytes:
        path = sigv4.quote_path(self.prefix + key)
        return self._request("GET", f"/{self.bucket}/{path}", {},
                             critical=True)

    def exists(self, key: str) -> bool:
        path = sigv4.quote_path(self.prefix + key)
        try:
            self._request("HEAD", f"/{self.bucket}/{path}", {})
        except SourceReadError as exc:
            if " 404 " in f" {exc} ":
                return False
            raise
        return True
