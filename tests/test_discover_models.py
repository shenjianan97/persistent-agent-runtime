import importlib.util
from pathlib import Path
import sys
import types

import pytest


def load_discover_models_module():
    module_path = Path(__file__).resolve().parents[1] / "services" / "model-discovery" / "main.py"
    spec = importlib.util.spec_from_file_location("discover_models", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules.setdefault("psycopg", types.SimpleNamespace(connect=None))
    spec.loader.exec_module(module)
    return module


class _FakeCursor:
    """Minimal in-memory cursor used by the embedding-validation tests.

    Keeps the set of ``(provider_id, model_id)`` rows upserted into ``models``
    plus a count of ``agents`` rows matching the memory-enabled predicate.
    """

    def __init__(self, *, memory_enabled_agents: int = 0) -> None:
        self.memory_enabled_agents = memory_enabled_agents
        self.upserted_models: list[dict[str, object]] = []
        self._last_result: object = None

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split()).lower()
        if "from agents" in normalized and "memory" in normalized:
            self._last_result = (self.memory_enabled_agents,)
        elif "insert into models" in normalized:
            self.upserted_models.append(
                {
                    "model_id": params[0],
                    "provider_id": params[1],
                    "display_name": params[2],
                }
            )
        else:
            self._last_result = None

    def fetchone(self):
        return self._last_result

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.committed = True


def test_resolve_model_pricing_returns_explicit_price_for_known_model():
    discover_models = load_discover_models_module()

    pricing = discover_models.resolve_model_pricing("openai", "gpt-4o")

    # OpenAI automatic caching: ``cache_creation`` is always 0 (the provider
    # absorbs the write cost), ``cache_read`` is 50% of input for the 4o
    # family.
    assert pricing == {
        "input": 2_500_000,
        "output": 10_000_000,
        "cache_creation": 0,
        "cache_read": 1_250_000,
    }


def test_resolve_model_pricing_uses_provider_fallback_for_unknown_model():
    discover_models = load_discover_models_module()

    pricing = discover_models.resolve_model_pricing("openai", "gpt-unknown-next")

    assert pricing == discover_models.PRICING_FALLBACKS["openai"]


def test_resolve_model_pricing_uses_provider_fallback_for_bedrock():
    discover_models = load_discover_models_module()

    pricing = discover_models.resolve_model_pricing("bedrock", "future-model")

    assert pricing == discover_models.PRICING_FALLBACKS["bedrock"]


def test_resolve_model_pricing_uses_global_fallback_for_unknown_provider():
    discover_models = load_discover_models_module()

    pricing = discover_models.resolve_model_pricing("some-unknown-provider", "some-model")

    assert pricing == discover_models.GLOBAL_FALLBACK_PRICING


# ---------------------------------------------------------------------------
# Context window resolution
# ---------------------------------------------------------------------------
#
# The worker's compaction pipeline reads ``models.context_window`` to decide
# when Tier 3 summarization fires. Under-populating it fires compaction too
# aggressively; over-populating would blow the provider's real ceiling.
# These tests pin down the lookup-order contract.


def test_resolve_model_context_window_returns_explicit_value():
    discover_models = load_discover_models_module()

    # Known values from CONTEXT_WINDOW_DEFAULTS (PR #80 baseline).
    assert discover_models.resolve_model_context_window("bedrock", "zai.glm-5") == 200_000
    assert discover_models.resolve_model_context_window("anthropic", "claude-opus-4-7") == 1_000_000
    assert discover_models.resolve_model_context_window("openai", "gpt-4.1") == 1_000_000

    # Newly-covered model families (Task 7 — Phase 2 Track 7 follow-up).
    # At least five representative IDs across distinct families, so a
    # regression in any one family's block trips this assertion.
    assert (
        discover_models.resolve_model_context_window("bedrock", "google.gemma-3-27b-it")
        == 128_000
    )
    assert (
        discover_models.resolve_model_context_window(
            "bedrock", "mistral.mistral-large-3-675b-instruct"
        )
        == 128_000
    )
    assert (
        discover_models.resolve_model_context_window("bedrock", "nvidia.nemotron-super-3-120b")
        == 262_144
    )
    assert (
        discover_models.resolve_model_context_window("bedrock", "qwen.qwen3-vl-235b-a22b")
        == 128_000
    )
    assert (
        discover_models.resolve_model_context_window("bedrock", "writer.palmyra-x5-v1:0")
        == 1_040_000
    )
    assert (
        discover_models.resolve_model_context_window("bedrock", "minimax.minimax-m2")
        == 1_000_000
    )
    assert (
        discover_models.resolve_model_context_window("bedrock", "amazon.nova-2-lite-v1:0")
        == 1_000_000
    )
    assert (
        discover_models.resolve_model_context_window("openai", "gpt-5.4") == 272_000
    )
    assert (
        discover_models.resolve_model_context_window("openai", "o1-pro") == 200_000
    )


def test_resolve_model_context_window_falls_back_by_provider():
    discover_models = load_discover_models_module()

    assert (
        discover_models.resolve_model_context_window("openai", "gpt-future-unknown")
        == discover_models.CONTEXT_WINDOW_FALLBACKS["openai"]
    )
    assert (
        discover_models.resolve_model_context_window("anthropic", "claude-future-unknown")
        == discover_models.CONTEXT_WINDOW_FALLBACKS["anthropic"]
    )
    assert (
        discover_models.resolve_model_context_window("bedrock", "future.model-v1:0")
        == discover_models.CONTEXT_WINDOW_FALLBACKS["bedrock"]
    )


def test_resolve_model_context_window_uses_global_fallback_for_unknown_provider():
    discover_models = load_discover_models_module()

    assert (
        discover_models.resolve_model_context_window("some-unknown-provider", "x")
        == discover_models.GLOBAL_FALLBACK_CONTEXT_WINDOW
    )


# ---------------------------------------------------------------------------
# DEACTIVATE_MODEL_IDS — platform deny list
# ---------------------------------------------------------------------------


def test_deactivate_list_excludes_legacy_small_context_openai_models():
    """Regression: the user reported that legacy sub-128K models (gpt-4,
    gpt-3.5-turbo*) were polluting the agent-config dropdown and creating
    unsafe fallback windows. These IDs must stay deny-listed."""
    discover_models = load_discover_models_module()

    required_denied = {
        "gpt-4",
        "gpt-4-0613",
        "gpt-3.5-turbo",
        "gpt-3.5-turbo-16k",
        "gpt-3.5-turbo-0125",
        "gpt-3.5-turbo-1106",
        "gpt-3.5-turbo-instruct",
        "gpt-3.5-turbo-instruct-0914",
    }
    assert required_denied.issubset(discover_models.DEACTIVATE_MODEL_IDS), (
        "deny-list must include all legacy sub-128K OpenAI chat models. "
        f"Missing: {required_denied - discover_models.DEACTIVATE_MODEL_IDS}"
    )


def test_platform_floor_consistent_with_deny_list():
    """Any model in CONTEXT_WINDOW_DEFAULTS with a value below the global
    fallback (128K) must also be on the deny list. Otherwise the worker's
    fallback-to-128K would overshoot the model's real ceiling."""
    discover_models = load_discover_models_module()

    floor = discover_models.GLOBAL_FALLBACK_CONTEXT_WINDOW
    for model_id, window in discover_models.CONTEXT_WINDOW_DEFAULTS.items():
        if window < floor:
            assert model_id in discover_models.DEACTIVATE_MODEL_IDS, (
                f"{model_id} has context_window={window} < floor={floor} "
                "but is not deny-listed — worker fallback would overshoot."
            )


# ---------------------------------------------------------------------------
# Embedding provider validation (Phase 2 Track 5)
# ---------------------------------------------------------------------------
#
# Model-discovery must now also validate the embedding provider's API key at
# startup alongside chat-model keys. The behaviour is:
#
# * If the key is valid: upsert a row into ``models`` for the embedding model
#   (so the write path can compute cost from ``input_microdollars_per_million``).
# * If the key is missing / invalid AND at least one memory-enabled agent
#   exists in the DB: raise to fail startup fast with a clear error.
# * If the key is missing / invalid AND no memory-enabled agents exist:
#   log a warning and continue (memory-disabled agents still operate).


def test_validate_embedding_provider_upserts_row_on_success():
    discover_models = load_discover_models_module()
    cursor = _FakeCursor(memory_enabled_agents=0)
    conn = _FakeConn(cursor)

    def _fake_probe(api_key):
        assert api_key == "sk-test"
        return True

    discover_models.validate_embedding_provider(
        conn,
        api_key="sk-test",
        probe=_fake_probe,
    )

    assert len(cursor.upserted_models) == 1
    row = cursor.upserted_models[0]
    assert row["model_id"] == discover_models.EMBEDDING_MODEL_ID
    assert row["provider_id"] == discover_models.EMBEDDING_PROVIDER_ID


def test_validate_embedding_provider_raises_when_key_missing_and_memory_enabled():
    discover_models = load_discover_models_module()
    cursor = _FakeCursor(memory_enabled_agents=3)
    conn = _FakeConn(cursor)

    with pytest.raises(RuntimeError) as excinfo:
        discover_models.validate_embedding_provider(
            conn,
            api_key=None,
            probe=lambda k: True,
        )

    message = str(excinfo.value)
    assert "embedding" in message.lower()
    assert "3" in message  # count of memory-enabled agents surfaced
    assert cursor.upserted_models == []


def test_validate_embedding_provider_raises_when_probe_fails_and_memory_enabled():
    discover_models = load_discover_models_module()
    cursor = _FakeCursor(memory_enabled_agents=1)
    conn = _FakeConn(cursor)

    def _failing_probe(api_key):
        return False

    with pytest.raises(RuntimeError) as excinfo:
        discover_models.validate_embedding_provider(
            conn,
            api_key="sk-bad",
            probe=_failing_probe,
        )

    assert "embedding" in str(excinfo.value).lower()
    assert cursor.upserted_models == []


def test_validate_embedding_provider_warns_when_key_missing_and_no_memory_agents(capsys):
    discover_models = load_discover_models_module()
    cursor = _FakeCursor(memory_enabled_agents=0)
    conn = _FakeConn(cursor)

    discover_models.validate_embedding_provider(
        conn,
        api_key=None,
        probe=lambda k: True,
    )

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "warn" in combined.lower() or "warning" in combined.lower()
    assert cursor.upserted_models == []


def test_validate_embedding_provider_warns_when_probe_fails_and_no_memory_agents(capsys):
    discover_models = load_discover_models_module()
    cursor = _FakeCursor(memory_enabled_agents=0)
    conn = _FakeConn(cursor)

    discover_models.validate_embedding_provider(
        conn,
        api_key="sk-bad",
        probe=lambda k: False,
    )

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "warn" in combined.lower() or "warning" in combined.lower()
    assert cursor.upserted_models == []
