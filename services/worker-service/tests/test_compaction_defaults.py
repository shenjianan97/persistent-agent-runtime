"""Unit tests for executor.compaction.defaults.

Tests import-purity, constant values, sanity-check assertions,
and the lazy env-var helper.
"""
import importlib
import os
import sys


class TestConstants:
    """Verify all platform constants have their spec-mandated values."""

    def test_tier1_trigger_fraction(self):
        from executor.compaction.defaults import TIER_1_TRIGGER_FRACTION

        assert TIER_1_TRIGGER_FRACTION == 0.50

    def test_tier3_trigger_fraction(self):
        from executor.compaction.defaults import TIER_3_TRIGGER_FRACTION

        assert TIER_3_TRIGGER_FRACTION == 0.75

    def test_output_budget_reserve_tokens(self):
        from executor.compaction.defaults import OUTPUT_BUDGET_RESERVE_TOKENS

        assert OUTPUT_BUDGET_RESERVE_TOKENS == 10_000

    def test_min_tier_separation_tokens(self):
        from executor.compaction.defaults import MIN_TIER_SEPARATION_TOKENS

        assert MIN_TIER_SEPARATION_TOKENS == 2_000

    def test_keep_tool_uses(self):
        from executor.compaction.defaults import KEEP_TOOL_USES

        assert KEEP_TOOL_USES == 3

    def test_offload_threshold_bytes(self):
        """Track 7 Follow-up (Task 4) — replaces PER_TOOL_RESULT_CAP_BYTES."""
        from executor.compaction.defaults import OFFLOAD_THRESHOLD_BYTES

        assert OFFLOAD_THRESHOLD_BYTES == 20_000

    def test_truncatable_arg_keys(self):
        """Track 7 Follow-up (Task 4) — the offload arg-key allowlist."""
        from executor.compaction.defaults import TRUNCATABLE_ARG_KEYS

        assert TRUNCATABLE_ARG_KEYS == frozenset(
            {"content", "new_string", "old_string", "text", "body"}
        )

    def test_arg_truncation_cap_bytes(self):
        from executor.compaction.defaults import ARG_TRUNCATION_CAP_BYTES

        assert ARG_TRUNCATION_CAP_BYTES == 1_000

    def test_summarizer_max_retries(self):
        from executor.compaction.defaults import SUMMARIZER_MAX_RETRIES

        assert SUMMARIZER_MAX_RETRIES == 2

    def test_tier3_max_firings_per_task(self):
        from executor.compaction.defaults import TIER_3_MAX_FIRINGS_PER_TASK

        assert TIER_3_MAX_FIRINGS_PER_TASK == 10

    def test_platform_default_summarizer_model(self):
        from executor.compaction.defaults import PLATFORM_DEFAULT_SUMMARIZER_MODEL

        assert PLATFORM_DEFAULT_SUMMARIZER_MODEL == "claude-haiku-4-5"

    def test_platform_default_summarizer_model_env(self):
        from executor.compaction.defaults import PLATFORM_DEFAULT_SUMMARIZER_MODEL_ENV

        assert PLATFORM_DEFAULT_SUMMARIZER_MODEL_ENV == "CONTEXT_MGMT_DEFAULT_SUMMARIZER_MODEL"


class TestPlatformExcludeTools:
    """PLATFORM_EXCLUDE_TOOLS must be a frozenset containing every tool
    whose ToolMessages Tier 1 must never age out.

    After issue #102 the set contains seven names: the two canonical memory
    tools ``note_finding`` and ``commit_memory``, their deprecated aliases
    ``memory_note`` / ``save_memory`` (remove once the aliases retire),
    ``request_human_input``, ``memory_search``, ``task_history_get``.
    """

    def test_is_frozenset(self):
        from executor.compaction.defaults import PLATFORM_EXCLUDE_TOOLS

        assert isinstance(PLATFORM_EXCLUDE_TOOLS, frozenset)

    def test_contains_note_finding(self):
        from executor.compaction.defaults import PLATFORM_EXCLUDE_TOOLS

        assert "note_finding" in PLATFORM_EXCLUDE_TOOLS

    def test_contains_memory_note_alias(self):
        """Legacy alias kept until the alias tool is retired (issue #102)."""
        from executor.compaction.defaults import PLATFORM_EXCLUDE_TOOLS

        assert "memory_note" in PLATFORM_EXCLUDE_TOOLS

    def test_contains_commit_memory(self):
        """Canonical terminal-commit tool (issue #102). Its confirmation
        ToolMessage must survive Tier 1 clearing so the agent retains
        evidence it opted in."""
        from executor.compaction.defaults import PLATFORM_EXCLUDE_TOOLS

        assert "commit_memory" in PLATFORM_EXCLUDE_TOOLS

    def test_contains_save_memory_alias(self):
        """Legacy alias kept until the alias tool is retired (issue #102)."""
        from executor.compaction.defaults import PLATFORM_EXCLUDE_TOOLS

        assert "save_memory" in PLATFORM_EXCLUDE_TOOLS

    def test_contains_request_human_input(self):
        from executor.compaction.defaults import PLATFORM_EXCLUDE_TOOLS

        assert "request_human_input" in PLATFORM_EXCLUDE_TOOLS

    def test_contains_memory_search(self):
        from executor.compaction.defaults import PLATFORM_EXCLUDE_TOOLS

        assert "memory_search" in PLATFORM_EXCLUDE_TOOLS

    def test_contains_task_history_get(self):
        from executor.compaction.defaults import PLATFORM_EXCLUDE_TOOLS

        assert "task_history_get" in PLATFORM_EXCLUDE_TOOLS

    def test_exactly_seven_tools(self):
        from executor.compaction.defaults import PLATFORM_EXCLUDE_TOOLS

        # Seven: note_finding, memory_note (alias), commit_memory,
        # save_memory (alias), request_human_input, memory_search,
        # task_history_get.
        assert len(PLATFORM_EXCLUDE_TOOLS) == 7

    def test_immutable(self):
        from executor.compaction.defaults import PLATFORM_EXCLUDE_TOOLS

        # frozenset has no add/discard — verify it's truly immutable
        assert not hasattr(PLATFORM_EXCLUDE_TOOLS, "add")


class TestTruncatableToolArgKeys:
    """TRUNCATABLE_TOOL_ARG_KEYS must be a frozenset with the five arg keys."""

    def test_is_frozenset(self):
        from executor.compaction.defaults import TRUNCATABLE_TOOL_ARG_KEYS

        assert isinstance(TRUNCATABLE_TOOL_ARG_KEYS, frozenset)

    def test_contains_content(self):
        from executor.compaction.defaults import TRUNCATABLE_TOOL_ARG_KEYS

        assert "content" in TRUNCATABLE_TOOL_ARG_KEYS

    def test_contains_new_string(self):
        from executor.compaction.defaults import TRUNCATABLE_TOOL_ARG_KEYS

        assert "new_string" in TRUNCATABLE_TOOL_ARG_KEYS

    def test_contains_old_string(self):
        from executor.compaction.defaults import TRUNCATABLE_TOOL_ARG_KEYS

        assert "old_string" in TRUNCATABLE_TOOL_ARG_KEYS

    def test_contains_text(self):
        from executor.compaction.defaults import TRUNCATABLE_TOOL_ARG_KEYS

        assert "text" in TRUNCATABLE_TOOL_ARG_KEYS

    def test_contains_body(self):
        from executor.compaction.defaults import TRUNCATABLE_TOOL_ARG_KEYS

        assert "body" in TRUNCATABLE_TOOL_ARG_KEYS

    def test_exactly_five_keys(self):
        from executor.compaction.defaults import TRUNCATABLE_TOOL_ARG_KEYS

        assert len(TRUNCATABLE_TOOL_ARG_KEYS) == 5


class TestGetPlatformDefaultSummarizerModel:
    """Lazy env-var helper must honor the override and fall back correctly."""

    def test_returns_default_without_env_override(self):
        from executor.compaction.defaults import (
            PLATFORM_DEFAULT_SUMMARIZER_MODEL,
            PLATFORM_DEFAULT_SUMMARIZER_MODEL_ENV,
            get_platform_default_summarizer_model,
        )

        # Ensure env var is NOT set
        os.environ.pop(PLATFORM_DEFAULT_SUMMARIZER_MODEL_ENV, None)
        result = get_platform_default_summarizer_model()
        assert result == PLATFORM_DEFAULT_SUMMARIZER_MODEL

    def test_returns_override_when_env_set(self):
        from executor.compaction.defaults import (
            PLATFORM_DEFAULT_SUMMARIZER_MODEL_ENV,
            get_platform_default_summarizer_model,
        )

        os.environ[PLATFORM_DEFAULT_SUMMARIZER_MODEL_ENV] = "claude-opus-4-5"
        try:
            result = get_platform_default_summarizer_model()
            assert result == "claude-opus-4-5"
        finally:
            os.environ.pop(PLATFORM_DEFAULT_SUMMARIZER_MODEL_ENV, None)

    def test_override_value_is_arbitrary_string(self):
        from executor.compaction.defaults import (
            PLATFORM_DEFAULT_SUMMARIZER_MODEL_ENV,
            get_platform_default_summarizer_model,
        )

        os.environ[PLATFORM_DEFAULT_SUMMARIZER_MODEL_ENV] = "my-custom-model-xyz"
        try:
            result = get_platform_default_summarizer_model()
            assert result == "my-custom-model-xyz"
        finally:
            os.environ.pop(PLATFORM_DEFAULT_SUMMARIZER_MODEL_ENV, None)


class TestImportPurity:
    """defaults.py must not directly import LangChain, LangGraph, asyncpg, or langfuse.

    The spec constraint is that these modules have no side-effecting imports at
    the module scope. The executor parent package itself legitimately imports
    LangChain/LangGraph (it's the core executor), so sys.modules checks would
    be a false positive. Instead, we verify the module's own __dict__ contains
    no references to these libraries, and that the source file has no direct
    import statements for them.
    """

    def _check_no_direct_import_in_source(self, module_name: str, forbidden: str) -> None:
        """Verify the source file for module_name has no direct import of forbidden."""
        import ast
        import importlib.util

        spec = importlib.util.find_spec(module_name)
        assert spec is not None and spec.origin is not None
        with open(spec.origin) as f:
            source = f.read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden), (
                        f"{module_name} directly imports {alias.name} "
                        f"(forbidden: {forbidden})"
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith(forbidden):
                    raise AssertionError(
                        f"{module_name} imports from {node.module} "
                        f"(forbidden: {forbidden})"
                    )

    def test_no_langchain_core_direct_import(self):
        self._check_no_direct_import_in_source(
            "executor.compaction.defaults", "langchain_core"
        )

    def test_no_langgraph_direct_import(self):
        self._check_no_direct_import_in_source(
            "executor.compaction.defaults", "langgraph"
        )

    def test_no_asyncpg_direct_import(self):
        self._check_no_direct_import_in_source(
            "executor.compaction.defaults", "asyncpg"
        )

    def test_no_langfuse_direct_import(self):
        self._check_no_direct_import_in_source(
            "executor.compaction.defaults", "langfuse"
        )

    def test_no_langchain_in_module_namespace(self):
        """The module's own namespace must not bind any langchain symbol."""
        import executor.compaction.defaults as mod

        ns = vars(mod)
        for name, val in ns.items():
            module_name = getattr(val, "__module__", "") or ""
            assert not module_name.startswith("langchain"), (
                f"executor.compaction.defaults binds '{name}' from {module_name}"
            )

    def test_import_produces_no_errors(self):
        """Importing the module (including assert guards) should not raise."""
        # The module is already imported; re-triggering via importlib verifies
        # re-import is also clean.
        import executor.compaction.defaults as mod

        importlib.reload(mod)  # reload to re-run module-level asserts
