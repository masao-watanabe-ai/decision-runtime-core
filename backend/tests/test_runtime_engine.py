from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from backend.app.models.decision import DecisionOutcome, DecisionStatus
from backend.app.models.flow import DecisionFlow, DecisionNode, NodeType
from backend.app.models.signal import Signal, SignalValueType
from backend.app.runtime.engine import DecisionRuntimeEngine


# ------------------------------------------------------------------ #
# Test-object factories                                                #
# ------------------------------------------------------------------ #


def _signal(
    type_: str = "test_event",
    payload: dict[str, Any] | None = None,
    confidence: float = 1.0,
) -> Signal:
    return Signal(
        name="test_signal",
        value_type=SignalValueType.JSON,
        type=type_,
        confidence=confidence,
        payload=payload or {},
        source="test_source",
    )


def _decision_node(
    id_: str,
    condition: str | None = None,
    priority: int = 0,
    action: dict[str, Any] | None = None,
) -> DecisionNode:
    config: dict[str, Any] = {
        "contract_type": "test_contract",
        "contract_version": "1.0.0",
    }
    return DecisionNode(
        id=id_, name=id_, node_type=NodeType.DECISION,
        condition=condition, priority=priority, action=action, config=config,
    )


def _fallback_node(id_: str = "fallback") -> DecisionNode:
    return DecisionNode(
        id=id_,
        name=id_,
        node_type=NodeType.FALLBACK,
        config={"contract_type": "fallback_contract", "contract_version": "1.0.0"},
    )


def _flow(
    nodes: list[DecisionNode],
    strategy: str = "first_match",
    entry_node_id: str | None = None,
) -> DecisionFlow:
    entry = entry_node_id or nodes[0].id
    return DecisionFlow(
        flow_id="test_flow",
        name="Test Flow",
        version="1.0.0",
        entry_node_id=entry,
        nodes=nodes,
        edges=[],
        metadata={"resolution_policy": {"strategy": strategy}},
    )


# ------------------------------------------------------------------ #
# Tests                                                                #
# ------------------------------------------------------------------ #


def test_decision_match_priority() -> None:
    """A matching decision node is selected when using the priority strategy."""
    signal = _signal(type_="vip_request", payload={"tier": "vip"})
    nodes = [
        _decision_node("node_vip", condition='type == "vip_request"', priority=8),
        _fallback_node(),
    ]
    result = DecisionRuntimeEngine().evaluate(signal, _flow(nodes, strategy="priority"))

    assert result.selected_node_id == "node_vip"
    assert result.status == DecisionStatus.CONFIRMED
    assert result.outcome == DecisionOutcome.PASS


def test_decision_match_first_match() -> None:
    """The first matching node in flow.nodes order is selected with first_match strategy."""
    signal = _signal(confidence=0.9)
    nodes = [
        _decision_node("first", condition="confidence > 0.5", priority=0),
        _decision_node("second", condition="confidence > 0.5", priority=0),
        _fallback_node(),
    ]
    result = DecisionRuntimeEngine().evaluate(signal, _flow(nodes, strategy="first_match"))

    assert result.selected_node_id == "first"
    assert result.status == DecisionStatus.CONFIRMED


def test_fallback_used_when_no_match() -> None:
    """Fallback node is selected when no decision node condition matches."""
    signal = _signal(type_="unknown_event")
    nodes = [
        _decision_node("vip_node", condition='type == "vip_request"'),
        _fallback_node(),
    ]
    result = DecisionRuntimeEngine().evaluate(signal, _flow(nodes))

    assert result.selected_node_id == "fallback"
    assert result.status == DecisionStatus.FALLBACK
    assert result.outcome == DecisionOutcome.FAIL


def test_multiple_candidates_priority_selects_highest() -> None:
    """With priority strategy, the node with the highest priority value is selected."""
    signal = _signal()  # None condition → all nodes match
    nodes = [
        _decision_node("low", priority=1),   # condition=None → always matches
        _decision_node("high", priority=10),
        _decision_node("mid", priority=5),
        _fallback_node(),
    ]
    result = DecisionRuntimeEngine().evaluate(signal, _flow(nodes, strategy="priority"))

    assert result.selected_node_id == "high"
    assert result.status == DecisionStatus.CONFIRMED


def test_condition_false_not_selected() -> None:
    """A node whose condition evaluates to False is not selected."""
    signal = _signal(payload={"amount": 50})
    nodes = [
        _decision_node("expensive", condition="payload.amount > 10000"),
        _fallback_node(),
    ]
    result = DecisionRuntimeEngine().evaluate(signal, _flow(nodes))

    assert result.selected_node_id == "fallback"
    assert result.status == DecisionStatus.FALLBACK
    assert result.conditions_evaluated == 1
    assert result.conditions_passed == 0


def test_missing_fallback_raises() -> None:
    """RuntimeError is raised when no active fallback node is present and no node matched."""
    signal = _signal(type_="unmatched")
    # Build a flow with no fallback node — bypasses FlowValidator intentionally
    nodes = [_decision_node("node_a", condition='type == "other"')]
    no_fallback_flow = DecisionFlow(
        flow_id="no_fallback",
        name="No Fallback Flow",
        version="1.0.0",
        entry_node_id="node_a",
        nodes=nodes,
        edges=[],
    )

    with pytest.raises(RuntimeError, match="no active fallback node"):
        DecisionRuntimeEngine().evaluate(signal, no_fallback_flow)


def test_deterministic_same_input_same_output() -> None:
    """evaluate() is deterministic: identical inputs always yield identical routing decisions."""
    signal = _signal(payload={"score": 42})
    nodes = [
        _decision_node("scorer", condition="payload.score > 10"),
        _fallback_node(),
    ]
    flow = _flow(nodes)
    engine = DecisionRuntimeEngine()

    r1 = engine.evaluate(signal, flow)
    r2 = engine.evaluate(signal, flow)

    assert r1.selected_node_id == r2.selected_node_id
    assert r1.status == r2.status
    assert r1.outcome == r2.outcome
    assert r1.flow_id == r2.flow_id
    assert r1.flow_version == r2.flow_version
    assert r1.source_signal_id == r2.source_signal_id
    assert r1.conditions_evaluated == r2.conditions_evaluated
    assert r1.conditions_passed == r2.conditions_passed


def test_no_side_effects_on_signal() -> None:
    """The engine must not mutate the input Signal."""
    signal = _signal(payload={"original_key": "original_value"})
    original_id = signal.id
    original_payload = dict(signal.payload)
    original_name = signal.name

    nodes = [
        _decision_node("checker", condition='payload.original_key == "original_value"'),
        _fallback_node(),
    ]
    DecisionRuntimeEngine().evaluate(signal, _flow(nodes))

    assert signal.id == original_id
    assert signal.name == original_name
    assert signal.payload == original_payload


def test_action_propagation() -> None:
    """The action dict from the selected node's config is propagated to the result."""
    expected_action: dict[str, Any] = {
        "type": "notify",
        "target": "support_queue",
        "parameters": {"priority": "high"},
    }
    signal = _signal(type_="complaint")
    nodes = [
        _decision_node("complaint_handler", condition='type == "complaint"', action=expected_action),
        _fallback_node(),
    ]
    result = DecisionRuntimeEngine().evaluate(signal, _flow(nodes))

    assert result.selected_node_id == "complaint_handler"
    assert result.action == expected_action


def test_empty_action_allowed() -> None:
    """A decision node with no action config produces result.action == None."""
    signal = _signal(type_="simple")
    nodes = [
        _decision_node("no_action_node", condition='type == "simple"'),  # no action key
        _fallback_node(),
    ]
    result = DecisionRuntimeEngine().evaluate(signal, _flow(nodes))

    assert result.selected_node_id == "no_action_node"
    assert result.action is None
    assert result.status == DecisionStatus.CONFIRMED
