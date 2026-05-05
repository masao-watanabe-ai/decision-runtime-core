from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from backend.app.models.boundary import BoundaryResult, RuntimeBoundaryResult
from backend.app.models.decision import DecisionOutcome, DecisionResult, DecisionStatus
from backend.app.models.flow import DecisionFlow, DecisionNode, NodeType
from backend.app.models.runtime import RuntimeState
from backend.app.models.signal import Signal, SignalValueType
from backend.app.runtime.boundary_engine import BoundaryEngine, to_runtime_boundary_results


# ------------------------------------------------------------------ #
# Test-object factories                                                #
# ------------------------------------------------------------------ #


def _signal(payload: dict[str, Any] | None = None, confidence: float = 1.0) -> Signal:
    return Signal(
        name="test_signal",
        value_type=SignalValueType.JSON,
        type="test_event",
        confidence=confidence,
        payload=payload or {},
        source="test_source",
    )


def _boundary_node(
    id_: str,
    condition: str | None = None,
    severity: str = "high",
    effect: str = "block",
    action: dict[str, Any] | None = None,
) -> DecisionNode:
    return DecisionNode(
        id=id_,
        name=id_,
        node_type=NodeType.BOUNDARY,
        condition=condition,
        severity=severity,
        effect=effect,
        action=action,
        config={"contract_type": "test_boundary", "contract_version": "1.0.0"},
    )


def _decision_result(
    status: DecisionStatus = DecisionStatus.CONFIRMED,
    action: dict[str, Any] | None = None,
    selected_node_id: str = "some_decision_node",
) -> DecisionResult:
    return DecisionResult(
        trace_id=uuid4(),
        flow_id=uuid4(),
        flow_version="1.0.0",
        selected_node_id=selected_node_id,
        source_signal_id=uuid4(),
        state=RuntimeState.CONFIRMED,
        status=status,
        outcome=DecisionOutcome.PASS,
        action=action or {"type": "route", "target": "queue"},
    )


def _flow(nodes: list[DecisionNode]) -> DecisionFlow:
    return DecisionFlow(
        flow_id="test_flow",
        name="Test Flow",
        version="1.0.0",
        entry_node_id=nodes[0].id,
        nodes=nodes,
        edges=[],
    )


# ------------------------------------------------------------------ #
# Tests                                                                #
# ------------------------------------------------------------------ #


def test_no_boundary_triggered() -> None:
    """When no boundary condition fires, DecisionResult is unchanged."""
    signal = _signal(payload={"amount": 50})
    nodes = [_boundary_node("b1", condition="payload.amount > 10000", effect="block")]
    flow = _flow(nodes)
    result = _decision_result()

    updated, boundary_results = BoundaryEngine().apply(signal, flow, result)

    assert updated.status == DecisionStatus.CONFIRMED
    assert updated.action == result.action
    assert updated.selected_node_id == result.selected_node_id
    assert len(boundary_results) == 1
    assert boundary_results[0].triggered is False
    assert updated.boundary_results == boundary_results


def test_boundary_allow_no_change() -> None:
    """A triggered boundary with effect=allow leaves the DecisionResult unchanged."""
    signal = _signal()
    nodes = [_boundary_node("b1", condition=None, severity="low", effect="allow")]
    flow = _flow(nodes)
    result = _decision_result()

    updated, boundary_results = BoundaryEngine().apply(signal, flow, result)

    assert updated.status == DecisionStatus.CONFIRMED
    assert updated.action == result.action
    assert boundary_results[0].triggered is True
    assert boundary_results[0].effect == "allow"


def test_boundary_block() -> None:
    """A triggered block boundary sets status=blocked and clears action."""
    signal = _signal()
    nodes = [_boundary_node("b1", condition=None, severity="high", effect="block")]
    flow = _flow(nodes)
    result = _decision_result()

    updated, _ = BoundaryEngine().apply(signal, flow, result)

    assert updated.status == DecisionStatus.BLOCKED
    assert updated.action is None


def test_boundary_override() -> None:
    """A triggered override boundary replaces action and selected_node_id."""
    override_action: dict[str, Any] = {"type": "override_route", "target": "premium_queue"}
    signal = _signal()
    nodes = [
        _boundary_node(
            "b_override",
            condition=None,
            severity="medium",
            effect="override",
            action=override_action,
        )
    ]
    flow = _flow(nodes)
    result = _decision_result()

    updated, _ = BoundaryEngine().apply(signal, flow, result)

    assert updated.action == override_action
    assert updated.selected_node_id == "b_override"
    assert updated.status == DecisionStatus.CONFIRMED


def test_boundary_escalate_sets_pending_human() -> None:
    """A triggered escalate boundary sets status=pending_human and updates selected_node_id."""
    signal = _signal()
    nodes = [_boundary_node("b_escalate", condition=None, severity="critical", effect="escalate")]
    flow = _flow(nodes)
    result = _decision_result()

    updated, _ = BoundaryEngine().apply(signal, flow, result)

    assert updated.status == DecisionStatus.PENDING_HUMAN
    assert updated.selected_node_id == "b_escalate"


def test_boundary_redirect() -> None:
    """A triggered redirect boundary replaces action and selected_node_id."""
    redirect_action: dict[str, Any] = {
        "type": "redirect",
        "target": "alt_queue",
        "parameters": {"reason": "capacity"},
    }
    signal = _signal()
    nodes = [
        _boundary_node(
            "b_redirect",
            condition=None,
            severity="medium",
            effect="redirect",
            action=redirect_action,
        )
    ]
    flow = _flow(nodes)
    result = _decision_result()

    updated, _ = BoundaryEngine().apply(signal, flow, result)

    assert updated.action == redirect_action
    assert updated.selected_node_id == "b_redirect"


def test_multiple_boundaries_highest_severity_wins() -> None:
    """When multiple boundaries trigger, the highest-severity effect is applied."""
    signal = _signal()
    redirect_action: dict[str, Any] = {"type": "redir", "target": "alt"}
    nodes = [
        _boundary_node("b_low", condition=None, severity="low", effect="allow"),
        _boundary_node("b_high", condition=None, severity="high", effect="block"),
        _boundary_node("b_med", condition=None, severity="medium", effect="redirect", action=redirect_action),
        _boundary_node("b_crit", condition=None, severity="critical", effect="escalate"),
    ]
    flow = _flow(nodes)
    result = _decision_result()

    updated, boundary_results = BoundaryEngine().apply(signal, flow, result)

    # critical is highest — escalate wins
    assert updated.status == DecisionStatus.PENDING_HUMAN
    assert updated.selected_node_id == "b_crit"
    assert len(boundary_results) == 4
    assert all(r.triggered for r in boundary_results)


def test_boundary_condition_false_not_triggered() -> None:
    """A boundary whose condition evaluates to False is not triggered."""
    signal = _signal(payload={"score": 5})
    nodes = [_boundary_node("b1", condition="payload.score > 100", effect="block")]
    flow = _flow(nodes)
    result = _decision_result()

    updated, boundary_results = BoundaryEngine().apply(signal, flow, result)

    assert boundary_results[0].triggered is False
    assert updated.status == DecisionStatus.CONFIRMED
    assert updated.action == result.action


def test_boundary_deterministic() -> None:
    """apply() is deterministic: identical inputs always yield identical results."""
    signal = _signal(payload={"value": 42})
    nodes = [_boundary_node("b1", condition="payload.value > 10", effect="block")]
    flow = _flow(nodes)
    result = _decision_result()
    engine = BoundaryEngine()

    r1, br1 = engine.apply(signal, flow, result)
    r2, br2 = engine.apply(signal, flow, result)

    assert r1.status == r2.status
    assert r1.selected_node_id == r2.selected_node_id
    assert r1.action == r2.action
    assert len(br1) == len(br2)
    assert br1[0].triggered == br2[0].triggered
    assert br1[0].severity == br2[0].severity
    assert br1[0].effect == br2[0].effect


# ------------------------------------------------------------------ #
# RuntimeBoundaryResult tests (Decision Runtime OS v2 canonical form) #
# ------------------------------------------------------------------ #


def test_runtime_boundary_result_from_triggered() -> None:
    """RuntimeBoundaryResult.from_boundary_result maps triggered→not passed."""
    br = BoundaryResult(
        boundary_id="b1",
        triggered=True,
        severity="high",
        effect="block",
        reason="Value exceeded limit",
    )
    rbr = RuntimeBoundaryResult.from_boundary_result(br)

    assert rbr.boundary_id == "b1"
    assert rbr.passed is False
    assert rbr.action == "block"
    assert rbr.severity == "high"
    assert rbr.reason == "Value exceeded limit"
    assert rbr.payload == {}


def test_runtime_boundary_result_from_not_triggered() -> None:
    """RuntimeBoundaryResult.from_boundary_result maps not-triggered→passed=True."""
    br = BoundaryResult(
        boundary_id="b_safe",
        triggered=False,
        severity="low",
        effect="allow",
        reason="Condition not met",
    )
    rbr = RuntimeBoundaryResult.from_boundary_result(br)

    assert rbr.passed is True
    assert rbr.action == "allow"


def test_runtime_boundary_result_payload_from_action() -> None:
    """RuntimeBoundaryResult.payload is populated from BoundaryResult.action dict."""
    action_payload = {"type": "redirect", "target": "alt_queue"}
    br = BoundaryResult(
        boundary_id="b_redir",
        triggered=True,
        severity="medium",
        effect="redirect",
        action=action_payload,
        reason="Redirecting due to capacity",
    )
    rbr = RuntimeBoundaryResult.from_boundary_result(br)

    assert rbr.payload == action_payload
    assert rbr.action == "redirect"


def test_to_runtime_boundary_results_converts_list() -> None:
    """to_runtime_boundary_results converts a full list of BoundaryResult."""
    signal = _signal()
    nodes = [
        _boundary_node("b_low", condition=None, severity="low", effect="allow"),
        _boundary_node("b_high", condition=None, severity="high", effect="block"),
    ]
    flow = _flow(nodes)
    result = _decision_result()

    _, boundary_results = BoundaryEngine().apply(signal, flow, result)
    runtime_results = to_runtime_boundary_results(boundary_results)

    assert len(runtime_results) == 2
    assert all(isinstance(r, RuntimeBoundaryResult) for r in runtime_results)
    # both triggered (condition=None → always triggers)
    assert all(r.passed is False for r in runtime_results)
    actions = {r.action for r in runtime_results}
    assert actions == {"allow", "block"}


def test_to_runtime_boundary_results_empty_list() -> None:
    """to_runtime_boundary_results handles an empty list."""
    assert to_runtime_boundary_results([]) == []
