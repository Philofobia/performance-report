"""Unit tests for normalize/url_safety.py — SSRF guards (SECURITY_PLAN §2.2)."""
from __future__ import annotations

import ipaddress

import pytest

from normalize import url_safety as us
from normalize.url_safety import UnSafeURLError

# One representative address from each blocked range.
BLOCKED_SAMPLES = [
    "127.0.0.1",      # 127/8 loopback
    "10.0.0.5",       # 10/8 private
    "172.16.0.1",     # 172.16/12 private (start)
    "172.31.255.255", # 172.16/12 private (end)
    "192.168.1.1",    # 192.168/16 private
    "169.254.1.1",    # 169.254/16 link-local
    "0.0.0.0",        # 0.0.0.0/8 this-network
    "::1",            # IPv6 loopback
    "fc00::1",        # fc00::/7 unique-local (start)
    "fdff::1",        # fc00::/7 unique-local (end)
]


@pytest.mark.parametrize("ip", BLOCKED_SAMPLES)
def test_is_internal_ip_covers_every_blocked_range(ip):
    assert us.is_internal_ip(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "93.184.216.34", "2606:4700::1111"])
def test_public_ip_not_internal(ip):
    assert not us.is_internal_ip(ip)


# ---- raw IP URLs are always forbidden ------------------------------------ #
@pytest.mark.parametrize("ip", BLOCKED_SAMPLES + ["8.8.8.8", "2606:4700::1111"])
def test_raw_ip_urls_rejected(ip):
    # IPv6 literals in URLs must be bracket-quoted: https://[addr]/
    literal = f"[{ip}]" if ":" in ip else ip
    url = f"https://{literal}/"
    assert us.is_safe_url(url, resolve=False) is False
    with pytest.raises(UnSafeURLError, match="Raw IP"):
        us.validate_url(url, resolve=False)


# ---- scheme enforcement --------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/",
        "file:///etc/passwd",
        "gopher://example.com/x",
        "ftp://example.com/file",
        "javascript:alert(1)",
    ],
)
def test_non_https_schemes_rejected(url):
    assert us.is_safe_url(url, resolve=False) is False
    with pytest.raises(UnSafeURLError, match="https"):
        us.validate_url(url, resolve=False)


# ---- userinfo ------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@example.com/",
        "https://:secret@example.com/",
        "https://user@example.com/",
    ],
)
def test_userinfo_urls_rejected(url):
    assert us.is_safe_url(url, resolve=False) is False
    with pytest.raises(UnSafeURLError, match="userinfo"):
        us.validate_url(url, resolve=False)


# ---- hostname resolution to private ranges -------------------------------- #
@pytest.mark.parametrize("private_ip", BLOCKED_SAMPLES)
def test_hostname_resolving_to_private_ip_rejected(monkeypatch, private_ip):
    monkeypatch.setattr(us, "_lookup", lambda host: {private_ip})
    with pytest.raises(UnSafeURLError, match="blocked/private"):
        us.validate_url("https://evil.example.com/")


def test_hostname_resolving_to_public_ip_accepted(monkeypatch):
    monkeypatch.setattr(us, "_lookup", lambda host: {"8.8.8.8"})
    assert us.is_safe_url("https://example.com/", resolve=True) is True


def test_empty_url_rejected():
    with pytest.raises(UnSafeURLError, match="non-empty"):
        us.validate_url("", resolve=False)
    with pytest.raises(UnSafeURLError, match="non-empty"):
        us.validate_url(None, resolve=False)


def test_missing_hostname_rejected():
    with pytest.raises(UnSafeURLError, match="hostname"):
        us.validate_url("https://", resolve=False)


def test_blocked_networks_math_is_complete():
    """Sanity: our network list matches the security plan exactly."""
    plan = [
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "0.0.0.0/8",
        "::1/128",
        "fc00::/7",
    ]
    ours = {str(net) for net in us.BLOCKED_NETWORKS}
    equal = (
        {str(ipaddress.ip_network(p)) for p in plan}
        == {str(ipaddress.ip_network(str(n))) for n in us.BLOCKED_NETWORKS}
    )
    assert equal, f"plan={plan} ours={sorted(ours)}"
