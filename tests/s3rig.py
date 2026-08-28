#!/usr/bin/env python3
"""A small S3 server the offline suite can point the package's transport at.

A transport is the part of a tool nobody can review by reading. A
canonical request that is one newline out, a signed header list that omits a
header the server hashes, an object key whose slashes got percent-encoded:
each of those is a 403 with no explanation, and none of them is visible in a
test that stubs out the HTTP layer. So the suite runs the real transport
against a real socket and a server that checks the signature the way MinIO
and Oracle check it.

Three things this file deliberately does the way a bucket does:

Delete returns 204 whether or not the key was there. That is the S3 semantic
that broke the resume accounting the retired native tool used to print, and a stub
that answered 404 for a missing key would hide it.

The batch multi-object delete answers the way the stores this project exists
for actually answer: 400 `MissingContentMD5` when the request carries neither
`Content-MD5` nor a recognised `x-amz-checksum-*` header, 400 `BadDigest` when
one is present and does not match the body, and otherwise 200 with a per-key
`DeleteResult`. Every attempt is recorded regardless of outcome, checksum
verification is reimplemented independently here rather than imported from
`generation_chain.reclaim.checksum` (the same reason `verify_sigv4` below does
not import `sources/signing/sigv4.py`: a stub that checked a client's own
arithmetic against itself would not be an oracle). A key given a
`delete_status` entry fails INSIDE that 200, the same partial-failure shape
the real fault produces; one given the status `"OMIT"` is left out of the
response's `DeleteResult` altogether, which is what a store that could not
fully honour a batch looks like on the wire.

Listing pages. The default page is small so the ordinary fixture needs
several round trips, because a pagination bug that only shows up past one
page is a bug that only shows up in production.

Nothing here is imported by the tool. It is an oracle, and its own
correctness is pinned in the signing tests against the signature AWS
publishes for its documented example request.
"""

from __future__ import annotations

import base64
import datetime as dt
import email.utils
import hashlib
import hmac
import http.server
import json
import os
import shutil
import socket
import struct
import tarfile
import tempfile
import threading
import urllib.parse
import xml.etree.ElementTree as ET
import xml.sax.saxutils as sax
import zlib

S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"

# What the rig's MinIO is set up with, so a test that talks to the stub and a
# run that talks to the rig differ in the endpoint and nothing else.
TEST_ACCESS_KEY = "s3testaccesskey"
TEST_SECRET_KEY = "s3testsecretkey0123456789"
TEST_REGION = "us-east-1"
TEST_BUCKET = "es-snapshots"


# ---------------------------------------------------------------------------
# SigV4, as a verifier rather than a signer
# ---------------------------------------------------------------------------

def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    k = _hmac(("AWS4" + secret).encode("utf-8"), datestamp)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


def canonical_query(raw_query: str) -> str:
    """The query string in the order and encoding SigV4 hashes it.

    Parameters sort by name then value, every one carries an `=` even when
    empty, and the encoding is RFC 3986 with nothing left unreserved beyond
    the four characters the spec names.
    """
    if not raw_query:
        return ""
    pairs = []
    for part in raw_query.split("&"):
        if not part:
            continue
        name, _sep, value = part.partition("=")
        pairs.append((urllib.parse.unquote(name), urllib.parse.unquote(value)))
    pairs.sort()
    return "&".join(
        f"{urllib.parse.quote(n, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
        for n, v in pairs)


def canonical_request(method: str, uri: str, query: str,
                      headers: dict, signed: list, payload_hash: str) -> str:
    """Rebuild the string the client says it signed.

    `uri` is the request target exactly as it arrived on the wire. S3 signs
    the path it was sent rather than a normalised form of it, which is why
    percent-encoding the slashes in an object key produces a 403 instead of a
    404: the server and the client end up hashing different strings.
    """
    lines = [method, uri, canonical_query(query)]
    for name in signed:
        value = " ".join(str(headers.get(name, "")).split())
        lines.append(f"{name}:{value}")
    lines.append("")
    lines.append(";".join(signed))
    lines.append(payload_hash)
    return "\n".join(lines)


# Derived from what MinIO actually returned for the 31 keys in
# tests/fixtures/real-minio-key-encoding.json, not from a specification.
# It is Java's URLEncoder with the slash left alone: alphanumerics and the
# four characters below survive, space becomes a plus sign, and everything
# else becomes an uppercase percent escape.
#
# The plus sign is the part that matters. It is not the encoding the request
# path uses, so a client that reads an encoding-type=url listing with plain
# percent-decoding turns the key `a b` into `a+b`. Both can exist in one
# bucket. Both deletes answer 204.
_STORE_SAFE = set("abcdefghijklmnopqrstuvwxyz"
                  "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-*_/")


def plain_listing_text(value: str) -> str:
    """A key the way a default listing carries it, control bytes included.

    Measured against MinIO rather than reasoned from the XML specification,
    because what it does is not what the specification would suggest. Bytes
    0x01 to 0x07 come back as numeric character references that XML does not
    permit, so the whole listing fails to parse. The other control bytes come
    back already replaced by U+FFFD, so the listing parses and hands over a
    key that is not the key in the bucket.

    Both matter, and a stub that quietly served a clean listing would let a
    reader that takes the default listing pass here and misread a bucket.
    """
    out = []
    for ch in value:
        code = ord(ch)
        if ch in "\t\n\r" or code >= 0x20:
            out.append(ch)
        elif 0x01 <= code <= 0x07:
            out.append(f"&#{code};")
        else:
            out.append("�")
    return "".join(out)


def url_encode_like_the_store(value: str) -> str:
    out = []
    for byte in value.encode("utf-8"):
        ch = chr(byte)
        if ch in _STORE_SAFE:
            out.append(ch)
        elif ch == " ":
            out.append("+")
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


# ---------------------------------------------------------------------------
# DeleteObjects: the checksum this store requires, verified independently
# ---------------------------------------------------------------------------

def _crc32c(data: bytes) -> int:
    """CRC-32C (Castagnoli), written independently of the package under test.

    Table-free on purpose: a bit-at-a-time construction from the polynomial is
    a different-shaped implementation from a table generated the same way
    twice, so the two are less likely to agree by sharing a mistake.
    """
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


# header name (lowercase, as the handler sees it) -> the base64 digest a
# correct request carries for that header. Order is the order a real store
# checks in: the first one present in the request is the one verified.
_CHECKSUM_DIGESTS = {
    "content-md5": lambda body: hashlib.md5(body).digest(),
    "x-amz-checksum-crc32": lambda body: struct.pack(">I", zlib.crc32(body)),
    "x-amz-checksum-crc32c": lambda body: struct.pack(">I", _crc32c(body)),
    "x-amz-checksum-sha256": lambda body: hashlib.sha256(body).digest(),
}


def _checksum_rejection(headers: dict, body: bytes):
    """None if the request's checksum header is present and correct.

    Otherwise `(status, code, message)`, matching what the stores this
    project exists for actually return: no recognised header at all is
    `MissingContentMD5`, and a header present but wrong is `BadDigest`.
    """
    present = [name for name in _CHECKSUM_DIGESTS if name in headers]
    if not present:
        return (400, "MissingContentMD5",
                "Missing required header for this request: Content-Md5")
    name = present[0]
    expected = base64.b64encode(_CHECKSUM_DIGESTS[name](body)).decode("ascii")
    if headers[name].strip() != expected:
        return (400, "BadDigest",
                f"the {name} header does not match the body sent")
    return None


def _parse_delete_request(body: bytes) -> list:
    """The `<Object><Key>` list out of a `DeleteObjects` request body.

    Raises ValueError, which the handler turns into `MalformedXML`, matching
    what a real store answers a body it cannot parse.
    """
    # The shipped code refuses a DOCTYPE before parsing, which is how it
    # closes entity expansion without a third-party parser. The double that
    # stands in for a store should refuse the same shapes, or a test can pass
    # against something more permissive than the real thing.
    from generation_chain.sources.s3 import refuse_doctype
    from generation_chain.errors import SourceReadError
    try:
        refuse_doctype(body, "delete request")
    except SourceReadError as exc:
        raise ValueError(str(exc)) from exc
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError(f"the delete request is not XML: {exc}") from exc
    keys = []
    for child in root:
        if child.tag.rsplit("}", 1)[-1] != "Object":
            continue
        for grandchild in child:
            if grandchild.tag.rsplit("}", 1)[-1] == "Key":
                keys.append(grandchild.text or "")
    return keys


def _render_delete_result(deleted: list, errors: list) -> bytes:
    root = ET.Element("DeleteResult", {"xmlns": S3_NS})
    for key in deleted:
        element = ET.SubElement(root, "Deleted")
        ET.SubElement(element, "Key").text = key
    for key, code, message in errors:
        element = ET.SubElement(root, "Error")
        ET.SubElement(element, "Key").text = key
        ET.SubElement(element, "Code").text = code
        ET.SubElement(element, "Message").text = message
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


class SignatureRejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def verify_sigv4(method: str, raw_target: str, headers: dict, body: bytes,
                 access_key: str, secret: str, region: str,
                 service: str = "s3") -> dict:
    """Check a request the way a bucket does. Raises SignatureRejected.

    Returns the parsed pieces so a test can assert on the signed header list
    and the credential scope without re-deriving them.
    """
    auth = headers.get("authorization")
    if not auth:
        raise SignatureRejected("AccessDenied", "no Authorization header")
    if not auth.startswith("AWS4-HMAC-SHA256 "):
        raise SignatureRejected(
            "InvalidRequest", f"not a SigV4 Authorization header: {auth[:40]!r}")
    fields = {}
    for part in auth[len("AWS4-HMAC-SHA256 "):].split(","):
        name, _sep, value = part.strip().partition("=")
        fields[name] = value
    for required in ("Credential", "SignedHeaders", "Signature"):
        if required not in fields:
            raise SignatureRejected(
                "AuthorizationHeaderMalformed", f"no {required} in Authorization")

    cred = fields["Credential"].split("/")
    if len(cred) != 5:
        raise SignatureRejected(
            "AuthorizationHeaderMalformed",
            f"credential scope is not five parts: {fields['Credential']!r}")
    key_id, datestamp, cred_region, cred_service, terminator = cred
    if key_id != access_key:
        raise SignatureRejected("InvalidAccessKeyId", f"unknown key {key_id!r}")
    if terminator != "aws4_request":
        raise SignatureRejected(
            "AuthorizationHeaderMalformed", f"bad terminator {terminator!r}")

    signed = fields["SignedHeaders"].split(";")
    if signed != sorted(signed):
        raise SignatureRejected(
            "AuthorizationHeaderMalformed",
            f"SignedHeaders is not sorted: {fields['SignedHeaders']!r}")
    for name in ("host", "x-amz-date", "x-amz-content-sha256"):
        if name not in signed:
            raise SignatureRejected(
                "AuthorizationHeaderMalformed",
                f"{name} is not in SignedHeaders, so the server would hash a "
                f"different string than the client did")

    amz_date = headers.get("x-amz-date", "")
    if not amz_date or amz_date[:8] != datestamp:
        raise SignatureRejected(
            "AuthorizationHeaderMalformed",
            f"x-amz-date {amz_date!r} does not match scope date {datestamp!r}")

    payload_hash = headers.get("x-amz-content-sha256", "")
    if payload_hash != "UNSIGNED-PAYLOAD":
        actual = hashlib.sha256(body).hexdigest()
        if payload_hash != actual:
            raise SignatureRejected(
                "XAmzContentSHA256Mismatch",
                "x-amz-content-sha256 does not match the body")

    path, _sep, query = raw_target.partition("?")
    creq = canonical_request(method, path, query, headers, signed, payload_hash)
    scope = f"{datestamp}/{cred_region}/{cred_service}/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                     hashlib.sha256(creq.encode("utf-8")).hexdigest()])
    expected = hmac.new(
        signing_key(secret, datestamp, cred_region, cred_service),
        sts.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, fields["Signature"]):
        raise SignatureRejected(
            "SignatureDoesNotMatch",
            "the signature does not match the request that carried it.\n"
            f"canonical request the server built:\n{creq}")
    if cred_region != region:
        raise SignatureRejected("AuthorizationHeaderMalformed",
                                f"wrong region {cred_region!r}")
    return {"canonical_request": creq, "signed_headers": signed,
            "scope": scope, "access_key": key_id}


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------

class Request:
    """One request as it arrived, kept so a test can assert on the wire."""

    def __init__(self, method, raw_target, headers, body):
        self.method = method
        self.raw_target = raw_target
        self.path, _sep, self.query = raw_target.partition("?")
        self.headers = headers
        self.body = body
        self.params = urllib.parse.parse_qs(self.query, keep_blank_values=True)

    def __repr__(self):
        return f"<{self.method} {self.raw_target}>"


class S3Rig:
    """A bucket backed by a directory, over a real socket.

    Keys map to paths under `root`. `prefix` puts the repository somewhere
    other than the bucket root, because a repository configured with a
    base_path is the ordinary case and a tool that forgets the prefix on the
    delete path deletes nothing while reporting success.
    """

    def __init__(self, root: str, bucket: str = TEST_BUCKET, prefix: str = "",
                 access_key: str = TEST_ACCESS_KEY,
                 secret: str = TEST_SECRET_KEY, region: str = TEST_REGION,
                 page_size: int = 7, versioning: str = "absent",
                 delete_status=None, verify_signature: bool = True,
                 stamps: dict = None, faults=None,
                 repeat_token_after: int = None, drop_token_after: int = None,
                 omit_is_truncated: bool = False, answer_v1: bool = False,
                 objects: dict = None, echo_encoding_type: bool = True,
                 phantom_keys=()):
        self.root = root
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.access_key = access_key
        self.secret = secret
        self.region = region
        self.page_size = page_size
        # "enabled", "suspended", "absent" (the OCI answer: the endpoint has
        # no such operation), "denied", "broken".
        self.versioning = versioning
        # key -> (http status, error code), for making a delete fail.
        self.delete_status = dict(delete_status or {})
        self.verify_signature = verify_signature
        self.stamps = dict(stamps or {})
        # How this store misbehaves. Each is a real failure a bucket produces
        # and none of them can be reached with page_size alone.
        #
        # `faults` is a queue of (status, code) served before the real answer,
        # or None to serve normally. It covers three cases in one knob: fail
        # twice then succeed, which is the retry path; fail forever, which is
        # whether the tool gives up or spins; and a 404, which must not be
        # retried at all because retrying it turns a missing object into a
        # long wait and then the same answer. Faults can be scoped to a method
        # by passing a dict of method -> queue.
        #
        # `repeat_token_after` hands back the same continuation token from
        # that page onward, which walks a trusting client into a loop that
        # never ends. `drop_token_after` sets IsTruncated true and omits the
        # token, which stops a trusting client early and makes it report the
        # rest of the bucket as absent. That one is worse than the loop: a
        # sweep that quits halfway through a listing has a reachability set
        # missing whatever it never saw, and everything missing from it looks
        # like an orphan.
        #
        # Both of those set IsTruncated true, so between them they cannot
        # express a store that understates truncation. The next two can.
        #
        # `omit_is_truncated` leaves the element out of every page. A client
        # that reads an absent IsTruncated as a false stops at the end of
        # page one and calls the listing complete. That is the same corrupted
        # reachability set `drop_token_after` produces, and this time nothing
        # on the wire hints at it.
        #
        # `answer_v1` is the store behind that. It ignores list-type=2 and
        # serves the V1 operation. Oracle's S3 compatibility surface names
        # ListObjects and does not name ListObjectsV2, and list-type is an
        # ordinary query parameter, so a store that never implemented V2
        # drops it rather than rejecting it. The page that comes back carries
        # Marker, adds NextMarker when truncated, reports no KeyCount, and
        # pages off `marker` instead of `continuation-token`. A V2 client
        # sends a token that store never reads, so it walks page one forever.
        self.faults = ({k: list(v) for k, v in faults.items()}
                       if isinstance(faults, dict) else list(faults or []))
        self.repeat_token_after = repeat_token_after
        self.drop_token_after = drop_token_after
        self.omit_is_truncated = omit_is_truncated
        self.answer_v1 = answer_v1
        self.list_calls = 0
        # Keys held in memory rather than on disk. A bucket accepts keys a
        # filesystem will not hold: one ending in a slash, one containing a
        # character the local encoding cannot represent. Those are exactly
        # the keys worth testing an encoder against, so they cannot be the
        # ones the harness quietly drops.
        self.objects = dict(objects or {})
        # MinIO and AWS both echo <EncodingType>url</EncodingType> when a
        # listing asked for it, which is the only way a client can know its
        # request was honoured rather than ignored. Not every S3 front end
        # does, and a client that assumed it had been honoured would read
        # `a+b` as `a b` on a store that ignored the parameter. Off is the
        # store that stays silent.
        self.echo_encoding_type = echo_encoding_type
        # Keys the listing reports and the store does not hold. Real stores
        # produce this transiently when an object is removed between the page
        # being built and being read, and it is the state in which a client
        # that guesses at key encoding has guessed wrong: the key it derived
        # names nothing, the delete answers 204, and the object it meant to
        # remove is still there.
        self.phantom_keys = tuple(phantom_keys)

        self.requests: list = []
        self.deleted: list = []
        self.batch_delete_attempts: list = []
        self.signature_failures: list = []
        self.lock = threading.Lock()
        self._httpd = None
        self._thread = None

    # Object storage.

    def _path_for(self, key: str):
        if not self.root:
            return None
        if self.prefix and not key.startswith(self.prefix):
            return None
        rel = key[len(self.prefix):]
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            return None
        return os.path.join(self.root, rel)

    def keys(self) -> set:
        out = set(self.objects)
        out |= set(self.phantom_keys)
        if self.root:
            for dirpath, _dirs, files in os.walk(self.root):
                for f in files:
                    full = os.path.join(dirpath, f)
                    rel = os.path.relpath(full, self.root).replace(os.sep, "/")
                    out.add(self.prefix + rel)
        return out

    def _read(self, key: str):
        """The object's bytes, or None when the key is not in the bucket."""
        if key in self.phantom_keys:
            return None
        if key in self.objects:
            return self.objects[key]
        path = self._path_for(key)
        if path is None or not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            return fh.read()

    def _last_modified(self, key: str, path) -> str:
        stamp = self.stamps.get(key)
        if stamp is None and (path is None or not os.path.exists(path)):
            stamp = "2026-01-01T00:00:00.000Z"
        if stamp is None:
            when = dt.datetime.fromtimestamp(os.path.getmtime(path),
                                             dt.timezone.utc)
            stamp = when.strftime("%Y-%m-%dT%H:%M:%S.") + \
                f"{when.microsecond // 1000:03d}Z"
        return stamp

    # Lifecycle.

    def __enter__(self):
        rig = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _dispatch(self, method):
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                req = Request(method, self.path, headers, body)
                with rig.lock:
                    rig.requests.append(req)
                try:
                    if rig.verify_signature:
                        verify_sigv4(method, self.path, headers, body,
                                     rig.access_key, rig.secret, rig.region)
                except SignatureRejected as exc:
                    with rig.lock:
                        rig.signature_failures.append((req, str(exc)))
                    return self._error(403, exc.code, exc.message)
                try:
                    rig.handle(self, req)
                except SignatureRejected as exc:
                    self._error(403, exc.code, exc.message)

            def do_GET(self):
                self._dispatch("GET")

            def do_HEAD(self):
                self._dispatch("HEAD")

            def do_DELETE(self):
                self._dispatch("DELETE")

            def do_POST(self):
                self._dispatch("POST")

            def do_PUT(self):
                self._dispatch("PUT")

            def _send(self, status, body=b"", ctype="application/xml",
                      extra=None, head_only=False):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if body and not head_only:
                    self.wfile.write(body)

            def _error(self, status, code, message):
                body = (f'<?xml version="1.0" encoding="UTF-8"?>'
                        f"<Error><Code>{sax.escape(code)}</Code>"
                        f"<Message>{sax.escape(message)}</Message>"
                        f"</Error>").encode("utf-8")
                self._send(status, body, head_only=(self.command == "HEAD"))

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
        return False

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # Routing.

    def handle(self, h, req: Request):
        head_only = req.method == "HEAD"
        segments = req.path.lstrip("/").split("/", 1)
        bucket = urllib.parse.unquote(segments[0]) if segments[0] else ""
        key = urllib.parse.unquote(segments[1]) if len(segments) > 1 else ""

        if bucket != self.bucket:
            return h._error(404, "NoSuchBucket", f"no bucket {bucket!r}")

        with self.lock:
            queue = (self.faults.get(req.method, [])
                     if isinstance(self.faults, dict) else self.faults)
            fault = queue.pop(0) if queue else None
        if fault is not None:
            status, code = fault
            return h._error(status, code, "injected fault")

        if req.method == "POST" and "delete" in req.params:
            return self._batch_delete(h, req)

        if not key and "versioning" in req.params:
            return self._versioning(h, head_only)

        if not key:
            return self._list(h, req, head_only)

        if req.method == "DELETE":
            return self._delete(h, key)
        if req.method in ("GET", "HEAD"):
            return self._get(h, key, head_only)
        return h._error(405, "MethodNotAllowed", req.method)

    def _versioning(self, h, head_only):
        if self.versioning == "absent":
            # What Oracle's S3 compatibility endpoint does: the operation is
            # not part of the surface at all.
            return h._error(404, "NotImplemented",
                            "GetBucketVersioning is not supported")
        if self.versioning == "denied":
            return h._error(403, "AccessDenied", "not authorised")
        if self.versioning == "broken":
            body = b"<html>gateway error</html>"
            return h._send(502, body, ctype="text/html", head_only=head_only)
        status = {"enabled": "<Status>Enabled</Status>",
                  "suspended": "<Status>Suspended</Status>",
                  "never": ""}[self.versioning]
        body = (f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<VersioningConfiguration xmlns="{S3_NS}">{status}'
                f'</VersioningConfiguration>').encode("utf-8")
        return h._send(200, body, head_only=head_only)

    def _list(self, h, req: Request, head_only):
        def one(name, default=""):
            return req.params.get(name, [default])[0]

        # `answer_v1` drops list-type the way a store that never implemented
        # V2 drops it, so the request asked for V2 and the answer is V1.
        v2 = one("list-type") == "2" and not self.answer_v1
        prefix = one("prefix")
        token = one("continuation-token") if v2 else one("marker")
        want = min(int(one("max-keys") or self.page_size), self.page_size)
        # MinIO and AWS both answer encoding-type=url with form encoding, so a
        # space in a key comes back as a plus sign. That is not the same
        # encoding the request path uses, and a client that decodes it with
        # plain percent-decoding reads the key `a b` as `a+b`. Both keys can
        # exist in one bucket, both deletes answer 204, and the wrong object
        # goes. The stub reproduces it because a stub that quietly did the
        # convenient thing would hide the whole hazard.
        url_encoded = one("encoding-type") == "url"

        def report(value: str) -> str:
            if url_encoded:
                return sax.escape(url_encode_like_the_store(value))
            # The numeric references this can produce are deliberately not
            # escaped again: emitting them raw is what makes the listing
            # unparseable, which is what the store does.
            return plain_listing_text(sax.escape(value))

        keys = sorted(k for k in self.keys() if k.startswith(prefix))
        if token:
            start = urllib.parse.unquote(
                base64.urlsafe_b64decode(token.encode()).decode()) if v2 else token
            keys = [k for k in keys if k > start]
        page, rest = keys[:want], keys[want:]

        rows = []
        for key in page:
            data = self._read(key) or b""
            size = len(data)
            etag = hashlib.md5(data).hexdigest()
            path = self._path_for(key)
            rows.append(
                f"<Contents><Key>{report(key)}</Key>"
                f"<LastModified>{self._last_modified(key, path)}</LastModified>"
                f"<ETag>&quot;{etag}&quot;</ETag><Size>{size}</Size>"
                f"<StorageClass>STANDARD</StorageClass></Contents>")

        with self.lock:
            self.list_calls += 1
            call = self.list_calls

        looping = (self.repeat_token_after is not None
                   and call >= self.repeat_token_after)
        dropping = (self.drop_token_after is not None
                    and call >= self.drop_token_after)

        more = "true" if (rest or looping or dropping) else "false"
        anchor = "" if looping else (page[-1] if page else prefix)
        tail = ""
        if more == "true" and not dropping:
            if v2:
                nxt = base64.urlsafe_b64encode(anchor.encode()).decode()
                tail = f"<NextContinuationToken>{nxt}</NextContinuationToken>"
            else:
                tail = f"<NextMarker>{report(anchor)}</NextMarker>"

        # KeyCount belongs to V2 and Marker belongs to V1. That pair tells a
        # client which operation answered on any page, truncated or not,
        # which the token fields cannot do on a page that ends the listing.
        paging_state = (f"<KeyCount>{len(page)}</KeyCount>" if v2
                        else f"<Marker>{report(token)}</Marker>")
        truncation = ("" if self.omit_is_truncated
                      else f"<IsTruncated>{more}</IsTruncated>")

        body = (f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<ListBucketResult xmlns="{S3_NS}">'
                f"<Name>{sax.escape(self.bucket)}</Name>"
                f"<Prefix>{report(prefix)}</Prefix>"
                + ("<EncodingType>url</EncodingType>"
                   if url_encoded and self.echo_encoding_type else "")
                + paging_state
                + f"<MaxKeys>{want}</MaxKeys>"
                + truncation + tail
                + "".join(rows) +
                "</ListBucketResult>").encode("utf-8")
        return h._send(200, body, head_only=head_only)

    def _get(self, h, key, head_only):
        data = self._read(key)
        if data is None:
            return h._error(404, "NoSuchKey", f"no key {key!r}")
        path = self._path_for(key)
        when = (dt.datetime.fromtimestamp(os.path.getmtime(path),
                                          dt.timezone.utc)
                if path and os.path.exists(path)
                else dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
        extra = {
            "Last-Modified": email.utils.format_datetime(when, usegmt=True),
            "ETag": '"' + hashlib.md5(data).hexdigest() + '"',
            "x-amz-request-id": "s3rig",
        }
        return h._send(200, data, ctype="application/octet-stream",
                       extra=extra, head_only=head_only)

    def _delete(self, h, key):
        if key in self.delete_status:
            status, code = self.delete_status[key]
            return h._error(status, code, f"refusing to delete {key!r}")
        self._remove(key)
        # The decision this whole stub exists to reproduce faithfully. S3
        # answers 204 for a key that was never there, so a caller cannot tell
        # a delete from a no-op.
        return h._send(204, b"", head_only=False)

    def _remove(self, key: str) -> bool:
        """Take `key` out of the bucket, and say whether it was there.

        Shared by the single-object and batch delete handlers, because both
        answer success for a key that was never there and neither may leave
        it on disk if it was.
        """
        existed = self._read(key) is not None
        if key in self.objects:
            del self.objects[key]
        elif existed:
            os.unlink(self._path_for(key))
        with self.lock:
            self.deleted.append((key, existed))
        return existed

    def _batch_delete(self, h, req: Request):
        with self.lock:
            self.batch_delete_attempts.append(req)
        rejection = _checksum_rejection(req.headers, req.body)
        if rejection is not None:
            status, code, message = rejection
            return h._error(status, code, message)
        try:
            keys = _parse_delete_request(req.body)
        except ValueError as exc:
            return h._error(400, "MalformedXML", str(exc))

        deleted = []
        errors = []
        for key in keys:
            if key in self.delete_status:
                status, code = self.delete_status[key]
                if code != "OMIT":
                    errors.append((key, code, f"refusing to delete {key!r}"))
                continue
            self._remove(key)
            deleted.append(key)
        return h._send(200, _render_delete_result(deleted, errors))

    # Helpers a test asserts against.

    def methods(self) -> set:
        with self.lock:
            return {r.method for r in self.requests}

    def targets(self, method: str = None) -> list:
        with self.lock:
            return [r.raw_target for r in self.requests
                    if method is None or r.method == method]


# ---------------------------------------------------------------------------
# The captured repository
# ---------------------------------------------------------------------------

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
REAL_REPO_TGZ = os.path.join(FIXTURES, "real-es952-repo.tar.gz")
REAL_REPO_STAMPS = os.path.join(FIXTURES, "real-es952-repo-last-modified.json")


def extract_real_repository(dest: str, age_hours: float = 30.0) -> dict:
    """Unpack the captured repository and restore the times it was written at.

    A downloaded copy carries download times, and a repository whose objects
    all landed inside one short window is exactly the mirror the tool refuses
    to sweep. The sidecar has every object's real Last-Modified; shifting the
    whole set by one constant ages it without disturbing the spacing, so it
    still reads as written rather than copied.

    Returns key -> Last-Modified in the format a listing prints, so the rig
    serves the same times it stamped on disk.
    """
    with tarfile.open(REAL_REPO_TGZ) as tf:
        tf.extractall(dest, filter="data")
    with open(REAL_REPO_STAMPS, encoding="utf-8") as fh:
        raw = json.load(fh)
    stamps = {
        rel: dt.datetime.strptime(v, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=dt.timezone.utc)
        for rel, v in raw.items()
    }
    target = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=age_hours)
    shift = target - max(stamps.values())
    out = {}
    for rel, when in stamps.items():
        path = os.path.join(dest, rel)
        if not os.path.exists(path):
            continue
        shifted = when + shift
        os.utime(path, (shifted.timestamp(), shifted.timestamp()))
        out[rel] = shifted.strftime("%Y-%m-%dT%H:%M:%S.") + \
            f"{shifted.microsecond // 1000:03d}Z"
    return out
