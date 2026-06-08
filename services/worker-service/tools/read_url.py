"""Bounded URL reader for the Phase 1 read_url tool."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html import unescape
from typing import Final
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from tools.errors import ToolExecutionError, ToolInputError, ToolTransportError


MAX_BODY_BYTES: Final[int] = 1_000_000
MAX_REDIRECTS: Final[int] = 3
DEFAULT_TIMEOUT_SECONDS: Final[float] = 10.0
# Single bounded in-tool retry for transient failures (transient DNS, fetch /
# connect timeout) before the error surfaces to the LLM as a correctable
# ToolMessage. A small fixed delay — deliberately no backoff machinery.
# The retry budget is per resolve/request attempt, so a redirect chain may
# retry once per hop (max MAX_REDIRECTS + 1 hops).
TRANSIENT_RETRY_DELAY_SECONDS: Final[float] = 0.5
# getaddrinfo errnos that mean the *name itself* is bad (NXDOMAIN-style) —
# retrying cannot help, surface immediately. Anything else (EAI_AGAIN, bare
# OSError, empty result) is indeterminate and treated as transient: one retry
# costs little, while a wrong "permanent" call costs the agent a source.
_PERMANENT_DNS_ERRNOS: Final[frozenset[int]] = frozenset(
    errno
    for errno in (
        getattr(socket, "EAI_NONAME", None),  # name does not exist (NXDOMAIN)
        getattr(socket, "EAI_NODATA", None),  # name exists, no address records
    )
    if errno is not None
)
DISALLOWED_HOST_SUFFIXES: Final[tuple[str, ...]] = (".localhost", ".local", ".internal")
DEFAULT_REQUEST_HEADERS: Final[dict[str, str]] = {
    # Some news sites and CDNs block obvious bot headers but allow the same public
    # pages to be fetched with standard browser request metadata.
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}
STRIP_TAGS: Final[tuple[str, ...]] = (
    "script",
    "style",
    "noscript",
    "header",
    "footer",
    "nav",
    "form",
    "aside",
    "svg",
    "canvas",
)

Resolver = Callable[[str, int], Awaitable[list[str]]]


@dataclass(frozen=True)
class ReadUrlResultData:
    final_url: str
    title: str | None
    content: str


@dataclass(frozen=True)
class _FetchedResponse:
    url: str
    status_code: int
    headers: dict[str, str]
    body: bytes
    body_truncated: bool = False


@dataclass(frozen=True)
class _PinnedTarget:
    """A validated connection target for a single URL.

    ``connect_ip`` is the exact IP the socket must connect to — it was
    resolved and asserted public during validation, so the connection cannot
    be rebound to a private/metadata address by a second DNS lookup.
    ``sni_hostname`` is the original hostname used for TLS SNI **and**
    certificate hostname verification (``None`` for a literal-IP URL, where
    the IP is its own pin and the cert legitimately binds to the IP).
    """

    connect_ip: str
    sni_hostname: str | None


class ReadUrlFetcher:
    """Fetch and sanitize readable text from public web pages."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        resolver: Resolver | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_body_bytes: int = MAX_BODY_BYTES,
        max_redirects: int = MAX_REDIRECTS,
    ) -> None:
        self._client = client
        self._resolver = resolver or _default_resolver
        self._timeout_seconds = timeout_seconds
        self._max_body_bytes = max_body_bytes
        self._max_redirects = max_redirects

    async def fetch(self, url: str, max_chars: int) -> ReadUrlResultData:
        original_url = _normalize_url(url)
        current_url = original_url

        for _ in range(self._max_redirects + 1):
            # Resolve + validate + pin in one step: the IP we validate as
            # public is the exact IP we connect to. Re-run per redirect hop so
            # each new host is validated and pinned independently.
            pinned = await self._resolve_and_pin(current_url)
            response = await self._request_with_retry(current_url, pinned)

            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise ToolExecutionError(
                        f"Redirect response for {current_url} did not include a location header."
                    )
                current_url = _normalize_url(urljoin(current_url, location))
                continue

            if response.status_code in {408, 429} or response.status_code >= 500:
                raise ToolTransportError(
                    f"URL fetch failed temporarily for {current_url} with status "
                    f"{response.status_code}. The site may be overloaded or temporarily "
                    "unavailable. Try again later, or use a different URL or source."
                )
            if response.status_code >= 400:
                raise ToolExecutionError(
                    f"URL fetch failed for {current_url} with status "
                    f"{response.status_code}. The URL may be wrong or the page "
                    "inaccessible. Try a different URL or source."
                )

            content_type = response.headers.get("content-type", "")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if not _is_allowed_content_type(media_type):
                raise ToolExecutionError(
                    f"Unsupported content type for {current_url}: {media_type or 'unknown'}."
                )

            title, content = _extract_content(response.body, media_type)
            truncated = _truncate_text(content, max_chars)
            if not truncated:
                raise ToolExecutionError(f"No readable content was extracted from {current_url}.")

            if response.body_truncated and len(content) <= max_chars:
                truncated = _append_fetch_truncation_notice(truncated, max_chars)

            return ReadUrlResultData(
                final_url=response.url,
                title=title,
                content=truncated,
            )

        raise ToolExecutionError(f"Too many redirects while fetching {original_url}.")

    async def _resolve_and_pin(self, url: str) -> _PinnedTarget:
        """Validate *url*'s host and return the pinned connection target.

        Closes the DNS-rebinding TOCTOU: DNS is resolved exactly once here and
        every resolved address is asserted public; the first validated address
        is pinned and used as the socket target by ``_stream_response`` (no
        second, unvalidated lookup at connect time). A literal-IP URL needs no
        DNS — the IP is validated and is its own pin.
        """
        parsed = urlparse(url)
        hostname = parsed.hostname
        if hostname is None:
            raise ToolInputError(f"URL must include a hostname: {url}")

        host = hostname.lower()
        if host == "localhost" or host.endswith(DISALLOWED_HOST_SUFFIXES):
            raise ToolInputError(f"Local and internal hostnames are not allowed: {url}")

        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        literal_ip = _try_parse_ip(host)
        if literal_ip is not None:
            _assert_public_ip(literal_ip)
            # Literal IP: it is its own pin; the cert (if https) legitimately
            # binds to the IP, so no SNI override.
            return _PinnedTarget(connect_ip=host, sni_hostname=None)

        resolved_ips = await self._resolve_with_retry(host, port, url)

        for ip_text in resolved_ips:
            _assert_public_ip(ipaddress.ip_address(ip_text))

        # All resolved addresses are public; pin the first. The original
        # hostname carries TLS SNI + cert verification.
        return _PinnedTarget(connect_ip=resolved_ips[0], sni_hostname=host)

    async def _resolve_with_retry(self, host: str, port: int, url: str) -> list[str]:
        """Resolve *host*, retrying once on transient (EAI_AGAIN-style) failures.

        Permanent failures (NXDOMAIN / EAI_NONAME) surface immediately — the
        agent chose the URL, so the actionable error text goes back to it.
        """
        dns_error_message = (
            f"Hostname could not be resolved for {url}. The URL may be invalid "
            "or the site unavailable. Try a different URL or source."
        )
        for attempt in (0, 1):
            try:
                resolved_ips = await self._resolver(host, port)
            except OSError as exc:
                permanent = (
                    isinstance(exc, socket.gaierror)
                    and exc.errno in _PERMANENT_DNS_ERRNOS
                )
                if permanent or attempt == 1:
                    raise ToolTransportError(dns_error_message) from exc
                await asyncio.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
                continue
            if resolved_ips:
                return resolved_ips
            if attempt == 0:
                await asyncio.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
        raise ToolTransportError(dns_error_message)

    async def _request_with_retry(
        self, url: str, pinned: _PinnedTarget
    ) -> _FetchedResponse:
        """Issue one logical request, retrying once on fetch/connect timeout.

        Only timeouts get the single in-tool retry; other request failures
        (connection refused/reset, protocol errors) surface immediately with
        an actionable message for the LLM. The retry reuses the SAME validated
        *pinned* IP — it never re-resolves, so the rebinding window stays shut
        across the retry.
        """
        try:
            return await self._request_attempt(url, pinned)
        except ToolTransportError as exc:
            if not isinstance(exc.__cause__, httpx.TimeoutException):
                raise
            await asyncio.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
            return await self._request_attempt(url, pinned)

    async def _request_attempt(
        self, url: str, pinned: _PinnedTarget
    ) -> _FetchedResponse:
        # When a caller injects ``self._client``, that client's transport
        # receives the same IP-pinned request (URL host rewritten to the
        # validated IP, original authority in the Host header, original
        # hostname in the ``sni_hostname`` extension). The default-client
        # path below is the security-relevant one: a stock
        # ``httpx.AsyncClient`` would otherwise re-resolve the hostname at
        # connect time — pinning the IP in the request URL prevents that.
        if self._client is not None:
            return await _stream_response(
                self._client,
                url,
                pinned,
                DEFAULT_REQUEST_HEADERS,
                self._timeout_seconds,
                self._max_body_bytes,
            )

        # trust_env=False: we do our own resolve→validate→pin, so egress must
        # not be silently redirected through an env proxy (HTTPS_PROXY/ALL_PROXY)
        # that performs its own DNS — that would re-open the rebinding window the
        # pinning closes. It also avoids the proxy CONNECT path using the pinned
        # IP as the TLS server_hostname (ignoring our sni_hostname extension),
        # which would fail cert verification closed and break env-proxy fetches.
        async with httpx.AsyncClient(trust_env=False) as client:
            return await _stream_response(
                client,
                url,
                pinned,
                DEFAULT_REQUEST_HEADERS,
                self._timeout_seconds,
                self._max_body_bytes,
            )


async def _stream_response(
    client: httpx.AsyncClient,
    url: str,
    pinned: _PinnedTarget,
    headers: dict[str, str],
    timeout_seconds: float,
    max_body_bytes: int,
) -> _FetchedResponse:
    """Fetch *url* by connecting to the validated, pinned IP.

    The socket connects to ``pinned.connect_ip`` (the request URL's host is
    rewritten to the IP, so httpx/httpcore performs no DNS lookup of its own).
    The original authority is restored in the ``Host`` header and the original
    hostname is carried in the ``sni_hostname`` request extension, which
    httpcore uses as the TLS ``server_hostname`` for BOTH SNI and certificate
    hostname verification — so verification binds to the hostname, not the IP,
    and is never disabled.
    """
    logical_url = httpx.URL(url)
    connect_url = logical_url.copy_with(host=pinned.connect_ip)
    request_headers = dict(headers)
    # Original authority (host[:port]) so the origin server routes correctly
    # even though the socket connects to a bare IP.
    request_headers["Host"] = logical_url.netloc.decode("ascii")

    # Build the timeout extension explicitly: client.send() bypasses
    # build_request, so without this it would fall back to the client's
    # default timeout instead of the configured one.
    extensions: dict[str, object] = {
        "timeout": httpx.Timeout(timeout_seconds).as_dict(),
    }
    if logical_url.scheme == "https" and pinned.sni_hostname is not None:
        extensions["sni_hostname"] = pinned.sni_hostname

    request = httpx.Request(
        "GET",
        connect_url,
        headers=request_headers,
        extensions=extensions,
    )

    response: httpx.Response | None = None
    try:
        response = await client.send(
            request,
            stream=True,
            follow_redirects=False,
        )
        body = bytearray()
        body_truncated = False
        async for chunk in response.aiter_bytes():
            remaining = max_body_bytes - len(body)
            if remaining <= 0:
                body_truncated = True
                break
            if len(chunk) > remaining:
                body.extend(chunk[:remaining])
                body_truncated = True
                break
            body.extend(chunk)
        return _FetchedResponse(
            # Report the logical hostname URL — never leak the pinned IP.
            url=url,
            status_code=response.status_code,
            headers={key.lower(): value for key, value in response.headers.items()},
            body=bytes(body),
            body_truncated=body_truncated,
        )
    except httpx.TimeoutException as exc:
        raise ToolTransportError(
            f"URL fetch timed out for {url}. The site may be slow or "
            "unreachable. Try a different URL or source."
        ) from exc
    except httpx.HTTPError as exc:
        raise ToolTransportError(
            f"URL fetch request failed for {url}: {exc}. The site may be "
            "unreachable. Try a different URL or source."
        ) from exc
    finally:
        if response is not None:
            await response.aclose()


async def _default_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.to_thread(
        socket.getaddrinfo,
        host,
        port,
        type=socket.SOCK_STREAM,
    )
    addresses = {item[4][0] for item in infos}
    return sorted(addresses)


def _normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ToolInputError("Only http and https URLs are allowed.")
    if not parsed.hostname:
        raise ToolInputError("URL must include a hostname.")
    if parsed.username or parsed.password:
        raise ToolInputError("Credentials in URLs are not allowed.")
    return parsed.geturl()


def _try_parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _assert_public_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise ToolInputError("Only publicly routable URLs are allowed.")


def _is_allowed_content_type(media_type: str) -> bool:
    if not media_type:
        return True
    if media_type.startswith("text/"):
        return True
    return media_type in {"application/xhtml+xml"}


def _extract_content(body: bytes, media_type: str) -> tuple[str | None, str]:
    text = body.decode("utf-8", errors="replace")
    if media_type == "text/plain":
        return None, _normalize_text(text)

    soup = BeautifulSoup(text, "html.parser")
    for tag in soup.find_all(STRIP_TAGS):
        tag.decompose()

    title = None
    if soup.title and soup.title.string:
        title = _normalize_text(soup.title.string)

    root = soup.find("main") or soup.body or soup
    content = _normalize_text(root.get_text("\n", strip=True))
    if title and content:
        lines = content.splitlines()
        if lines and lines[0].strip().lower() == title.lower():
            content = "\n".join(lines[1:]).strip()
        content = f"# {title}\n\n{content}".strip()
    return title, content


def _normalize_text(text: str) -> str:
    normalized = unescape(text)
    normalized = normalized.replace("\r\n", "\n")
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text

    marker = "\n\n[truncated]"
    if max_chars <= len(marker):
        return text[:max_chars]
    return text[: max_chars - len(marker)].rstrip() + marker


def _append_fetch_truncation_notice(text: str, max_chars: int) -> str:
    marker = "\n\n[source HTML truncated during fetch]"
    if len(text) + len(marker) <= max_chars:
        return text + marker
    return _truncate_text(text + marker, max_chars)
