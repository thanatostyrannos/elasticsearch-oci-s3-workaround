"""A local stand-in for Oracle's native Object Storage API.

There is no OCI endpoint in this environment, so the native transport can only
be measured against something written here. This serves the two calls the
transport makes, ListObjects and GetObject, with Oracle's paging contract:
`limit`, `start`, and a `nextStartWith` marker, plus the `opc-next-page`
header variant the transport also honours.

It can be told to fail a chosen fraction of requests, with a chosen status, so
the auditor's real retry policy runs against real HTTP faults rather than
against an exception injected past it.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import random
import socketserver
import sys
import threading
import time
import urllib.parse
from typing import Dict, Optional

sys.dont_write_bytecode = True

PAGE_SIZE = 1000


class OciStub:
    def __init__(self, objects: Dict[str, bytes], namespace: str = "ns",
                 bucket: str = "b", prefix: str = "", page_size: int = PAGE_SIZE,
                 fail_rate: float = 0.0, fail_status: int = 503,
                 seed: int = 0, latency: float = 0.0,
                 next_page_in_header: bool = False) -> None:
        self.objects = objects
        self.namespace = namespace
        self.bucket = bucket
        self.prefix = (prefix.strip("/") + "/") if prefix.strip("/") else ""
        self.page_size = page_size
        self.fail_rate = fail_rate
        self.fail_status = fail_status
        self.seed = seed
        self.latency = latency
        self.next_page_in_header = next_page_in_header
        self.sorted_keys = sorted(self.prefix + k for k in objects)
        # Every request, whether it was failed, and how deep the retries went.
        self.requests = 0
        self.failed = 0
        self.pages_served = 0
        self.gets = 0
        self.heads = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.attempts: Dict[str, int] = {}
        self._lock = threading.Lock()

    # -- fault policy -----------------------------------------------------

    def _should_fail(self, target: str) -> bool:
        """Fails per ATTEMPT, not per key, so a retry can succeed.

        A store that failed the same key on every attempt would measure the
        retry policy as useless, which is not what a 503 or a slow object
        does.
        """
        if self.fail_rate <= 0:
            return False
        with self._lock:
            self.attempts[target] = self.attempts.get(target, 0) + 1
            n = self.attempts[target]
        digest = hashlib.sha256(f"{self.seed}:{target}:{n}".encode()).digest()
        return int.from_bytes(digest[:8], "big") / float(1 << 64) < self.fail_rate

    # -- server -----------------------------------------------------------

    def __enter__(self) -> "OciStub":
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_HEAD(self):
                self._head = True
                self.do_GET()

            def do_GET(self):
                with stub._lock:
                    stub.requests += 1
                    stub.in_flight += 1
                    if stub.in_flight > stub.max_in_flight:
                        stub.max_in_flight = stub.in_flight
                try:
                    if stub.latency:
                        time.sleep(stub.latency)
                    path, _, query = self.path.partition("?")
                    params = urllib.parse.parse_qs(query)
                    head = f"/n/{stub.namespace}/b/{stub.bucket}/o"
                    if not path.startswith(head):
                        return self._send(404, b'{"code":"BucketNotFound"}')
                    name = urllib.parse.unquote(path[len(head):].lstrip("/"))
                    target = name or f"LIST:{params.get('start', [''])[0]}"
                    if stub._should_fail(target):
                        with stub._lock:
                            stub.failed += 1
                        return self._send(stub.fail_status, b'{"code":"fault"}')
                    if not name:
                        return stub._list(self, params)
                    return stub._get(self, name)
                finally:
                    with stub._lock:
                        stub.in_flight -= 1

            def _send(self, status, body, ctype="application/json", headers=()):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                if not getattr(self, "_head", False):
                    self.wfile.write(body)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    # -- the two calls ----------------------------------------------------

    def _list(self, handler, params):
        start = params.get("start", [""])[0]
        prefix = params.get("prefix", [""])[0]
        limit = int(params.get("limit", [str(self.page_size)])[0])
        limit = min(limit, self.page_size)
        keys = [k for k in self.sorted_keys if k.startswith(prefix) and k >= start]
        with self._lock:
            self.pages_served += 1
        page, rest = keys[:limit], keys[limit:]
        body = {"objects": [{"name": k} for k in page]}
        headers = []
        if rest:
            if self.next_page_in_header:
                headers.append(("opc-next-page", rest[0]))
            else:
                body["nextStartWith"] = rest[0]
        handler._send(200, json.dumps(body).encode(), headers=headers)

    def _get(self, handler, name):
        if not name.startswith(self.prefix):
            return handler._send(404, b'{"code":"ObjectNotFound"}')
        relative = name[len(self.prefix):]
        data = self.objects.get(relative)
        if data is None:
            return handler._send(404, b'{"code":"ObjectNotFound"}')
        with self._lock:
            if getattr(handler, "_head", False):
                self.heads += 1
            else:
                self.gets += 1
        handler._send(200, data, "application/octet-stream")


def credentials():
    """The test key the project ships, used only to sign against this stub."""
    from generation_chain.sources.oci import OciCredentials
    from generation_chain.sources.signing.rsa import RsaPrivateKey
    vector = os.environ.get(
        "GENCHAIN_OCI_VECTOR",
        "/home/thanatostyrannos/projects/wt-issue-43/tests/fixtures/"
        "genchain-oci-signing-vector.json")
    with open(vector) as handle:
        pem = json.load(handle)["private_key_pkcs8"]
    return OciCredentials(key_id="t/u/f",
                          private_key=RsaPrivateKey.from_pem(pem.encode()))
