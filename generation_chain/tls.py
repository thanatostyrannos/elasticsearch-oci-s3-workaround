"""The TLS client settings every HTTPS request this package sends starts from.

The store reads, the delete and the cluster client all come here, so one line
decides the floor for all of them.
"""

from __future__ import annotations

import ssl
from typing import Optional


def client_context(ca_certificate: Optional[str] = None) -> ssl.SSLContext:
    """A context that verifies the server and refuses anything below TLS 1.2.

    `ssl.create_default_context` verifies the certificate and the host name
    and leaves `minimum_version` at MINIMUM_SUPPORTED, measured on Python
    3.12.3 with OpenSSL 3.0.13. Which protocols the handshake then accepts is
    up to the host's OpenSSL build and its security level, and that differs
    from one machine to the next, so this function sets the floor itself.

    1.2 rather than 1.3, because a store or a cluster that speaks only 1.2 is
    ordinary, and refusing it would fail a run for a reason that has nothing
    to do with what the server had to say.
    """
    context = ssl.create_default_context(cafile=ca_certificate)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context
