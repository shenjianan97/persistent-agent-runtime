"""Safe arithmetic evaluator for the Phase 1 calculator tool."""

from __future__ import annotations

import ast
import math
from decimal import Decimal, InvalidOperation

from tools.errors import ToolInputError


MAX_EXPRESSION_LENGTH = 200
MAX_ABS_LITERAL = Decimal("1000000000000")
MAX_ABS_RESULT = Decimal("1000000000000")
MAX_EXPONENT = 12


def evaluate_expression(expression: str) -> int | float:
    """Evaluate a bounded arithmetic expression without using eval()."""
    cleaned = expression.strip()
    if not cleaned:
        raise ToolInputError("Expression must not be empty.")
    if len(cleaned) > MAX_EXPRESSION_LENGTH:
        raise ToolInputError(
            f"Expression exceeds maximum length of {MAX_EXPRESSION_LENGTH} characters."
        )

    try:
        parsed = ast.parse(cleaned, mode="eval")
    except SyntaxError as exc:
        raise ToolInputError("Expression is not valid arithmetic syntax.") from exc

    result = _evaluate_node(parsed.body)
    if not result.is_finite():
        raise ToolInputError("Expression produced a non-finite result.")
    if abs(result) > MAX_ABS_RESULT:
        raise ToolInputError("Expression result exceeds the allowed size limit.")

    normalized = result.normalize()
    if normalized == normalized.to_integral_value():
        return int(normalized)

    value = float(normalized)
    if not math.isfinite(value):
        raise ToolInputError("Expression produced a non-finite result.")
    return value


def _evaluate_node(node: ast.AST) -> Decimal:
    if isinstance(node, ast.Constant):
        return _coerce_literal(node.value)
    if isinstance(node, ast.UnaryOp):
        operand = _evaluate_node(node.operand)
        if isinstance(node.op, ast.UAdd):
            return operand
        if isinstance(node.op, ast.USub):
            return -operand
        raise ToolInputError("Unsupported unary operator.")
    if isinstance(node, ast.BinOp):
        left = _evaluate_node(node.left)
        right = _evaluate_node(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                raise ToolInputError("Division by zero is not allowed.")
            return left / right
        if isinstance(node.op, ast.FloorDiv):
            if right == 0:
                raise ToolInputError("Division by zero is not allowed.")
            return left // right
        if isinstance(node.op, ast.Mod):
            if right == 0:
                raise ToolInputError("Modulo by zero is not allowed.")
            return left % right
        if isinstance(node.op, ast.Pow):
            if right != right.to_integral_value():
                raise ToolInputError("Exponent must be an integer.")
            exponent = int(right)
            if abs(exponent) > MAX_EXPONENT:
                raise ToolInputError(
                    f"Exponent magnitude cannot exceed {MAX_EXPONENT}."
                )
            if left == 0 and exponent < 0:
                raise ToolInputError("Zero cannot be raised to a negative power.")
            result = left**exponent
            if abs(result) > MAX_ABS_RESULT:
                raise ToolInputError("Expression result exceeds the allowed size limit.")
            return result
        raise ToolInputError("Unsupported binary operator.")
    if isinstance(node, ast.Expression):
        return _evaluate_node(node.body)

    raise ToolInputError(
        f"Unsupported syntax: {node.__class__.__name__}. Only arithmetic is allowed."
    )


def _coerce_literal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ToolInputError("Booleans are not allowed in expressions.")
    if not isinstance(value, (int, float)):
        raise ToolInputError("Only numeric literals are allowed.")
    try:
        decimal_value = Decimal(str(value))
    except InvalidOperation as exc:
        raise ToolInputError("Literal could not be parsed as a number.") from exc
    if not decimal_value.is_finite():
        raise ToolInputError("Non-finite literals are not allowed.")
    if abs(decimal_value) > MAX_ABS_LITERAL:
        raise ToolInputError("Numeric literal exceeds the allowed size limit.")
    return decimal_value
