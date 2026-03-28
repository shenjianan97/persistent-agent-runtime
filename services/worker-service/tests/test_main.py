"""Startup guard tests for worker main."""

import os

import pytest

from core.config import WorkerConfig
from main import _apply_langfuse_defaults, _assert_langfuse_ready


def test_apply_langfuse_defaults_sets_local_required_values(monkeypatch):
    for name in ("LANGFUSE_ENABLED", "LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)

    _apply_langfuse_defaults()

    assert os.environ["LANGFUSE_ENABLED"] == "true"
    assert os.environ["LANGFUSE_HOST"] == "http://127.0.0.1:3300"
    assert os.environ["LANGFUSE_PUBLIC_KEY"] == "pk-lf-local"
    assert os.environ["LANGFUSE_SECRET_KEY"] == "sk-lf-local"


def test_langfuse_startup_check_raises_when_unreachable():
    config = WorkerConfig(
        langfuse_enabled=True,
        langfuse_host="http://127.0.0.1:1",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )

    with pytest.raises(RuntimeError, match="Unable to reach Langfuse"):
        _assert_langfuse_ready(config)
