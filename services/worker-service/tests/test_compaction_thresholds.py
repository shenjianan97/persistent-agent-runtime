"""Unit tests for executor.compaction.thresholds.

Tests resolve_thresholds() with table-driven coverage for common model context
sizes, the minimum-separation guardrail, and ValueError on invalid inputs.
Also covers import-purity.
"""
import sys

import pytest


class TestThresholdsNamedTuple:
    """Thresholds is a NamedTuple with tier1 and tier3 fields."""

    def test_named_tuple_fields(self):
        from executor.compaction.thresholds import Thresholds

        t = Thresholds(tier1=1000, tier3=3000)
        assert t.tier1 == 1000
        assert t.tier3 == 3000

    def test_unpacking(self):
        from executor.compaction.thresholds import Thresholds

        tier1, tier3 = Thresholds(tier1=5000, tier3=8000)
        assert tier1 == 5000
        assert tier3 == 8000

    def test_immutable(self):
        from executor.compaction.thresholds import Thresholds

        t = Thresholds(tier1=1000, tier3=3000)
        with pytest.raises(AttributeError):
            t.tier1 = 999  # type: ignore[misc]


class TestResolveThresholdsTableDriven:
    """Table-driven coverage for common model context windows.

    Formula:
        effective_budget = max(0, model_context_window - OUTPUT_BUDGET_RESERVE_TOKENS)
        tier1 = int(effective_budget * TIER_1_TRIGGER_FRACTION)
        tier3 = int(effective_budget * TIER_3_TRIGGER_FRACTION)
        if tier3 - tier1 < MIN_TIER_SEPARATION_TOKENS:
            tier3 = tier1 + MIN_TIER_SEPARATION_TOKENS

    Constants: OUTPUT_BUDGET_RESERVE=10_000, T1_FRAC=0.50, T3_FRAC=0.75,
               MIN_SEP=2_000
    """

    @pytest.mark.parametrize(
        "context_window, expected_tier1, expected_tier3, min_sep_check",
        [
            # 4K: effective=0 (4000-10000 clamped to 0), tier1=0, tier3=0+2000=2000
            (4_000, 0, 2_000, True),
            # 8K: effective=max(0,8000-10000)=0, tier1=0, tier3=0+2000=2000
            (8_000, 0, 2_000, True),
            # 16K: effective=6000, tier1=3000, tier3=4500, sep=1500<2000 -> tier3=5000
            (16_000, 3_000, 5_000, True),
            # 32K: effective=22000, tier1=11000, tier3=16500, sep=5500>=2000 -> ok
            (32_000, 11_000, 16_500, False),
            # 128K: effective=118000, tier1=59000, tier3=88500
            (128_000, 59_000, 88_500, False),
            # 200K: effective=190000, tier1=95000, tier3=142500
            (200_000, 95_000, 142_500, False),
            # 1M: effective=990000, tier1=495000, tier3=742500
            (1_000_000, 495_000, 742_500, False),
            # 2M: effective=1990000, tier1=995000, tier3=1492500
            (2_000_000, 995_000, 1_492_500, False),
        ],
    )
    def test_resolve_thresholds(
        self, context_window, expected_tier1, expected_tier3, min_sep_check
    ):
        from executor.compaction.defaults import MIN_TIER_SEPARATION_TOKENS
        from executor.compaction.thresholds import resolve_thresholds

        result = resolve_thresholds(context_window)
        assert result.tier1 == expected_tier1, (
            f"context_window={context_window}: "
            f"expected tier1={expected_tier1}, got {result.tier1}"
        )
        assert result.tier3 == expected_tier3, (
            f"context_window={context_window}: "
            f"expected tier3={expected_tier3}, got {result.tier3}"
        )
        # Always verify min-separation guardrail holds
        assert result.tier3 - result.tier1 >= MIN_TIER_SEPARATION_TOKENS, (
            f"context_window={context_window}: "
            f"tier3-tier1={result.tier3 - result.tier1} < {MIN_TIER_SEPARATION_TOKENS}"
        )


class TestResolveThresholdsMinSeparationGuardrail:
    """The guardrail must fire on pathologically small context windows."""

    def test_small_model_tier3_strictly_above_tier1(self):
        from executor.compaction.defaults import MIN_TIER_SEPARATION_TOKENS
        from executor.compaction.thresholds import resolve_thresholds

        result = resolve_thresholds(8_000)
        assert result.tier3 >= result.tier1 + MIN_TIER_SEPARATION_TOKENS

    def test_tier3_never_equal_tier1(self):
        from executor.compaction.thresholds import resolve_thresholds

        result = resolve_thresholds(4_000)
        assert result.tier3 > result.tier1

    def test_tier3_at_least_min_sep_above_tier1_for_16k(self):
        from executor.compaction.defaults import MIN_TIER_SEPARATION_TOKENS
        from executor.compaction.thresholds import resolve_thresholds

        result = resolve_thresholds(16_000)
        assert result.tier3 - result.tier1 >= MIN_TIER_SEPARATION_TOKENS


class TestResolveThresholdsInvalidInputs:
    """ValueError must be raised for non-positive context windows."""

    def test_zero_raises_value_error(self):
        from executor.compaction.thresholds import resolve_thresholds

        with pytest.raises(ValueError, match="model_context_window"):
            resolve_thresholds(0)

    def test_negative_raises_value_error(self):
        from executor.compaction.thresholds import resolve_thresholds

        with pytest.raises(ValueError, match="model_context_window"):
            resolve_thresholds(-1)

    def test_large_negative_raises_value_error(self):
        from executor.compaction.thresholds import resolve_thresholds

        with pytest.raises(ValueError):
            resolve_thresholds(-1_000_000)

    def test_error_message_includes_value(self):
        from executor.compaction.thresholds import resolve_thresholds

        with pytest.raises(ValueError, match="-5"):
            resolve_thresholds(-5)


class TestResolveThresholdsReturnType:
    """resolve_thresholds must return a Thresholds NamedTuple."""

    def test_returns_thresholds_instance(self):
        from executor.compaction.thresholds import Thresholds, resolve_thresholds

        result = resolve_thresholds(200_000)
        assert isinstance(result, Thresholds)

    def test_fields_are_integers(self):
        from executor.compaction.thresholds import resolve_thresholds

        result = resolve_thresholds(128_000)
        assert isinstance(result.tier1, int)
        assert isinstance(result.tier3, int)

    def test_spec_200k_tolerance(self):
        """AC: resolve_thresholds(200_000) returns tier1≈95_000, tier3≈142_500."""
        from executor.compaction.thresholds import resolve_thresholds

        result = resolve_thresholds(200_000)
        assert abs(result.tier1 - 95_000) <= 500
        assert abs(result.tier3 - 142_500) <= 500

    def test_spec_1m_tolerance(self):
        """AC: resolve_thresholds(1_000_000) returns tier1≈495_000, tier3≈742_500."""
        from executor.compaction.thresholds import resolve_thresholds

        result = resolve_thresholds(1_000_000)
        assert abs(result.tier1 - 495_000) <= 500
        assert abs(result.tier3 - 742_500) <= 500


class TestThresholdsImportPurity:
    """thresholds.py must not directly import LangChain, LangGraph, asyncpg, or langfuse.

    The executor parent package legitimately imports LangChain/LangGraph, so
    sys.modules checks are false positives. We verify the module's own source
    has no direct import statements for forbidden libraries.
    """

    def _check_no_direct_import_in_source(self, module_name: str, forbidden: str) -> None:
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

    def test_no_langchain_core(self):
        self._check_no_direct_import_in_source(
            "executor.compaction.thresholds", "langchain_core"
        )

    def test_no_langgraph(self):
        self._check_no_direct_import_in_source(
            "executor.compaction.thresholds", "langgraph"
        )

    def test_no_asyncpg(self):
        self._check_no_direct_import_in_source(
            "executor.compaction.thresholds", "asyncpg"
        )

    def test_no_langfuse(self):
        self._check_no_direct_import_in_source(
            "executor.compaction.thresholds", "langfuse"
        )
