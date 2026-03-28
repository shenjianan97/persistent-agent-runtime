"""Worker service configuration."""

from __future__ import annotations

import json
import logging
import os
import socket
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

logger = logging.getLogger(__name__)


def _generate_worker_id() -> str:
    hostname = socket.gethostname()
    pid = os.getpid()
    short_uuid = uuid.uuid4().hex[:8]
    return f"worker-{hostname}-{pid}-{short_uuid}"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


@dataclass(frozen=True)
class WorkerConfig:
    """Immutable configuration for a worker service instance."""

    # Identity
    worker_id: str = field(default_factory=_generate_worker_id)
    worker_pool_id: str = "shared"
    tenant_id: str = "default"

    # Database (no default — must be provided explicitly)
    db_dsn: str = ""

    # Concurrency
    max_concurrent_tasks: int = 10

    # Polling
    poll_backoff_initial_ms: int = 100
    poll_backoff_max_ms: int = 5000
    poll_backoff_multiplier: float = 2.0

    # Lease / heartbeat
    lease_duration_seconds: int = field(default_factory=lambda: _env_int("LEASE_DURATION_SECONDS", 60))
    heartbeat_interval_seconds: int = field(default_factory=lambda: _env_int("HEARTBEAT_INTERVAL_SECONDS", 15))

    # Reaper
    reaper_interval_seconds: int = field(default_factory=lambda: _env_int("REAPER_INTERVAL_SECONDS", 30))
    reaper_jitter_seconds: int = field(default_factory=lambda: _env_int("REAPER_JITTER_SECONDS", 10))

    # Shutdown drain — seconds to wait for in-flight tasks to finish before
    # stopping heartbeats. Default fits within ECS's 30-second SIGTERM window.
    shutdown_drain_seconds: int = field(default_factory=lambda: _env_int("SHUTDOWN_DRAIN_SECONDS", 25))

    # Customer-facing execution observability (Langfuse)
    langfuse_enabled: bool = field(default_factory=lambda: _env_bool("LANGFUSE_ENABLED", False))
    langfuse_host: str | None = field(default_factory=lambda: os.environ.get("LANGFUSE_HOST") or None)
    langfuse_public_key: str | None = field(default_factory=lambda: os.environ.get("LANGFUSE_PUBLIC_KEY") or None)
    langfuse_secret_key: str | None = field(default_factory=lambda: os.environ.get("LANGFUSE_SECRET_KEY") or None)

    def __post_init__(self) -> None:
        if self.langfuse_enabled and (
            not self.langfuse_host
            or not self.langfuse_public_key
            or not self.langfuse_secret_key
        ):
            raise ValueError(
                "LANGFUSE_ENABLED requires LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, and LANGFUSE_SECRET_KEY"
            )

