"""Outbound URL safety validator for customer-configured hosts (Langfuse, etc.).

Mirrors the policy of the Java UrlSafetyValidator used by the API service: block
the SSRF vectors that matter (link-local / cloud-metadata, CGNAT, 0/8, multicast,
reserved, IPv6 ULA, IPv4-mapped IPv6 forms of each, non-http(s) schemes, userinfo,
and .local / .internal suffixes) while keeping dev workflows working — loopback
and RFC1918 stay allowed so local Langfuse (127.0.0.1:3300) and VPN-reachable
internal instances don't break.

When the platform onboards external tenants, add a strict mode that additionally
rejects loopback and RFC1918 and gate it on a config flag.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlparse


Resolver = Callable[[str], list[str]]


class UrlSafetyError(ValueError):
    """Raised when a URL is rejected by the safety check."""


_ALLOWED_SCHEMES = frozenset({"http", "https"})

_BLOCKED_IPV4_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),         # "this" network / unspecified block
    ipaddress.ip_network("100.64.0.0/10"),     # CGNAT — covers Alibaba IMDS 100.100.100.200
    ipaddress.ip_network("169.254.0.0/16"),    # link-local — covers AWS/GCP/Azure IMDS
    ipaddress.ip_network("224.0.0.0/4"),       # multicast
    ipaddress.ip_network("240.0.0.0/4"),       # reserved / future use
)

_BLOCKED_IPV6_NETWORKS = (
    ipaddress.ip_network("::/128"),            # unspecified
    ipaddress.ip_network("fc00::/7"),          # unique local addresses
    ipaddress.ip_network("fe80::/10"),         # link-local
    ipaddress.ip_network("ff00::/8"),          # multicast
)


def _default_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UrlSafetyError(f"Hostname could not be resolved: {host}") from exc
    return sorted({info[4][0] for info in infos})


def validate(url: str, *, resolver: Resolver = _default_resolver) -> None:
    """Raise UrlSafetyError if the URL points anywhere unsafe; return None otherwise."""
    if url is None or not str(url).strip():
        raise UrlSafetyError("URL is required")

    parsed = urlparse(str(url).strip())

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UrlSafetyError("URL must use http or https scheme")

    if parsed.username or parsed.password:
        raise UrlSafetyError("Credentials embedded in URLs are not allowed")

    host = parsed.hostname
    if not host:
        raise UrlSafetyError("URL must include a hostname")

    normalized = host.lower()
    if normalized.endswith(".local") or normalized.endswith(".internal"):
        raise UrlSafetyError("Hostnames ending in .local or .internal are not allowed")

    ips = resolver(host)
    if not ips:
        raise UrlSafetyError(f"Hostname could not be resolved: {host}")

    for ip_text in ips:
        _assert_safe_address(ipaddress.ip_address(ip_text))


def _assert_safe_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if isinstance(address, ipaddress.IPv4Address):
        _assert_safe_ipv4(address)
        return

    # IPv4-mapped IPv6 (::ffff:a.b.c.d) must be re-checked against v4 rules so
    # ::ffff:169.254.169.254 can't bypass the IPv4 metadata block.
    mapped = address.ipv4_mapped
    if mapped is not None:
        _assert_safe_ipv4(mapped)
        return

    for net in _BLOCKED_IPV6_NETWORKS:
        if address in net:
            raise UrlSafetyError("URL resolves to a reserved address range")


def _assert_safe_ipv4(address: ipaddress.IPv4Address) -> None:
    for net in _BLOCKED_IPV4_NETWORKS:
        if address in net:
            raise UrlSafetyError("URL resolves to a reserved address range")
