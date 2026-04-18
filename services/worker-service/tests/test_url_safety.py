"""Tests for executor.url_safety.

Mirrors services/api-service/.../UrlSafetyValidatorTest.java — same policy, same
positive cases, same threat-focused negatives. Tests inject an async resolver so
they never hit real DNS.
"""

from __future__ import annotations

import pytest

from executor.url_safety import UrlSafetyError, validate


def resolver_returning(*ips: str):
    async def _resolve(host: str) -> list[str]:
        return list(ips)
    return _resolve


async def failing_resolver(host: str) -> list[str]:
    raise UrlSafetyError(f"Hostname could not be resolved: {host}")


# --- scheme / format ---

@pytest.mark.asyncio
async def test_rejects_none():
    with pytest.raises(UrlSafetyError):
        await validate(None)


@pytest.mark.asyncio
async def test_rejects_blank():
    with pytest.raises(UrlSafetyError):
        await validate("   ")


@pytest.mark.asyncio
async def test_rejects_file_scheme():
    with pytest.raises(UrlSafetyError, match="scheme"):
        await validate("file:///etc/passwd")


@pytest.mark.asyncio
async def test_rejects_gopher_scheme():
    with pytest.raises(UrlSafetyError):
        await validate("gopher://example.com/")


@pytest.mark.asyncio
async def test_rejects_data_scheme():
    with pytest.raises(UrlSafetyError):
        await validate("data:text/plain,hi")


@pytest.mark.asyncio
async def test_rejects_schemeless():
    with pytest.raises(UrlSafetyError):
        await validate("example.com/path")


@pytest.mark.asyncio
async def test_rejects_credentials_in_url():
    with pytest.raises(UrlSafetyError, match="[Cc]redentials"):
        await validate(
            "https://alice:secret@example.com/",
            resolver=resolver_returning("93.184.216.34"),
        )


# --- hostname suffix blocks ---

@pytest.mark.asyncio
async def test_rejects_dot_local_suffix():
    with pytest.raises(UrlSafetyError):
        await validate("http://printer.local/", resolver=resolver_returning("93.184.216.34"))


@pytest.mark.asyncio
async def test_rejects_dot_internal_suffix():
    with pytest.raises(UrlSafetyError):
        await validate("http://metadata.internal/", resolver=resolver_returning("93.184.216.34"))


# --- resolved-IP blocks (the ones that actually matter for SSRF) ---

@pytest.mark.asyncio
async def test_rejects_aws_metadata_literal():
    with pytest.raises(UrlSafetyError, match="[Rr]eserved"):
        await validate(
            "http://169.254.169.254/latest/meta-data/",
            resolver=resolver_returning("169.254.169.254"),
        )


@pytest.mark.asyncio
async def test_rejects_alibaba_metadata_literal():
    # 100.100.100.200 — Alibaba Cloud IMDS, in CGNAT range.
    with pytest.raises(UrlSafetyError):
        await validate(
            "http://100.100.100.200/",
            resolver=resolver_returning("100.100.100.200"),
        )


@pytest.mark.asyncio
async def test_rejects_dns_pointing_at_metadata():
    # DNS rebind shape: public-looking hostname resolves to metadata IP.
    with pytest.raises(UrlSafetyError):
        await validate(
            "http://attacker.example.com/",
            resolver=resolver_returning("169.254.169.254"),
        )


@pytest.mark.asyncio
async def test_rejects_zero_network():
    with pytest.raises(UrlSafetyError):
        await validate("http://a.example.com/", resolver=resolver_returning("0.0.0.0"))


@pytest.mark.asyncio
async def test_rejects_ipv6_unique_local():
    with pytest.raises(UrlSafetyError):
        await validate("http://a.example.com/", resolver=resolver_returning("fd00::1"))


@pytest.mark.asyncio
async def test_rejects_ipv4_mapped_metadata():
    # ::ffff:169.254.169.254 — mapped form must not bypass the v4 check.
    with pytest.raises(UrlSafetyError):
        await validate(
            "http://a.example.com/",
            resolver=resolver_returning("::ffff:169.254.169.254"),
        )


@pytest.mark.asyncio
async def test_rejects_mixed_resolution_if_any_is_reserved():
    # DNS returns both a public and a metadata IP — must reject.
    with pytest.raises(UrlSafetyError):
        await validate(
            "http://a.example.com/",
            resolver=resolver_returning("93.184.216.34", "169.254.169.254"),
        )


@pytest.mark.asyncio
async def test_rejects_unresolvable():
    with pytest.raises(UrlSafetyError, match="could not be resolved"):
        await validate("http://nx.example.com/", resolver=failing_resolver)


# --- dev-friendly positive cases ---

@pytest.mark.asyncio
async def test_accepts_public_https():
    await validate(
        "https://cloud.langfuse.com/api/public/health",
        resolver=resolver_returning("93.184.216.34"),
    )


@pytest.mark.asyncio
async def test_accepts_localhost():
    # Dev workflow: Langfuse / MCP on localhost.
    await validate(
        "http://localhost:3300/api/public/health",
        resolver=resolver_returning("127.0.0.1"),
    )


@pytest.mark.asyncio
async def test_accepts_loopback_literal():
    await validate(
        "http://127.0.0.1:3300/api/public/health",
        resolver=resolver_returning("127.0.0.1"),
    )


@pytest.mark.asyncio
async def test_accepts_rfc1918():
    await validate(
        "https://tools-10/mcp",
        resolver=resolver_returning("10.20.30.40"),
    )


# --- event-loop safety ---

@pytest.mark.asyncio
async def test_default_resolver_does_not_block_the_loop():
    """The default resolver must yield control while DNS is in flight.

    We run validate() against an IP literal — getaddrinfo returns instantly for
    an IP — alongside a heartbeat coroutine that should tick at least once.
    If validate were blocking, the heartbeat would not tick until it returned.
    """
    import asyncio

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        for _ in range(3):
            await asyncio.sleep(0)
            ticks += 1

    await asyncio.gather(
        validate("http://93.184.216.34/", resolver=resolver_returning("93.184.216.34")),
        heartbeat(),
    )

    assert ticks >= 1, "validate() should yield to other coroutines while resolving"
