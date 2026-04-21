"""Structured logging and metrics for the worker service.

Provides structured logging with mandatory labels (task_id, worker_id, node_name)
and metric emission primitives. Uses structlog for structured JSON output.

**Worker-only.** The API service has its own logging configuration and does
not share this module. The ``WORKER_LOG_LEVEL`` env var read below controls
the worker's structlog filter exclusively; if the API service later adopts a
shared logger, the env var name will want a rename to match.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import structlog


_WORKER_LOG_LEVEL_ENV = "WORKER_LOG_LEVEL"


def _resolve_level() -> int:
    """Resolve the structlog filter level from ``WORKER_LOG_LEVEL``.

    Accepts ``DEBUG``/``INFO``/``WARNING``/``ERROR``/``CRITICAL`` (case-
    insensitive). Falls back to ``logging.INFO`` on unset or invalid values,
    logging a one-time stdlib warning so the misconfig is visible. The default
    keeps production behaviour unchanged — DEBUG is strictly opt-in for local
    dev (see ``docs/LOCAL_DEVELOPMENT.md`` § Tracking a running task).
    """
    raw = os.environ.get(_WORKER_LOG_LEVEL_ENV)
    if raw is None or raw == "":
        return logging.INFO
    candidate = raw.strip().upper()
    level = logging.getLevelName(candidate)
    # ``getLevelName`` returns the numeric level for known names and the
    # string ``"Level <n>"`` for unknown input. Guard on both.
    if isinstance(level, int):
        return level
    logging.getLogger(__name__).warning(
        "Unknown %s=%r; falling back to INFO", _WORKER_LOG_LEVEL_ENV, raw
    )
    return logging.INFO


def configure_logging() -> None:
    """Configure structlog for JSON structured output."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_resolve_level()),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(worker_id: str, **initial_context: Any) -> structlog.stdlib.BoundLogger:
    """Get a structured logger bound with worker_id."""
    return structlog.get_logger(worker_id=worker_id, **initial_context)


# -- Lifecycle event constants --

TASK_CLAIMED = "TASK_CLAIMED"
LEASE_REVOKED = "LEASE_REVOKED"
TASK_DEAD_LETTERED = "TASK_DEAD_LETTERED"
TASK_COMPLETED = "TASK_COMPLETED"
TASK_REQUEUED = "TASK_REQUEUED"
REAPER_LEASE_EXPIRED = "REAPER_LEASE_EXPIRED"
REAPER_TASK_TIMEOUT = "REAPER_TASK_TIMEOUT"
REAPER_DEAD_LETTERED = "REAPER_DEAD_LETTERED"
HEARTBEAT_SENT = "HEARTBEAT_SENT"
POLL_EMPTY = "POLL_EMPTY"


# -- Metrics collection --

@dataclass
class MetricsCollector:
    """Simple in-process metrics collector.

    Provides counters and gauges that can be consumed by an OpenTelemetry
    exporter or read directly in tests. Thread-safe via asyncio single-thread model.
    """

    _counters: dict[str, float] = field(default_factory=dict)
    _gauges: dict[str, float] = field(default_factory=dict)
    _last_updated: dict[str, float] = field(default_factory=dict)

    def increment(self, name: str, value: float = 1.0, **labels: str) -> None:
        """Increment a counter metric."""
        key = self._make_key(name, labels)
        self._counters[key] = self._counters.get(key, 0) + value
        self._last_updated[key] = time.monotonic()

    def set_gauge(self, name: str, value: float, **labels: str) -> None:
        """Set a gauge metric."""
        key = self._make_key(name, labels)
        self._gauges[key] = value
        self._last_updated[key] = time.monotonic()

    def get_counter(self, name: str, **labels: str) -> float:
        """Read a counter value."""
        key = self._make_key(name, labels)
        return self._counters.get(key, 0)

    def get_gauge(self, name: str, **labels: str) -> float:
        """Read a gauge value."""
        key = self._make_key(name, labels)
        return self._gauges.get(key, 0)

    @staticmethod
    def _make_key(name: str, labels: dict[str, str]) -> str:
        if not labels:
            return name
        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"
