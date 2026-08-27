"""The OCI native Object Storage path.

Oracle's own API rather than the S3 compatibility layer, signed with an RSA
key. Two calls are used: ListObjects to enumerate the repository and
GetObject to read one blob.

No OCI endpoint exists in this project's test environment, so this transport's
correctness rests on the offline known-answer vector in
tests/test_generation_chain_signing.py and on the in-process rig in
tests/test_generation_chain_transports.py, which verifies each signature
against the public half of the test key. Nothing here has been exercised
against a real tenancy, and that is stated rather than implied.
"""

from __future__ import annotations

import configparser
import datetime as dt
import email.utils
import json
import os
import urllib.parse
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..errors import GenerationChainError, SourceReadError
from .http_reads import ALLOWED_METHODS, DEFAULT_TIMEOUT_SECONDS, HttpReader, Response
from .signing import oci_signature
from .signing.rsa import RsaPrivateKey

MAX_KEYS_PER_PAGE = 1000
# Only the realms whose second-level domain is not oraclecloud.com. Anything
# missing here falls back, and an operator in a realm nobody enumerated needs
# the override rather than a table this package cannot keep current.
REALM_DOMAINS = {
    "us-langley-1": "oraclegovcloud.com",
    "us-luke-1": "oraclegovcloud.com",
    "us-gov-ashburn-1": "oraclegovcloud.com",
    "us-gov-chicago-1": "oraclegovcloud.com",
    "us-gov-phoenix-1": "oraclegovcloud.com",
    "uk-gov-london-1": "oraclegovcloud.uk",
    "uk-gov-cardiff-1": "oraclegovcloud.uk",
}
DEFAULT_REALM_DOMAIN = "oraclecloud.com"
ENDPOINT_ENV_VAR = "OCI_OBJECTSTORAGE_ENDPOINT"
MAX_PAGES = 100_000
CONFIG_ENV_VAR = "OCI_CONFIG_FILE"
DEFAULT_CONFIG = "~/.oci/config"
API_KEY_FIELDS = ("user", "fingerprint", "tenancy", "key_file")


def endpoint_for_region(region: str, override: Optional[str] = None) -> str:
    """The Object Storage host for a region, or whatever the operator named.

    The override takes a bare host or a pasted URL, because an operator in a
    dedicated region or a sovereign cloud has a hostname this table will never
    hold, and without a working escape hatch they cannot use the tool at all.
    """
    named = override or os.environ.get(ENDPOINT_ENV_VAR)
    if named:
        if "//" not in named:
            return named.strip("/")
        return urllib.parse.urlsplit(named).netloc
    domain = REALM_DOMAINS.get(region, DEFAULT_REALM_DOMAIN)
    return f"objectstorage.{region}.{domain}"


class OciConfigError(GenerationChainError):
    """The profile or the key is unusable, named precisely enough to fix."""


@dataclass(frozen=True)
class OciCredentials:
    key_id: str
    private_key: RsaPrivateKey

    @classmethod
    def from_profile(cls, path: Optional[str] = None,
                     profile: str = "DEFAULT") -> "OciCredentials":
        """Read an api-key profile out of an `~/.oci/config`-style file."""
        location = os.path.expanduser(
            path or os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG)
        parser = configparser.ConfigParser()
        if not parser.read(location):
            raise OciConfigError(f"cannot read the OCI config at {location}")
        if profile not in parser:
            raise OciConfigError(
                f"{location} has no profile named {profile!r}")
        section = parser[profile]
        missing = [f for f in API_KEY_FIELDS if not section.get(f)]
        if missing:
            raise OciConfigError(
                f"profile {profile!r} in {location} is missing "
                f"{', '.join(missing)}")
        key_path = os.path.expanduser(section["key_file"])
        try:
            with open(key_path, "rb") as handle:
                pem = handle.read()
        except OSError as exc:
            raise OciConfigError(
                f"cannot read the private key at {key_path}: {exc}") from exc
        return cls(
            key_id=f"{section['tenancy']}/{section['user']}/{section['fingerprint']}",
            private_key=RsaPrivateKey.from_pem(pem))


class OciNativeSource:
    """Reads one repository over Oracle's native Object Storage API."""

    def __init__(self, endpoint: str, namespace: str, bucket: str,
                 credentials: OciCredentials, prefix: str = "",
                 timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 reader: Optional[HttpReader] = None) -> None:
        parsed = urllib.parse.urlsplit(endpoint)
        if not parsed.scheme or not parsed.netloc:
            raise SourceReadError(f"the endpoint {endpoint!r} is not a URL")
        self.scheme = parsed.scheme
        self.host = parsed.netloc
        self.namespace = namespace
        self.bucket = bucket
        self.prefix = (prefix.strip("/") + "/") if prefix.strip("/") else ""
        self.credentials = credentials
        self.timeout = timeout
        self.reader = reader or HttpReader()
        # Filled by list_keys from the same response the keys come from.
        self._sizes: Dict[str, int] = {}

    def describe(self) -> str:
        return (f"OCI native Object Storage at {self.scheme}://{self.host}, "
                f"namespace {self.namespace}, bucket {self.bucket}, prefix "
                f"{self.prefix or '(none)'}")

    # -- transport --------------------------------------------------------

    def _base_path(self) -> str:
        return (f"/n/{oci_signature.quote_segment(self.namespace)}"
                f"/b/{oci_signature.quote_segment(self.bucket)}/o")

    def _request(self, method: str, path_and_query: str) -> Response:
        assert method in ALLOWED_METHODS, (
            f"{method} is not a method this package may send; version one "
            "reads and never deletes")
        date_header = email.utils.format_datetime(
            dt.datetime.now(dt.timezone.utc), usegmt=True)
        signature = self.credentials.private_key.sign_sha256(
            oci_signature.signing_string(method, path_and_query, self.host,
                                         date_header))
        headers = {
            "Date": date_header,
            "Host": self.host,
            "Authorization": oci_signature.authorization_header(
                self.credentials.key_id, signature),
        }
        return self.reader.get(
            f"{self.scheme}://{self.host}{path_and_query}", headers,
            method=method, timeout=self.timeout)

    # -- the source interface ---------------------------------------------

    def sizes(self) -> Dict[str, int]:
        """Stored bytes per key, as the listing reported them.

        The listing asks for `name,size`, so this costs no extra request.
        Empty before a listing has run.
        """
        return dict(self._sizes)

    def list_keys(self) -> List[str]:
        keys: List[str] = []
        self._sizes = {}
        start: Optional[str] = None
        for _ in range(MAX_PAGES):
            query = oci_signature.query_string({
                "prefix": self.prefix or None,
                "limit": str(MAX_KEYS_PER_PAGE),
                "start": start,
                "fields": "name,size",
            })
            response = self._request(
                "GET", self._base_path() + (f"?{query}" if query else ""))
            page, following = self._page(response)
            keys.extend(page)
            if following is None:
                return sorted(keys)
            if following == start:
                # Abuse case seen in the wild: a store that answers every page
                # with the same marker. Following it forever would burn the
                # retry budget on one page and never say why.
                raise SourceReadError(
                    "the listing repeated its next-page marker")
            start = following
        raise SourceReadError(
            f"the listing did not finish in {MAX_PAGES} pages")

    def _page(self, response: Response):
        body = response.body
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceReadError(f"the listing is not JSON: {exc}") from exc
        if not isinstance(document, dict) or \
                not isinstance(document.get("objects"), list):
            raise SourceReadError("the listing carries no objects array")
        keys = []
        for entry in document["objects"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise SourceReadError("a listing entry carries no name")
            relative = self._relative(entry["name"])
            if relative is not None:
                keys.append(relative)
                # A missing or unparseable size is left out rather than
                # guessed. The report counts what it could not size and calls
                # its total a floor.
                raw = entry.get("size")
                if isinstance(raw, int):
                    self._sizes[relative] = raw
        following = document.get("nextStartWith")
        if following is None:
            # Oracle carries the marker in a header when the body omits it.
            # A transport that only read the body stops after page one and
            # reports a repository far smaller than it is.
            following = response.header("opc-next-page")
        if following is not None and not isinstance(following, str):
            raise SourceReadError("the listing's next page marker is not a string")
        return keys, following

    def _relative(self, key: str) -> Optional[str]:
        if not key.startswith(self.prefix):
            return None
        return key[len(self.prefix):] or None

    def fetch(self, key: str) -> bytes:
        path = oci_signature.quote_segment(self.prefix + key)
        return self._request("GET", f"{self._base_path()}/{path}").body

    def exists(self, key: str) -> bool:
        path = oci_signature.quote_segment(self.prefix + key)
        try:
            self._request("HEAD", f"{self._base_path()}/{path}")
        except SourceReadError as exc:
            if " 404 " in f" {exc} ":
                return False
            raise
        return True
