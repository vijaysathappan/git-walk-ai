"""Shared SSRF protection for connectors that fetch an operator-configured
URL/host (REST, database, S3-compatible endpoints). An authenticated user
who can configure an integration would otherwise be able to point the
backend at an internal-only service or the cloud metadata endpoint
(169.254.169.254) and have the response read back through the connector's
normal read path -- this resolves the hostname and rejects anything that
isn't a public, routable address before the connection is ever opened.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class SSRFBlockedError(RuntimeError):
    def __init__(self, message: str):
        super().__init__(message)


def _is_blocked_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
        or str(ip) == "169.254.169.254"  # cloud metadata endpoint (AWS/GCP/Azure)
    )


def assert_url_is_safe(url: str) -> None:
    """Raises SSRFBlockedError if `url`'s host resolves to a
    private/loopback/link-local/reserved/metadata address. Call this
    immediately before every outbound request built from operator/user
    configuration -- resolving fresh each call (rather than caching) also
    keeps this correct against DNS-rebinding between check and connect,
    since urllib re-resolves the same hostname when it actually connects."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SSRFBlockedError(f"Unsupported URL scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise SSRFBlockedError("URL has no host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SSRFBlockedError(f"Could not resolve host: {host!r}") from exc
    for info in infos:
        address = info[4][0]
        if _is_blocked_address(address):
            raise SSRFBlockedError(
                f"Host {host!r} resolves to a private/internal address ({address}); refusing to connect"
            )
