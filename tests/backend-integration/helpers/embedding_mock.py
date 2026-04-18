"""Fixed-port HTTP mock for the memory embedding provider.

The api-service's ``DefaultMemoryEmbeddingClient`` reads its endpoint URL
at startup via ``@Value("${app.memory.embedding.endpoint:...}")``. To drive
the vector and hybrid RRF code paths from integration tests we need to
point that endpoint at a local server we can control per-test.

This mock runs on a fixed port in a background thread for the life of the
test session. Tests poke ``set_next_vector`` or ``set_next_error`` on the
server handle to program the *next* response; each request then pops the
head of a FIFO queue (or falls back to the default 503-with-zero-vector
behavior if the queue is empty, which matches an unconfigured test).
"""
from __future__ import annotations

import json
import socket
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that re-binds after a TIME_WAIT lingering socket.

    The base class already sets ``allow_reuse_address = True``, which
    enables SO_REUSEADDR. We keep a subclass so we can re-assert that
    explicitly and, in the future, add a socket-level shutdown fuse
    without monkey-patching the stdlib class.
    """

    allow_reuse_address = True

    def server_bind(self) -> None:  # type: ignore[override]
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


class _Response:
    """Programmed response: either a vector or an HTTP error status."""

    __slots__ = ("status", "vector")

    def __init__(self, *, status: int = 200, vector: list[float] | None = None):
        self.status = status
        self.vector = vector


class EmbeddingMockServer:
    """Threaded HTTP embedding provider mock with a per-test programmable queue."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self._host = host
        self._port = port
        self._queue: deque[_Response] = deque()
        self._call_count = 0
        self._lock = threading.Lock()

        server = self  # closed over by the handler below

        class Handler(BaseHTTPRequestHandler):
            # Silence the default noisy access log.
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                return

            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler contract
                length = int(self.headers.get("Content-Length", "0"))
                _ = self.rfile.read(length)  # drain request body
                resp = server._take_next()
                server._bump()
                if resp.status >= 400 or resp.vector is None:
                    self.send_response(resp.status if resp.status >= 400 else 503)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error": "stub"}')
                    return
                body = json.dumps({
                    "data": [{"embedding": list(resp.vector)}],
                    "usage": {"total_tokens": 1},
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = _ReusableThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="embedding-mock",
            daemon=True,
        )

    # ---- lifecycle ----

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self.port}/v1/embeddings"

    # ---- test-facing API ----

    def reset(self) -> None:
        with self._lock:
            self._queue.clear()
            self._call_count = 0

    def set_next_vector(self, vector: list[float]) -> None:
        with self._lock:
            self._queue.append(_Response(status=200, vector=list(vector)))

    def set_next_error(self, status: int) -> None:
        with self._lock:
            self._queue.append(_Response(status=status, vector=None))

    @property
    def call_count(self) -> int:
        with self._lock:
            return self._call_count

    # ---- internal ----

    def _take_next(self) -> _Response:
        with self._lock:
            if self._queue:
                return self._queue.popleft()
        # Unprogrammed request — default to a provider-unavailable response so
        # accidental hits surface visibly.
        return _Response(status=503)

    def _bump(self) -> None:
        with self._lock:
            self._call_count += 1
