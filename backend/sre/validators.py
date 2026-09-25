import ipaddress
import socket
from urllib.parse import urlparse

from django.conf import settings


class UnsafeURLError(ValueError):
    pass


def validate_llm_base_url(url: str) -> None:
    """Blocks SSRF: the worker can reach Temporal, Langfuse and the DB, so a
    user-supplied base_url must be public https unless explicitly allowed."""
    if not url or settings.SRE_ALLOW_PRIVATE_LLM_URLS:
        return
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UnsafeURLError("base_url must use https")
    if not parsed.hostname:
        raise UnsafeURLError("base_url has no host")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"base_url host does not resolve: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise UnsafeURLError("base_url must not point at a private, loopback or link-local address")
