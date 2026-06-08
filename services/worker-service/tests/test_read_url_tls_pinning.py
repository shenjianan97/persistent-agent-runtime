"""TLS-correctness proof for read_url's IP pinning.

When the fetcher connects to a pinned IP (closing the DNS-rebinding TOCTOU),
the TLS handshake must STILL verify the server certificate against the
original *hostname* — never the IP — and verification must never be disabled.
A wrong implementation here (verifying against the IP, or disabling
verification) would be a worse vulnerability than the one being fixed.

These tests stand up a real local TLS server on 127.0.0.1 (ephemeral port,
worktree-concurrency-safe) presenting a cert for ``pinned.test`` signed by an
in-test CA, then drive the production connection helper with the IP pinned to
127.0.0.1:

- success: SNI/cert host = ``pinned.test`` (matches cert) → handshake succeeds
  even though the socket connected to 127.0.0.1, proving verification binds to
  the hostname, not the IP.
- mismatch: SNI/cert host = ``wrong.test`` (cert is for ``pinned.test``) →
  ``ssl.SSLCertVerificationError`` surfaces as ``ToolTransportError``, proving
  verification is genuinely enforced (no silent downgrade).

No external network; loopback only.
"""

from __future__ import annotations

import asyncio
import datetime
import ssl

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from tools.errors import ToolTransportError
from tools.read_url import DEFAULT_REQUEST_HEADERS, _PinnedTarget, _stream_response

CERT_HOST = "pinned.test"


def _make_ca_and_leaf(leaf_host: str):
    """Return (ca_pem_bytes, leaf_cert_pem_bytes, leaf_key_pem_bytes)."""
    one_day = datetime.timedelta(days=1)
    now = datetime.datetime.now(datetime.timezone.utc)

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "read_url-test-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - one_day)
        .not_valid_after(now + one_day)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, leaf_host)]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - one_day)
        .not_valid_after(now + one_day)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(leaf_host)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM)
    leaf_cert_pem = leaf_cert.public_bytes(serialization.Encoding.PEM)
    leaf_key_pem = leaf_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return ca_pem, leaf_cert_pem, leaf_key_pem


async def _start_tls_server(tmp_path, leaf_cert_pem: bytes, leaf_key_pem: bytes):
    cert_file = tmp_path / "leaf.pem"
    key_file = tmp_path / "leaf.key"
    cert_file.write_bytes(leaf_cert_pem)
    key_file.write_bytes(leaf_key_pem)

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
            body = (
                b"<html><head><title>TLS OK</title></head>"
                b"<body><main><p>secure body</p></main></body></html>"
            )
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/html; charset=utf-8\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
        except Exception:  # pragma: no cover - best-effort test server
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0, ssl=server_ctx)
    port = server.sockets[0].getsockname()[1]
    return server, port


def _client_trusting(ca_pem: bytes) -> httpx.AsyncClient:
    client_ctx = ssl.create_default_context()
    client_ctx.load_verify_locations(cadata=ca_pem.decode("ascii"))
    # check_hostname stays True (default) — the whole point of the test.
    return httpx.AsyncClient(verify=client_ctx)


@pytest.mark.asyncio
async def test_cert_verified_against_hostname_not_pinned_ip(tmp_path) -> None:
    ca_pem, leaf_cert_pem, leaf_key_pem = _make_ca_and_leaf(CERT_HOST)
    server, port = await _start_tls_server(tmp_path, leaf_cert_pem, leaf_key_pem)
    client = _client_trusting(ca_pem)
    try:
        # Connect to 127.0.0.1 (the pin) but verify the cert against CERT_HOST.
        # The cert has NO IP SAN, so success proves verification binds to the
        # hostname carried via SNI, not the connection IP.
        pinned = _PinnedTarget(connect_ip="127.0.0.1", sni_hostname=CERT_HOST)
        resp = await _stream_response(
            client,
            f"https://{CERT_HOST}:{port}/",
            pinned,
            DEFAULT_REQUEST_HEADERS,
            5.0,
            1_000_000,
        )
        assert resp.status_code == 200
        assert b"secure body" in resp.body
        # final_url reports the logical hostname URL, never the pinned IP.
        assert resp.url == f"https://{CERT_HOST}:{port}/"
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_cert_hostname_mismatch_is_rejected(tmp_path) -> None:
    ca_pem, leaf_cert_pem, leaf_key_pem = _make_ca_and_leaf(CERT_HOST)
    server, port = await _start_tls_server(tmp_path, leaf_cert_pem, leaf_key_pem)
    client = _client_trusting(ca_pem)
    try:
        # Cert is for pinned.test; verify against wrong.test → must be rejected.
        pinned = _PinnedTarget(connect_ip="127.0.0.1", sni_hostname="wrong.test")
        with pytest.raises(ToolTransportError):
            await _stream_response(
                client,
                f"https://wrong.test:{port}/",
                pinned,
                DEFAULT_REQUEST_HEADERS,
                5.0,
                1_000_000,
            )
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()
