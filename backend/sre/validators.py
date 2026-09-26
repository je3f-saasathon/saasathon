import ipaddress
import socket
from urllib.parse import urlparse

from django.conf import settings


class UnsafeURLError(ValueError):
    pass


def _validate_public_https(url: str, label: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UnsafeURLError(f"{label} must use https")
    if not parsed.hostname:
        raise UnsafeURLError(f"{label} has no host")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"{label} host does not resolve: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise UnsafeURLError(f"{label} must not point at a private, loopback or link-local address")


def validate_llm_base_url(url: str) -> None:
    """Blocks SSRF: the worker can reach Temporal, Langfuse and the DB, so a
    user-supplied base_url must be public https unless explicitly allowed."""
    if not url or settings.SRE_ALLOW_PRIVATE_LLM_URLS:
        return
    _validate_public_https(url, "base_url")


def validate_uptrace_api_url(url: str) -> None:
    """Same SSRF rule for an Uptrace credential's api_base_url. Checked when it's saved
    and again before every call (DNS can change in between)."""
    if not url:
        raise UnsafeURLError("api_base_url is required")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeURLError("api_base_url must be an http(s) URL")
    if settings.SRE_ALLOW_PRIVATE_UPTRACE_URLS:
        return
    _validate_public_https(url, "api_base_url")
