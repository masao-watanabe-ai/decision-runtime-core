"""
Safe condition evaluator using Python's ast module.

Design contract:
  - eval() and exec() are never called
  - Imports are structurally impossible (only ast.Expression is parsed)
  - Only an explicit whitelist of AST node types is permitted
  - Context access is restricted to a fixed set of top-level variable names
  - Dot notation maps to dict key traversal; missing keys return False
  - Deterministic: same condition + same context always produces the same result
"""
from __future__ import annotations

import ast
import re
from typing import Any


# ------------------------------------------------------------------ #
# Sentinel for missing context values                                  #
# ------------------------------------------------------------------ #

_MISSING: object = object()


# ------------------------------------------------------------------ #
# Configuration constants                                              #
# ------------------------------------------------------------------ #

_ALLOWED_CONTEXT_VARS: frozenset[str] = frozenset(
    {"type", "confidence", "payload", "source", "created_at"}
)

_ALLOWED_AST_TYPES: frozenset[type] = frozenset(
    {
        ast.Expression,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.UnaryOp,
        ast.Not,
        ast.Compare,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.Name,
        ast.Constant,
        ast.Attribute,
        ast.Load,
    }
)

# Pre-compiled patterns for JSON-style literal normalisation
_RE_TRUE: re.Pattern[str] = re.compile(r"\btrue\b")
_RE_FALSE: re.Pattern[str] = re.compile(r"\bfalse\b")
_RE_NULL: re.Pattern[str] = re.compile(r"\bnull\b")


# ------------------------------------------------------------------ #
# Public exception                                                     #
# ------------------------------------------------------------------ #


class ConditionEvaluationError(Exception):
    """Raised when a condition is syntactically invalid or uses a disallowed construct."""


# ------------------------------------------------------------------ #
# Literal normalisation                                                #
# ------------------------------------------------------------------ #


def _normalize_literals(expr: str) -> str:
    """Translate JSON-style boolean/null literals to Python equivalents.

    Operates on whole-word boundaries so string values like "truelove" are
    left intact.
    """
    expr = _RE_TRUE.sub("True", expr)
    expr = _RE_FALSE.sub("False", expr)
    expr = _RE_NULL.sub("None", expr)
    return expr


# ------------------------------------------------------------------ #
# AST safety checker                                                   #
# ------------------------------------------------------------------ #


class _SafeNodeChecker(ast.NodeVisitor):
    """Walks a parsed AST and raises ConditionEvaluationError on any disallowed construct.

    Uses a strict whitelist: every node type that is not explicitly listed
    causes an immediate rejection.
    """

    def visit(self, node: ast.AST) -> None:
        if type(node) not in _ALLOWED_AST_TYPES:
            raise ConditionEvaluationError(
                f"Expression construct '{type(node).__name__}' is not permitted in conditions"
            )
        # Delegate to a specific visitor if defined, otherwise recurse into children
        super().visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id not in _ALLOWED_CONTEXT_VARS:
            raise ConditionEvaluationError(
                f"Variable '{node.id}' is not allowed in conditions; "
                f"permitted variables: {sorted(_ALLOWED_CONTEXT_VARS)}"
            )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__"):
            raise ConditionEvaluationError(
                f"Dunder attribute access '.{node.attr}' is not permitted in conditions"
            )
        self.generic_visit(node)

    def generic_visit(self, node: ast.AST) -> None:
        super().generic_visit(node)


# ------------------------------------------------------------------ #
# AST evaluator                                                        #
# ------------------------------------------------------------------ #


class _SafeEvaluator:
    """Evaluates a pre-validated AST against a bounded context dictionary.

    Only called after _SafeNodeChecker has confirmed the tree is safe.
    The _MISSING sentinel propagates through attribute chains so that
    missing dict keys cause comparisons to return False rather than crash.
    """

    def __init__(self, context: dict[str, Any]) -> None:
        self._ctx = context

    def evaluate(self, node: ast.AST) -> Any:
        handler = getattr(self, f"_eval_{type(node).__name__}", None)
        if handler is None:
            raise ConditionEvaluationError(
                f"No evaluator registered for AST node '{type(node).__name__}'"
            )
        return handler(node)

    # ---- node handlers ------------------------------------------- #

    def _eval_Expression(self, node: ast.Expression) -> Any:
        return self.evaluate(node.body)

    def _eval_BoolOp(self, node: ast.BoolOp) -> bool:
        if isinstance(node.op, ast.And):
            for operand in node.values:
                if not self._coerce_bool(self.evaluate(operand)):
                    return False
            return True
        if isinstance(node.op, ast.Or):
            for operand in node.values:
                if self._coerce_bool(self.evaluate(operand)):
                    return True
            return False
        raise ConditionEvaluationError(
            f"Unsupported boolean operator: {type(node.op).__name__}"
        )

    def _eval_UnaryOp(self, node: ast.UnaryOp) -> bool:
        if isinstance(node.op, ast.Not):
            return not self._coerce_bool(self.evaluate(node.operand))
        raise ConditionEvaluationError(
            f"Unsupported unary operator: {type(node.op).__name__}"
        )

    def _eval_Compare(self, node: ast.Compare) -> bool:
        left = self.evaluate(node.left)
        for op, comparator_node in zip(node.ops, node.comparators):
            right = self.evaluate(comparator_node)
            if not self._apply_cmp(left, op, right):
                return False
            left = right
        return True

    def _eval_Name(self, node: ast.Name) -> Any:
        return self._ctx.get(node.id, _MISSING)

    def _eval_Constant(self, node: ast.Constant) -> Any:
        return node.value

    def _eval_Attribute(self, node: ast.Attribute) -> Any:
        """Resolve dotted access to nested dict lookup.

        payload.customer_tier  →  context["payload"]["customer_tier"]
        """
        obj = self.evaluate(node.value)
        if obj is _MISSING:
            return _MISSING
        if not isinstance(obj, dict):
            return _MISSING
        return obj.get(node.attr, _MISSING)

    # ---- helpers -------------------------------------------------- #

    def _apply_cmp(self, left: Any, op: ast.cmpop, right: Any) -> bool:
        """Apply a comparison operator; return False when either operand is missing."""
        if left is _MISSING or right is _MISSING:
            return False
        try:
            if isinstance(op, ast.Eq):
                return left == right  # type: ignore[operator]
            if isinstance(op, ast.NotEq):
                return left != right  # type: ignore[operator]
            if isinstance(op, ast.Lt):
                return left < right  # type: ignore[operator]
            if isinstance(op, ast.LtE):
                return left <= right  # type: ignore[operator]
            if isinstance(op, ast.Gt):
                return left > right  # type: ignore[operator]
            if isinstance(op, ast.GtE):
                return left >= right  # type: ignore[operator]
        except TypeError:
            # Incompatible types (e.g. str < int) are not an evaluator error;
            # the comparison simply fails.
            return False
        raise ConditionEvaluationError(
            f"Unsupported comparison operator: {type(op).__name__}"
        )

    @staticmethod
    def _coerce_bool(value: Any) -> bool:
        """Convert value to bool, treating the _MISSING sentinel as False."""
        if value is _MISSING:
            return False
        return bool(value)


# ------------------------------------------------------------------ #
# Public API                                                           #
# ------------------------------------------------------------------ #


class ConditionEvaluator:
    """Evaluates condition expressions safely using Python's ast module.

    Guarantees:
      - No eval(), exec(), or compile() is ever called on the expression
      - No imports are possible
      - No function calls are possible
      - No dunder attribute access is possible
      - Only variables listed in _ALLOWED_CONTEXT_VARS can be referenced
      - Missing dict fields yield False rather than raising
      - Behaviour is fully deterministic
    """

    def evaluate(self, condition: str | None, context: dict[str, Any]) -> bool:
        """Evaluate a condition expression against the given context.

        Args:
            condition: A condition string (e.g. ``"confidence >= 0.8"``).
                       ``None`` or an empty/whitespace-only string is treated as
                       unconditionally true.
            context:   A dict whose keys must be a subset of the allowed
                       context variables.

        Returns:
            ``True`` when the condition passes; ``False`` otherwise.

        Raises:
            ConditionEvaluationError: When the expression has invalid syntax
                or uses a disallowed language construct.
        """
        if condition is None or not condition.strip():
            return True

        normalized = _normalize_literals(condition.strip())

        try:
            tree = ast.parse(normalized, mode="eval")
        except SyntaxError as exc:
            raise ConditionEvaluationError(
                f"Condition has invalid syntax ({exc.msg!r}): {condition!r}"
            ) from exc

        _SafeNodeChecker().visit(tree)

        result = _SafeEvaluator(context).evaluate(tree)

        if result is _MISSING:
            return False
        return bool(result)

    def validate_syntax(self, condition: str) -> None:
        """Verify that a condition expression is syntactically valid and uses only
        allowed constructs, without evaluating it.

        Raises:
            ConditionEvaluationError: When the expression is invalid.
        """
        if not condition or not condition.strip():
            return
        normalized = _normalize_literals(condition.strip())
        try:
            tree = ast.parse(normalized, mode="eval")
        except SyntaxError as exc:
            raise ConditionEvaluationError(
                f"Condition has invalid syntax ({exc.msg!r}): {condition!r}"
            ) from exc
        _SafeNodeChecker().visit(tree)
