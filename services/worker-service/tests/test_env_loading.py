"""Tests for local `.env` loading in the tools package."""

from __future__ import annotations

from pathlib import Path

import pytest

import tools.env as env_module
from tools.errors import ToolTransportError
from tools.providers.search import TavilySearchProvider


class TestEnvLoading:
    def test_tavily_provider_instantiates_without_api_key(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """TavilySearchProvider is fail-closed at FIRST SEARCH, not at
        construction — this lets tests / import paths that stub the provider
        out still construct it before a key is injected."""
        env_module.load_worker_env.cache_clear()
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)

        provider = TavilySearchProvider()

        assert provider.provider_name == "tavily"

    async def test_tavily_search_without_api_key_raises_clear_error(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """If no ``TAVILY_API_KEY`` is set, searching raises a
        ToolTransportError that names the offending env var."""
        env_module.load_worker_env.cache_clear()
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)

        provider = TavilySearchProvider()
        with pytest.raises(ToolTransportError, match="TAVILY_API_KEY"):
            await provider.search("anything", 1)
