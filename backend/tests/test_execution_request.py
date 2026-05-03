from __future__ import annotations

from typing import Any

from backend.app.integrations.event_bus import EventBus
from backend.app.models.decision import DecisionStatus
from backend.app.models.event import EventType
from backend.app.models.flow import DecisionFlow, DecisionNode, NodeType
from backend.app.models.signal import Signal, SignalValueType
from backend.app.runtime.engine import DecisionRuntimeEngine


# ------------------------------------------------------------------ #
# Test-object factories                                                #
# ------------------------------------------------------------------ #


def _signal() -> Signal:
    return Signal(
        name="test_signal",
        value_type=SignalValueType.JSON,
        type="test_event",
        source="test_source",
    )


def _decision_node(id_: str, condition: str | None = None) -> DecisionNode:
    return DecisionNode(
        id=id_,
        name=id_,
        node_type=NodeType.DECISION,
        condition=condition,
        action={"type": "route", "target": "queue"},
        config={"contract_type": "t", "contract_version": "1.0.0"},
    )


def _fallback_node() -> DecisionNode:
    return DecisionNode(
        id="fallback",
        name="fallback",
        node_type=NodeType.FALLBACK,
        config={"contract_type": "t", "contract_version": "1.0.0"},
    )


def _boundary_node(id_: str, effect: str) -> DecisionNode:
    return DecisionNode(
        id=id_,
        name=id_,
        node_type=NodeType.BOUNDARY,
        condition=None,
        severity="high",
        effect=effect,
        config={"contract_type": "t", "contract_version": "1.0.0"},
    )


def _confirmed_flow() -> DecisionFlow:
    """Flow whose decision node always matches → status=confirmed."""
    return DecisionFlow(
        flow_id="confirmed_flow",
        name="Confirmed Flow",
        version="1.0.0",
        entry_node_id="always_match",
        nodes=[_decision_node("always_match"), _fallback_node()],
        edges=[],
    )


def _blocked_flow() -> DecisionFlow:
    """Flow with a decision node + blocking boundary → status=blocked."""
    return DecisionFlow(
        flow_id="blocked_flow",
        name="Blocked Flow",
        version="1.0.0",
        entry_node_id="always_match",
        nodes=[
            _decision_node("always_match"),
            _boundary_node("b_block", effect="block"),
            _fallback_node(),
        ],
        edges=[],
    )


def _escalate_flow() -> DecisionFlow:
    """Flow with a decision node + escalating boundary → status=pending_human."""
    return DecisionFlow(
        flow_id="escalate_flow",
        name="Escalate Flow",
        version="1.0.0",
        entry_node_id="always_match",
        nodes=[
            _decision_node("always_match"),
            _boundary_node("b_esc", effect="escalate"),
            _fallback_node(),
        ],
        edges=[],
    )


# ------------------------------------------------------------------ #
# Tests                                                                #
# ------------------------------------------------------------------ #


def test_execution_request_created_on_confirmed() -> None:
    """A confirmed decision triggers a runtime.execution.requested event."""
    bus = EventBus()
    engine = DecisionRuntimeEngine(event_bus=bus)

    result = engine.evaluate(_signal(), _confirmed_flow())

    assert result.status == DecisionStatus.CONFIRMED
    assert result.execution_id is not None

    events = bus.get_events()
    assert len(events) == 1
    assert events[0].event_type == EventType.EXECUTION_REQUESTED
    assert events[0].payload["execution_id"] == result.execution_id


def test_no_execution_request_when_pending_human() -> None:
    """A pending_human decision does NOT trigger an execution request."""
    bus = EventBus()
    engine = DecisionRuntimeEngine(event_bus=bus)

    result = engine.evaluate(_signal(), _escalate_flow())

    assert result.status == DecisionStatus.PENDING_HUMAN
    assert result.execution_id is None
    assert len(bus.get_events()) == 0


def test_no_execution_request_when_blocked() -> None:
    """A blocked decision does NOT trigger an execution request."""
    bus = EventBus()
    engine = DecisionRuntimeEngine(event_bus=bus)

    result = engine.evaluate(_signal(), _blocked_flow())

    assert result.status == DecisionStatus.BLOCKED
    assert result.execution_id is None
    assert len(bus.get_events()) == 0


def test_event_emitted_with_correct_payload() -> None:
    """The execution.requested event carries execution_id, decision_id, and action."""
    bus = EventBus()
    engine = DecisionRuntimeEngine(event_bus=bus)

    result = engine.evaluate(_signal(), _confirmed_flow())
    event = bus.get_events()[0]

    assert event.payload["execution_id"] == result.execution_id
    assert event.payload["decision_id"] == str(result.id)
    assert event.payload["action"] == result.action
    assert event.decision_id == result.id
    assert str(event.trace_id) == str(result.trace_id)


def test_execution_id_unique() -> None:
    """Each confirmed evaluation produces a distinct execution_id."""
    bus = EventBus()
    engine = DecisionRuntimeEngine(event_bus=bus)
    flow = _confirmed_flow()
    signal = _signal()

    r1 = engine.evaluate(signal, flow)
    r2 = engine.evaluate(signal, flow)

    assert r1.execution_id != r2.execution_id
    events = bus.get_events()
    assert len(events) == 2
    assert events[0].payload["execution_id"] != events[1].payload["execution_id"]
