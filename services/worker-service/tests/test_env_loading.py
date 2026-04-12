"""Tests for local `.env` loading in the tools package."""

from __future__ import annotations

from pathlib import Path

import tools.env as env_module
from tools.providers.search import DuckDuckGoSearchProvider


class TestEnvLoading:
    def test_duckduckgo_provider_instantiates_without_env_vars(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """DuckDuckGoSearchProvider requires no API key — should instantiate cleanly."""
        env_module.load_worker_env.cache_clear()
        monkeypatch.chdir(tmp_path)

        provider = DuckDuckGoSearchProvider()

        assert provider.provider_name == "duckduckgo"
