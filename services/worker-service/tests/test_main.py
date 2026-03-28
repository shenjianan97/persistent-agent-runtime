"""Startup guard tests for worker main."""

import os

import pytest

from core.config import WorkerConfig
from main import _assert_langfuse_ready


def test_langfuse_startup_check_is_noop_when_disabled():
    config = WorkerConfig(langfuse_enabled=False)

    _assert_langfuse_ready(config)


def test_langfuse_startup_check_raises_when_unreachable():
    config = WorkerConfig(
        langfuse_enabled=True,
        langfuse_host="http://127.0.0.1:1",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )

    with pytest.raises(RuntimeError, match="Unable to reach Langfuse"):
        _assert_langfuse_ready(config)
