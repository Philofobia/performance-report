"""SSRF-safe URL validation for the browser ingestion layer.

Implements SECURITY_PLAN.md §2.2 and the `security` skill checklist:

  * require ``https`` (reject http/file/gopher/...),
  * block private / internal / reserved IP ranges,
  * forbid raw-IP URLs,
  * forbid userinfo URLs (``user:pass@host``).

`validate_url` raises :class:`UnSafeURLError`; `is_safe_url` returns a bool.
Hostname range-checking requires DNS resolution; the injectable :data:`_lookup`
hook keeps the logic deterministic and unit-testable offline.
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Set
from urllib.parse import urlsplit

# Private / internal / reserved ranges (SECURITY_PLAN.md §2.2).
BLOCKED_NETWORKS: tuple = (
    ipaddress.ip_network("127.0.0.0/8"),    # loopback
    ipaddress.ip_network("10.0.0.0/8"),     # private
    ipaddress.ip_network("172.16.0.0/12"),  # private
    ipaddress.ip_network("192.168.0.0/16"), # private
    ipaddress.ip_network("169.254.0.0/16"), # link-local
    ipaddress.ip_network("0.0.0.0/8"),      # "this network" / any
    ipaddress.ip_network("::1/128"),        # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),       # IPv6 unique-local
)


class UnSafeURLError(ValueError):
    """Raised when ``url`` fails one of the SSRF guards."""


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def is_internal_ip(addr) -> bool:
    """Whether ``addr`` falls inside any blocked/private/reserved network."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in BLOCKED_NETWORKS)


def _lookup(host: str) -> Set[str]:
    """Resolve ``host`` to a set of IP strings (injectable for tests)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:  # pragma: no cover - hostname resolution
        raise UnSafeURLError(f"Could not resolve host {host!r}") from exc
    return {info[4][0] for info in infos}


def _check_resolved(host: str) -> None:
    """Reject when any address a hostname resolves to is internal."""
    for ip in _lookup(host):
        if is_internal_ip(ip):
            raise UnSafeURLError(
                f"Host {host!r} resolves to blocked/private address {ip}"
            )


def validate_url(url, *, resolve: bool = True) -> str:
    """Validate ``url`` for SSRF safety; return the normalized URL on success.

    Raises :class:`UnSafeURLError` with a concrete reason on any violation.
    """
    if not isinstance(url, str) or not url.strip():
        raise UnSafeURLError("URL must be a non-empty string")
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UnSafeURLError(f"Malformed URL: {exc}") from exc

    if parts.scheme.lower() != "https":
        raise UnSafeURLError(f"Only https URLs are allowed, got {parts.scheme!r}")

    if parts.username is not None or parts.password is not None:
        raise UnSafeURLError("URLs containing userinfo (user:pass@host) are not allowed")

    host = parts.hostname
    if not host:
        raise UnSafeURLError("URL must include a hostname")

    # Raw-IP URLs are always forbidden regardless of whether the IP is public.
    if _is_ip_literal(host):
        raise UnSafeURLError("Raw IP addresses are not allowed")

    if resolve:
        _check_resolved(host)

    return url


def is_safe_url(url: str, *, resolve: bool = True) -> bool:
    """Return True iff ``url`` passes all SSRF guards."""
    try:
        validate_url(url, resolve=resolve)
    except UnSafeURLError:
        return False
    return True
