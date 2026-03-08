"""Tests for local `.env` loading in the tools package."""

from __future__ import annotations

from pathlib import Path

import tools.env as env_module
from tools.providers.search import TavilySearchProvider


class TestEnvLoading:
    def test_tavily_provider_loads_api_key_from_dotenv(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        env_module.load_worker_env.cache_clear()

        (tmp_path / ".env").write_text("TAVILY_API_KEY=dotenv-test-key\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        provider = TavilySearchProvider()

        assert provider._api_key == "dotenv-test-key"

    def test_existing_environment_variable_wins_over_dotenv(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("TAVILY_API_KEY", "process-env-key")
        env_module.load_worker_env.cache_clear()

        (tmp_path / ".env").write_text("TAVILY_API_KEY=dotenv-test-key\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        provider = TavilySearchProvider()

        assert provider._api_key == "process-env-key"

    def test_tavily_provider_loads_api_key_from_tools_dotenv(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        tools_dir = tmp_path / "tools"
        worker_service_dir = tmp_path
        work_dir = tmp_path / "unrelated"
        tools_dir.mkdir()
        work_dir.mkdir()

        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        env_module.load_worker_env.cache_clear()
        monkeypatch.setattr(env_module, "TOOLS_DIR", tools_dir)
        monkeypatch.setattr(env_module, "WORKER_SERVICE_DIR", worker_service_dir)

        (tools_dir / ".env").write_text("TAVILY_API_KEY=tools-dotenv-key\n", encoding="utf-8")
        monkeypatch.chdir(work_dir)

        provider = TavilySearchProvider()

        assert provider._api_key == "tools-dotenv-key"
