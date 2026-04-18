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
            await self._validate_public_url(current_url)
            response = await self._request_once(current_url)

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
                    f"URL fetch failed temporarily for {current_url} with status {response.status_code}."
                )
            if response.status_code >= 400:
                raise ToolExecutionError(
                    f"URL fetch failed for {current_url} with status {response.status_code}."
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

    async def _validate_public_url(self, url: str) -> None:
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
            return

        try:
            resolved_ips = await self._resolver(host, port)
        except OSError as exc:
            raise ToolTransportError(f"Hostname could not be resolved for {url}.") from exc

        if not resolved_ips:
            raise ToolTransportError(f"Hostname could not be resolved for {url}.")

        for ip_text in resolved_ips:
            _assert_public_ip(ipaddress.ip_address(ip_text))

    async def _request_once(self, url: str) -> _FetchedResponse:
        if self._client is not None:
            return await _stream_response(
                self._client,
                url,
                DEFAULT_REQUEST_HEADERS,
                self._timeout_seconds,
                self._max_body_bytes,
            )

        async with httpx.AsyncClient() as client:
            return await _stream_response(
                client,
                url,
                DEFAULT_REQUEST_HEADERS,
                self._timeout_seconds,
                self._max_body_bytes,
            )


async def _stream_response(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    timeout_seconds: float,
    max_body_bytes: int,
) -> _FetchedResponse:
    try:
        async with client.stream(
            "GET",
            url,
            follow_redirects=False,
            headers=headers,
            timeout=timeout_seconds,
        ) as response:
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
                url=str(response.url),
                status_code=response.status_code,
                headers={key.lower(): value for key, value in response.headers.items()},
                body=bytes(body),
                body_truncated=body_truncated,
            )
    except httpx.TimeoutException as exc:
        raise ToolTransportError(f"URL fetch timed out for {url}.") from exc
    except httpx.HTTPError as exc:
        raise ToolTransportError(f"URL fetch request failed for {url}: {exc}") from exc


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
