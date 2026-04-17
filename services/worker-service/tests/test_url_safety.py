"""Tests for executor.url_safety.

Mirrors services/api-service/.../UrlSafetyValidatorTest.java — same policy, same
positive cases, same threat-focused negatives. Tests inject a resolver so they
never hit real DNS.
"""

from __future__ import annotations

import pytest

from executor.url_safety import UrlSafetyError, validate


def resolver_returning(*ips: str):
    def _resolve(host: str) -> list[str]:
        return list(ips)
    return _resolve


def failing_resolver(host: str) -> list[str]:
    raise UrlSafetyError(f"Hostname could not be resolved: {host}")


# --- scheme / format ---

def test_rejects_none():
    with pytest.raises(UrlSafetyError):
        validate(None)


def test_rejects_blank():
    with pytest.raises(UrlSafetyError):
        validate("   ")


def test_rejects_file_scheme():
    with pytest.raises(UrlSafetyError, match="scheme"):
        validate("file:///etc/passwd")


def test_rejects_gopher_scheme():
    with pytest.raises(UrlSafetyError):
        validate("gopher://example.com/")


def test_rejects_data_scheme():
    with pytest.raises(UrlSafetyError):
        validate("data:text/plain,hi")


def test_rejects_schemeless():
    with pytest.raises(UrlSafetyError):
        validate("example.com/path")


def test_rejects_credentials_in_url():
    with pytest.raises(UrlSafetyError, match="[Cc]redentials"):
        validate(
            "https://alice:secret@example.com/",
            resolver=resolver_returning("93.184.216.34"),
        )


# --- hostname suffix blocks ---

def test_rejects_dot_local_suffix():
    with pytest.raises(UrlSafetyError):
        validate("http://printer.local/", resolver=resolver_returning("93.184.216.34"))


def test_rejects_dot_internal_suffix():
    with pytest.raises(UrlSafetyError):
        validate("http://metadata.internal/", resolver=resolver_returning("93.184.216.34"))


# --- resolved-IP blocks (the ones that actually matter for SSRF) ---

def test_rejects_aws_metadata_literal():
    with pytest.raises(UrlSafetyError, match="[Rr]eserved"):
        validate(
            "http://169.254.169.254/latest/meta-data/",
            resolver=resolver_returning("169.254.169.254"),
        )


def test_rejects_alibaba_metadata_literal():
    # 100.100.100.200 — Alibaba Cloud IMDS, in CGNAT range.
    with pytest.raises(UrlSafetyError):
        validate(
            "http://100.100.100.200/",
            resolver=resolver_returning("100.100.100.200"),
        )


def test_rejects_dns_pointing_at_metadata():
    # DNS rebind shape: public-looking hostname resolves to metadata IP.
    with pytest.raises(UrlSafetyError):
        validate(
            "http://attacker.example.com/",
            resolver=resolver_returning("169.254.169.254"),
        )


def test_rejects_zero_network():
    with pytest.raises(UrlSafetyError):
        validate("http://a.example.com/", resolver=resolver_returning("0.0.0.0"))


def test_rejects_ipv6_unique_local():
    with pytest.raises(UrlSafetyError):
        validate("http://a.example.com/", resolver=resolver_returning("fd00::1"))


def test_rejects_ipv4_mapped_metadata():
    # ::ffff:169.254.169.254 — mapped form must not bypass the v4 check.
    with pytest.raises(UrlSafetyError):
        validate(
            "http://a.example.com/",
            resolver=resolver_returning("::ffff:169.254.169.254"),
        )


def test_rejects_mixed_resolution_if_any_is_reserved():
    # DNS returns both a public and a metadata IP — must reject.
    with pytest.raises(UrlSafetyError):
        validate(
            "http://a.example.com/",
            resolver=resolver_returning("93.184.216.34", "169.254.169.254"),
        )


def test_rejects_unresolvable():
    with pytest.raises(UrlSafetyError, match="could not be resolved"):
        validate("http://nx.example.com/", resolver=failing_resolver)


# --- dev-friendly positive cases ---

def test_accepts_public_https():
    # No exception.
    validate(
        "https://cloud.langfuse.com/api/public/health",
        resolver=resolver_returning("93.184.216.34"),
    )


def test_accepts_localhost():
    # Dev workflow: Langfuse / MCP on localhost.
    validate(
        "http://localhost:3300/api/public/health",
        resolver=resolver_returning("127.0.0.1"),
    )


def test_accepts_loopback_literal():
    validate(
        "http://127.0.0.1:3300/api/public/health",
        resolver=resolver_returning("127.0.0.1"),
    )


def test_accepts_rfc1918():
    # Dev workflow: VPN-reachable internal service.
    validate(
        "https://tools-10/mcp",
        resolver=resolver_returning("10.20.30.40"),
    )
