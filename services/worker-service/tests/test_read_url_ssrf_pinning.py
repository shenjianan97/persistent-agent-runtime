"""SSRF / DNS-rebinding pinning tests for the read_url fetcher.

Pre-existing TOCTOU (found in PR security review): ``_validate_public_url``
resolved the host and asserted public, then the httpx fetch performed its
OWN independent DNS lookup and connected to whatever that second lookup
returned. A DNS-rebinding attacker could answer the validation lookup with a
public IP and the connection lookup with a private/metadata IP
(169.254.169.254, 127.0.0.1, …). The bounded timeout retry and every
redirect hop each reopened the window.

Fix: resolve+validate exactly ONCE per URL, pin the validated public IP, and
connect the socket to that exact IP — while preserving TLS correctness (SNI
and certificate hostname verification still use the original hostname, never
the IP, and verification is never disabled).

These tests prove the connection target equals the validated IP and never the
rebound private IP — for the first request, the timeout retry, and a redirect
hop — plus the one-resolution-per-attempt invariant. TLS cert-verification
binding is proven separately in :mod:`test_read_url_tls_pinning`.
"""

from __future__ import annotations

import httpx
import pytest

from tools.errors import ToolTransportError
from tools.read_url import ReadUrlFetcher


PUBLIC_IP = "93.184.216.34"
PRIVATE_IP = "169.254.169.254"  # link-local / cloud metadata — must never be hit


def _counting_resolver(scripted: list):
    """Resolver popping scripted outcomes (a list of IPs or an Exception)."""
    state = {"count": 0}

    async def resolver(host: str, port: int) -> list[str]:
        state["count"] += 1
        assert scripted, "resolver called more times than scripted"
        outcome = scripted.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return resolver, state


def _spying_client(handler):
    """Wrap a MockTransport handler so every connection target host is recorded."""
    targets: list[str] = []
    sni: list[str | None] = []
    host_headers: list[str | None] = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        targets.append(request.url.host)
        sni.append(request.extensions.get("sni_hostname"))
        host_headers.append(request.headers.get("host"))
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_wrapped))
    return client, targets, sni, host_headers


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"Content-Type": "text/html; charset=utf-8"},
        content="<html><head><title>OK</title></head><body><main><p>Body here.</p></main></body></html>",
    )


@pytest.mark.asyncio
async def test_connects_to_validated_ip_with_hostname_sni_and_host_header() -> None:
    """One resolution; socket targets the validated IP; SNI + Host stay the hostname."""
    resolver, state = _counting_resolver([[PUBLIC_IP]])
    client, targets, sni, host_headers = _spying_client(_ok)

    fetcher = ReadUrlFetcher(client=client, resolver=resolver)
    result = await fetcher.fetch("https://example.com/page", 5000)

    assert state["count"] == 1, "DNS must be resolved exactly once per attempt"
    assert targets == [PUBLIC_IP], "socket must connect to the validated IP, not the hostname"
    assert sni == ["example.com"], "TLS SNI must be the original hostname"
    assert host_headers == ["example.com"], "Host header must be the original authority"
    # final_url is the logical hostname URL, never the pinned IP.
    assert result.final_url == "https://example.com/page"


@pytest.mark.asyncio
async def test_rebinding_first_request_connects_only_to_validated_ip() -> None:
    """Validation sees PUBLIC; a re-resolution would see PRIVATE — never connect to it."""
    resolver, state = _counting_resolver([[PUBLIC_IP], [PRIVATE_IP]])
    client, targets, _sni, _hh = _spying_client(_ok)

    fetcher = ReadUrlFetcher(client=client, resolver=resolver)
    await fetcher.fetch("https://example.com/page", 5000)

    assert state["count"] == 1
    assert targets == [PUBLIC_IP]
    assert PRIVATE_IP not in targets


@pytest.mark.asyncio
async def test_rebinding_on_timeout_retry_reuses_pinned_ip() -> None:
    """The timeout retry reuses the pinned IP — it must not re-resolve to PRIVATE."""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.ReadTimeout("read timed out", request=request)
        return _ok(request)

    # Second list entry is the rebound private answer that must never be used.
    resolver, state = _counting_resolver([[PUBLIC_IP], [PRIVATE_IP]])
    client, targets, _sni, _hh = _spying_client(handler)

    fetcher = ReadUrlFetcher(client=client, resolver=resolver)
    await fetcher.fetch("https://example.com/page", 5000)

    assert state["count"] == 1, "retry must reuse the pinned IP, not re-resolve"
    assert attempts["count"] == 2, "the timeout retry must have fired"
    assert targets == [PUBLIC_IP, PUBLIC_IP]
    assert PRIVATE_IP not in targets


@pytest.mark.asyncio
async def test_rebinding_on_redirect_hop_revalidates_and_pins_each_hop() -> None:
    """Each redirect hop resolves+validates+pins its own host; private never hit."""
    def handler(request: httpx.Request) -> httpx.Response:
        # hop 1 (connected to PUBLIC_IP for a.example) → redirect to b.example
        if request.url.host == PUBLIC_IP:
            return httpx.Response(302, headers={"location": "https://b.example/next"})
        return _ok(request)

    public_b = "1.1.1.1"
    # a.example → PUBLIC_IP (validated), b.example → public_b (validated).
    # A stray third call would return PRIVATE_IP — it must never happen.
    resolver, state = _counting_resolver([[PUBLIC_IP], [public_b], [PRIVATE_IP]])
    client, targets, _sni, _hh = _spying_client(handler)

    fetcher = ReadUrlFetcher(client=client, resolver=resolver)
    await fetcher.fetch("https://a.example/start", 5000)

    assert state["count"] == 2, "one resolution per redirect hop"
    assert targets == [PUBLIC_IP, public_b]
    assert PRIVATE_IP not in targets


@pytest.mark.asyncio
async def test_private_rebind_target_is_never_connected_even_if_resolver_flaps() -> None:
    """Defense-in-depth: validated IPs are all asserted public before pinning."""
    # Validation itself returns a private IP → must be rejected, no connection.
    resolver, state = _counting_resolver([[PRIVATE_IP]])
    client, targets, _sni, _hh = _spying_client(_ok)

    fetcher = ReadUrlFetcher(client=client, resolver=resolver)
    with pytest.raises(Exception):
        await fetcher.fetch("https://evil.example/page", 5000)

    assert targets == [], "no socket may be opened when validation rejects the host"


@pytest.mark.asyncio
async def test_configured_timeout_is_applied_via_extension() -> None:
    """client.send() bypasses build_request, so the timeout must be set explicitly."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions.get("timeout")
        return _ok(request)

    resolver, _state = _counting_resolver([[PUBLIC_IP]])
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = ReadUrlFetcher(client=client, resolver=resolver, timeout_seconds=7.5)
    await fetcher.fetch("https://example.com/page", 5000)

    assert seen["timeout"] == {"connect": 7.5, "read": 7.5, "write": 7.5, "pool": 7.5}


@pytest.mark.asyncio
async def test_literal_ip_url_pins_to_itself_without_sni() -> None:
    """A literal public IP is already its own pin; no SNI override is set."""
    # Resolver must not be consulted for a literal IP.
    async def resolver(host: str, port: int) -> list[str]:  # pragma: no cover
        raise AssertionError("resolver must not be called for a literal-IP URL")

    client, targets, sni, _hh = _spying_client(_ok)
    fetcher = ReadUrlFetcher(client=client, resolver=resolver)
    await fetcher.fetch("https://93.184.216.34/page", 5000)

    assert targets == ["93.184.216.34"]
    assert sni == [None], "no SNI override for a literal-IP URL (cert binds to the IP)"
