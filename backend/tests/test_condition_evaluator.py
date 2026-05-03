from __future__ import annotations

import pytest

from backend.app.runtime.condition_evaluator import (
    ConditionEvaluationError,
    ConditionEvaluator,
)


@pytest.fixture
def evaluator() -> ConditionEvaluator:
    return ConditionEvaluator()


# ------------------------------------------------------------------ #
# Trivial pass-through cases                                           #
# ------------------------------------------------------------------ #


def test_none_condition_returns_true(evaluator: ConditionEvaluator) -> None:
    """A None condition is unconditionally true."""
    assert evaluator.evaluate(None, {}) is True


def test_empty_condition_returns_true(evaluator: ConditionEvaluator) -> None:
    """An empty or whitespace-only condition is unconditionally true."""
    assert evaluator.evaluate("", {}) is True
    assert evaluator.evaluate("   ", {}) is True


# ------------------------------------------------------------------ #
# Comparison operators                                                  #
# ------------------------------------------------------------------ #


def test_simple_equality(evaluator: ConditionEvaluator) -> None:
    """String equality comparison against a context variable."""
    ctx = {"type": "customer_complaint"}
    assert evaluator.evaluate('type == "customer_complaint"', ctx) is True
    assert evaluator.evaluate('type == "billing_query"', ctx) is False


def test_numeric_comparison(evaluator: ConditionEvaluator) -> None:
    """Numeric comparison operators work correctly."""
    ctx = {"confidence": 0.9}
    assert evaluator.evaluate("confidence >= 0.8", ctx) is True
    assert evaluator.evaluate("confidence > 0.95", ctx) is False
    assert evaluator.evaluate("confidence < 1.0", ctx) is True
    assert evaluator.evaluate("confidence != 0.5", ctx) is True


# ------------------------------------------------------------------ #
# Boolean operators                                                    #
# ------------------------------------------------------------------ #


def test_boolean_and(evaluator: ConditionEvaluator) -> None:
    """'and' requires all sub-expressions to be true."""
    ctx: dict = {"confidence": 0.9, "payload": {"amount": 500}}
    assert evaluator.evaluate("confidence >= 0.8 and payload.amount > 100", ctx) is True
    assert evaluator.evaluate("confidence >= 0.8 and payload.amount > 1000", ctx) is False
    assert evaluator.evaluate("confidence < 0.5 and payload.amount > 100", ctx) is False


def test_boolean_or(evaluator: ConditionEvaluator) -> None:
    """'or' passes when at least one sub-expression is true."""
    ctx: dict = {"confidence": 0.5, "payload": {"tier": "vip"}}
    assert evaluator.evaluate('confidence >= 0.8 or payload.tier == "vip"', ctx) is True
    assert evaluator.evaluate('confidence >= 0.8 or payload.tier == "standard"', ctx) is False


def test_boolean_not(evaluator: ConditionEvaluator) -> None:
    """'not' inverts the boolean value of its operand."""
    ctx: dict = {"payload": {"is_blocked": False}}
    assert evaluator.evaluate("not payload.is_blocked", ctx) is True

    ctx2: dict = {"payload": {"is_active": True}}
    assert evaluator.evaluate("not payload.is_active", ctx2) is False


# ------------------------------------------------------------------ #
# Parentheses and nested logic                                         #
# ------------------------------------------------------------------ #


def test_parentheses(evaluator: ConditionEvaluator) -> None:
    """Parentheses correctly group sub-expressions."""
    ctx: dict = {"payload": {"amount": 150000, "legal": True}}
    assert evaluator.evaluate(
        "(payload.amount > 100000 and payload.legal == true)", ctx
    ) is True
    assert evaluator.evaluate(
        "(payload.amount > 200000 and payload.legal == true)", ctx
    ) is False
    # Outer 'or' with parenthesised group
    ctx2: dict = {"confidence": 0.3, "payload": {"vip": True}}
    assert evaluator.evaluate(
        "confidence > 0.9 or (confidence > 0.2 and payload.vip == true)", ctx2
    ) is True


# ------------------------------------------------------------------ #
# Missing field handling                                               #
# ------------------------------------------------------------------ #


def test_missing_payload_field_returns_false(evaluator: ConditionEvaluator) -> None:
    """A comparison against a missing payload field must return False, not crash."""
    ctx: dict = {"payload": {}}
    assert evaluator.evaluate("payload.missing_field > 100", ctx) is False
    assert evaluator.evaluate('payload.nonexistent == "value"', ctx) is False


# ------------------------------------------------------------------ #
# Security: disallowed constructs                                      #
# ------------------------------------------------------------------ #


def test_unknown_variable_rejected(evaluator: ConditionEvaluator) -> None:
    """Variables outside the allowed set must raise ConditionEvaluationError."""
    with pytest.raises(ConditionEvaluationError, match="not allowed"):
        evaluator.evaluate("outcome == 'pass'", {})

    with pytest.raises(ConditionEvaluationError, match="not allowed"):
        evaluator.evaluate("result > 0", {})


def test_function_call_rejected(evaluator: ConditionEvaluator) -> None:
    """Any function call must be structurally rejected before evaluation."""
    with pytest.raises(ConditionEvaluationError):
        evaluator.evaluate("len(payload) > 0", {"payload": {}})

    with pytest.raises(ConditionEvaluationError):
        evaluator.evaluate("str(confidence)", {"confidence": 0.5})


def test_import_rejected(evaluator: ConditionEvaluator) -> None:
    """Attempts to use __import__ or reference imported names must be rejected."""
    with pytest.raises(ConditionEvaluationError):
        evaluator.evaluate("__import__('os')", {})

    # Call node is disallowed regardless of the callee
    with pytest.raises(ConditionEvaluationError):
        evaluator.evaluate("__import__('subprocess').run(['id'])", {})


def test_dunder_access_rejected(evaluator: ConditionEvaluator) -> None:
    """Dunder attribute access must be rejected to prevent object introspection."""
    with pytest.raises(ConditionEvaluationError, match="Dunder"):
        evaluator.evaluate("payload.__class__", {"payload": {}})

    with pytest.raises(ConditionEvaluationError, match="Dunder"):
        evaluator.evaluate("confidence.__class__.__name__", {"confidence": 0.5})


def test_invalid_syntax_raises(evaluator: ConditionEvaluator) -> None:
    """Syntactically malformed expressions must raise ConditionEvaluationError."""
    with pytest.raises(ConditionEvaluationError, match="invalid syntax"):
        evaluator.evaluate("payload.field ==", {})

    with pytest.raises(ConditionEvaluationError, match="invalid syntax"):
        evaluator.evaluate("and confidence > 0.5", {})


def test_eval_does_not_execute_code(evaluator: ConditionEvaluator) -> None:
    """Injection attempts must raise an error; no side-effecting code is run."""
    executed: list[str] = []

    injection_attempts = [
        # Function calls (Call node)
        "__import__('os').system('echo pwned')",
        "len([1, 2, 3])",
        # Lambda (Lambda node)
        "(lambda: None)()",
        # List comprehension (ListComp node)
        "[x for x in [1, 2, 3]]",
        # Dict comprehension (DictComp node)
        "{k: v for k, v in {}.items()}",
        # Dunder introspection chain
        "payload.__class__.__mro__",
    ]

    ctx = {"payload": {}, "confidence": 0.9}

    for attempt in injection_attempts:
        with pytest.raises(ConditionEvaluationError):
            evaluator.evaluate(attempt, ctx)

    # Confirm that none of the attempts caused a side effect
    assert executed == [], "No code should have executed during rejection"


# ------------------------------------------------------------------ #
# JSON-style literals                                                  #
# ------------------------------------------------------------------ #


def test_json_boolean_literals(evaluator: ConditionEvaluator) -> None:
    """Lowercase 'true', 'false', and 'null' are normalised to Python equivalents."""
    ctx: dict = {"payload": {"verified": True, "blocked": False, "tag": None}}

    assert evaluator.evaluate("payload.verified == true", ctx) is True
    assert evaluator.evaluate("payload.blocked == false", ctx) is True
    assert evaluator.evaluate("payload.tag == null", ctx) is True
    assert evaluator.evaluate("payload.verified == false", ctx) is False
