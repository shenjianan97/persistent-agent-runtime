"""Environment loading helpers for the worker-service tools package."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


TOOLS_DIR = Path(__file__).resolve().parent
WORKER_SERVICE_DIR = TOOLS_DIR.parent


@lru_cache(maxsize=1)
def load_worker_env() -> None:
    """Load `.env` files for local worker-service development.

    Order is current working directory first, then the tools package, then the
    worker-service root.
    Existing process environment variables are never overridden.
    """
    candidates = [
        Path.cwd() / ".env",
        TOOLS_DIR / ".env",
        WORKER_SERVICE_DIR / ".env",
    ]
    seen: set[Path] = set()

    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        load_dotenv(resolved, override=False)
