"""An HTTP opener that refuses every redirect.

S3, OCI and Elasticsearch answer an API request in place. A 3xx from one of
them means a misconfigured endpoint, a proxy, or a party trying to steer the
request somewhere else. urllib's default handler follows it and copies every
request header, Authorization included, to the new host, and it follows an
https to http downgrade. This opener raises instead, so the request goes to
the host the operator configured and nowhere else.

The refusal names the status and the Location host. It never names a header
or the Location path and query, which can carry a signature or a token.
"""

from __future__ import annotations

import ssl
import urllib.parse
import urllib.request
from typing import Optional

from .errors import GenerationChainError
from .tls import client_context


class RedirectRefused(GenerationChainError):
    """The server answered with a 3xx. The redirect was not followed."""

    def __init__(self, code: int, location_host: str) -> None:
        super().__init__(
            f"the server answered {code} and redirected to "
            f"{location_host or '(an unreadable location)'}. A redirect is "
            "never followed, because it would carry this request's "
            "credentials to that host. Point the endpoint at the host that "
            "answers directly")
        self.code = code
        self.location_host = location_host


def _host_of(location: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(location)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return ""
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{port}" if host and port else host


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RedirectRefused(code, _host_of(newurl))


def refusing_urlopen(request: urllib.request.Request,
                     timeout: Optional[float] = None,
                     context: Optional[ssl.SSLContext] = None):
    """`urllib.request.urlopen`, except that a 3xx raises RedirectRefused.

    An https request that names no `context` gets `tls.client_context()`,
    so the store reads and the delete hold TLS to the same floor as the
    cluster client. A caller that supplies a context, to load a CA file,
    keeps its own.
    """
    handlers = [_RefuseRedirects()]
    if context is None and request.type == "https":
        context = client_context()
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    if timeout is None:
        return opener.open(request)
    return opener.open(request, timeout=timeout)
