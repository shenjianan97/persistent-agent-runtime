"""Worker service configuration."""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass, field


def _generate_worker_id() -> str:
    hostname = socket.gethostname()
    pid = os.getpid()
    short_uuid = uuid.uuid4().hex[:8]
    return f"worker-{hostname}-{pid}-{short_uuid}"


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
    lease_duration_seconds: int = 60
    heartbeat_interval_seconds: int = 15

    # Reaper
    reaper_interval_seconds: int = 30
    reaper_jitter_seconds: int = 10
