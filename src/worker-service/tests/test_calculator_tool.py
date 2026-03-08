"""Tests for the Phase 1 calculator tool."""

from __future__ import annotations

import pytest

from tools.calculator import evaluate_expression
from tools.errors import ToolInputError


class TestEvaluateExpression:
    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ("1 + 2 * 3", 7),
            ("(10 - 4) / 3", 2.0),
            ("2 ** 5", 32),
            ("17 % 5", 2),
            ("9 // 2", 4),
            ("-3 + 5", 2),
        ],
    )
    def test_evaluates_supported_arithmetic(self, expression: str, expected: int | float) -> None:
        assert evaluate_expression(expression) == expected

    @pytest.mark.parametrize(
        "expression",
        [
            "",
            "foo + 1",
            "__import__('os')",
            "abs(2)",
            "1 < 2",
            "[1, 2, 3]",
            "True + 1",
            "2 ** 99",
            "1 / 0",
        ],
    )
    def test_rejects_unsafe_or_invalid_expressions(self, expression: str) -> None:
        with pytest.raises(ToolInputError):
            evaluate_expression(expression)
