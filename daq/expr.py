"""Safe arithmetic expression evaluation for derived channels.

Derived channels are defined by expression strings in config.yaml. Those are
compiled once at startup into an AST that only permits arithmetic over named
channel values -- no attribute access, calls to arbitrary functions, or name
lookups outside the supplied scope.
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any, Callable, Mapping

_BIN_OPS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS: dict[type[ast.unaryop], Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_FUNCS: dict[str, Callable[..., float]] = {
    "abs": abs,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "pow": math.pow,
    "floor": math.floor,
    "ceil": math.ceil,
}

_CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e}


class ExpressionError(ValueError):
    """Raised when an expression is malformed or uses a disallowed construct."""


class Expression:
    """A compiled, sandboxed arithmetic expression."""

    def __init__(self, source: str) -> None:
        self.source = source
        try:
            tree = ast.parse(source, mode="eval")
        except SyntaxError as exc:
            raise ExpressionError(f"could not parse {source!r}: {exc.msg}") from exc
        self._node = tree.body
        self.names = _collect_names(self._node)
        _validate(self._node)

    def evaluate(self, scope: Mapping[str, Any]) -> float | None:
        """Evaluate against `scope`.

        Returns None when the result is undefined -- a missing or null input,
        a division by zero, or a domain error such as sqrt(-1). Callers treat
        None as "no reading this sample" rather than as a failure.
        """
        try:
            value = self._eval(self._node, scope)
        except (ZeroDivisionError, ValueError, OverflowError, TypeError):
            return None
        if value is None:
            return None
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            return None
        return value

    def _eval(self, node: ast.AST, scope: Mapping[str, Any]) -> Any:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                return node.value
            raise ExpressionError(f"unsupported literal {node.value!r}")

        if isinstance(node, ast.Name):
            if node.id in _CONSTANTS:
                return _CONSTANTS[node.id]
            if node.id not in scope:
                return None
            return scope[node.id]

        if isinstance(node, ast.BinOp):
            left = self._eval(node.left, scope)
            right = self._eval(node.right, scope)
            if left is None or right is None:
                return None
            return _BIN_OPS[type(node.op)](float(left), float(right))

        if isinstance(node, ast.UnaryOp):
            operand = self._eval(node.operand, scope)
            if operand is None:
                return None
            return _UNARY_OPS[type(node.op)](float(operand))

        if isinstance(node, ast.Call):
            func = _FUNCS[node.func.id]  # type: ignore[union-attr]
            args = [self._eval(a, scope) for a in node.args]
            if any(a is None for a in args):
                return None
            return func(*[float(a) for a in args])

        if isinstance(node, ast.IfExp):
            test = self._eval(node.test, scope)
            if test is None:
                return None
            return self._eval(node.body if test else node.orelse, scope)

        if isinstance(node, ast.Compare):
            left = self._eval(node.left, scope)
            for op, comparator in zip(node.ops, node.comparators):
                right = self._eval(comparator, scope)
                if left is None or right is None:
                    return None
                if not _COMPARE_OPS[type(op)](float(left), float(right)):
                    return False
                left = right
            return True

        raise ExpressionError(f"unsupported expression node {type(node).__name__}")


_COMPARE_OPS: dict[type[ast.cmpop], Callable[[float, float], bool]] = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}

_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.BinOp,
    ast.UnaryOp,
    ast.Call,
    ast.IfExp,
    ast.Compare,
    *_BIN_OPS,
    *_UNARY_OPS,
    *_COMPARE_OPS,
)


def _validate(node: ast.AST) -> None:
    for child in ast.walk(node):
        if not isinstance(child, _ALLOWED_NODES):
            raise ExpressionError(
                f"{type(child).__name__} is not allowed in a channel expression"
            )
        if isinstance(child, ast.Call):
            if not isinstance(child.func, ast.Name) or child.func.id not in _FUNCS:
                allowed = ", ".join(sorted(_FUNCS))
                raise ExpressionError(f"only these functions are available: {allowed}")


def _collect_names(node: ast.AST) -> frozenset[str]:
    return frozenset(
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and child.id not in _CONSTANTS
    ) - frozenset(_FUNCS)
