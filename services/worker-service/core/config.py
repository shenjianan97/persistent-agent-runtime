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


@dataclass(frozen=True)
class ModelPricing:
    input_microdollars_per_million: int
    output_microdollars_per_million: int


DEFAULT_MODEL_PRICING_FILE = (
    Path(__file__).resolve().parent.parent / "config" / "model_pricing.json"
)


def _default_model_pricing_file() -> str:
    return os.environ.get("MODEL_PRICING_FILE", str(DEFAULT_MODEL_PRICING_FILE))


def _coerce_model_pricing(model_name: str, payload: object) -> ModelPricing:
    if not isinstance(payload, dict):
        raise ValueError(f"Pricing entry for model {model_name!r} must be an object.")

    input_rate = payload.get("input_microdollars_per_million")
    output_rate = payload.get("output_microdollars_per_million")
    if not isinstance(input_rate, int) or not isinstance(output_rate, int):
        raise ValueError(
            f"Pricing entry for model {model_name!r} must define integer input/output rates."
        )

    return ModelPricing(
        input_microdollars_per_million=input_rate,
        output_microdollars_per_million=output_rate,
    )


def load_model_pricing(path: str | Path) -> Mapping[str, ModelPricing]:
    pricing_path = Path(path)
    try:
        raw_payload = json.loads(pricing_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Model pricing file not found: {pricing_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model pricing file is not valid JSON: {pricing_path}") from exc

    if not isinstance(raw_payload, dict):
        raise ValueError(f"Model pricing file must contain a JSON object: {pricing_path}")

    normalized: dict[str, ModelPricing] = {}
    for model_name, payload in raw_payload.items():
        normalized[model_name] = _coerce_model_pricing(model_name, payload)

    if not normalized:
        raise ValueError(f"Model pricing file is empty: {pricing_path}")

    logger.info("Loaded pricing for %s models from %s", len(normalized), pricing_path)
    return MappingProxyType(normalized)


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

    # Pricing
    model_pricing_file: str = field(default_factory=_default_model_pricing_file)
    model_pricing: Mapping[str, ModelPricing] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.model_pricing:
            normalized: dict[str, ModelPricing] = {}
            for model_name, payload in self.model_pricing.items():
                if isinstance(payload, ModelPricing):
                    normalized[model_name] = payload
                    continue
                normalized[model_name] = _coerce_model_pricing(model_name, payload)
            object.__setattr__(self, "model_pricing", MappingProxyType(normalized))
            return

        object.__setattr__(
            self,
            "model_pricing",
            load_model_pricing(self.model_pricing_file),
        )
