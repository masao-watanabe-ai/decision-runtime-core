from __future__ import annotations

from uuid import uuid4

import pytest

from backend.app.models.boundary import BoundaryResult
from backend.app.models.decision import DecisionOutcome, DecisionResult, DecisionStatus
from backend.app.models.flow import DecisionFlow, DecisionNode, NodeType
from backend.app.models.human_gate import HumanGateOption, HumanGateRequest
from backend.app.models.runtime import RuntimeState
from backend.app.models.signal import Signal, SignalValueType
from backend.app.runtime.trace_builder import TraceBuilder
from backend.app.runtime.trace_store import TraceNotFoundError, TraceStore


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


def _flow() -> DecisionFlow:
    return DecisionFlow(
        flow_id="test_flow",
        name="Test Flow",
        version="1.0.0",
        entry_node_id="n1",
        nodes=[
            DecisionNode(
                id="n1", name="n1", node_type=NodeType.DECISION,
                config={"contract_type": "t", "contract_version": "1.0.0"},
            ),
            DecisionNode(
                id="fallback", name="fallback", node_type=NodeType.FALLBACK,
                config={"contract_type": "t", "contract_version": "1.0.0"},
            ),
        ],
        edges=[],
    )


def _result(
    status: DecisionStatus = DecisionStatus.CONFIRMED,
    action: dict | None = None,
) -> DecisionResult:
    return DecisionResult(
        trace_id=uuid4(),
        flow_id=uuid4(),
        flow_version="1.0.0",
        selected_node_id="n1",
        source_signal_id=uuid4(),
        state=RuntimeState.CONFIRMED,
        status=status,
        outcome=DecisionOutcome.PASS if status == DecisionStatus.CONFIRMED else DecisionOutcome.FAIL,
        action=action,
    )


# ------------------------------------------------------------------ #
# Tests                                                                #
# ------------------------------------------------------------------ #


def test_trace_created_for_confirmed_decision() -> None:
    """create_trace() returns a correctly linked DecisionTrace for a confirmed result."""
    signal = _signal()
    flow = _flow()
    result = _result()
    evaluated_nodes = [
        {
            "node_id": "n1",
            "node_type": "decision",
            "matched": True,
            "condition": "confidence > 0.5",
            "reason": "condition matched",
        }
    ]

    trace = TraceBuilder().create_trace(signal, flow, result, evaluated_nodes)

    assert str(trace.id) == str(result.trace_id)
    assert trace.flow_id == flow.id
    assert trace.flow_version == flow.version
    assert trace.state == RuntimeState.COMPLETED
    assert trace.decision_id == result.id
    assert trace.signal_id == signal.id
    assert len(trace.decision_results) == 1
    assert trace.decision_results[0].id == result.id
    assert trace.evaluated_nodes == evaluated_nodes
    assert len(trace.signals) == 1
    assert trace.signals[0].id == signal.id
    assert trace.completed_at is not None


def test_trace_created_for_fallback_decision() -> None:
    """create_trace() correctly records a fallback decision."""
    signal = _signal()
    flow = _flow()
    result = _result(status=DecisionStatus.FALLBACK)
    evaluated_nodes = [
        {"node_id": "n1", "node_type": "decision", "matched": False, "condition": "confidence > 0.9", "reason": "condition not matched"},
        {"node_id": "fallback", "node_type": "fallback", "matched": True, "condition": "", "reason": "no decision node matched; fallback selected"},
    ]

    trace = TraceBuilder().create_trace(signal, flow, result, evaluated_nodes)

    assert trace.decision_results[0].status == DecisionStatus.FALLBACK
    assert len(trace.evaluated_nodes) == 2
    assert trace.evaluated_nodes[1]["node_type"] == "fallback"


def test_trace_includes_boundary_results() -> None:
    """create_trace() copies boundary_results from the DecisionResult into the trace."""
    signal = _signal()
    flow = _flow()
    br = BoundaryResult(
        boundary_id="b1",
        triggered=True,
        severity="high",
        effect="block",
        action=None,
        reason="Boundary 'b1' triggered with effect 'block'",
    )
    result = _result()
    result = result.model_copy(update={"boundary_results": [br]})

    trace = TraceBuilder().create_trace(signal, flow, result, [])

    assert len(trace.boundary_results) == 1
    assert trace.boundary_results[0].boundary_id == "b1"
    assert trace.boundary_results[0].triggered is True


def test_trace_includes_human_gate() -> None:
    """create_trace() captures the HumanGateRequest from the DecisionResult."""
    signal = _signal()
    flow = _flow()
    gate = HumanGateRequest(
        flow_id=uuid4(),
        node_id="b_escalate",
        title="Review required",
        question="Approve or reject?",
        options=[
            HumanGateOption(value="approve", label="Approve"),
            HumanGateOption(value="reject", label="Reject"),
        ],
    )
    result = _result(status=DecisionStatus.PENDING_HUMAN)
    result = result.model_copy(update={"human_gate": gate})

    trace = TraceBuilder().create_trace(signal, flow, result, [])

    assert len(trace.human_gate_requests) == 1
    assert trace.human_gate_requests[0].id == gate.id


def test_trace_store_get_by_trace_id() -> None:
    """TraceStore.get() returns the trace matching the trace_id."""
    store = TraceStore()
    signal = _signal()
    flow = _flow()
    result = _result()
    trace = TraceBuilder().create_trace(signal, flow, result, [])

    store.save(trace)
    retrieved = store.get(str(trace.id))

    assert retrieved.id == trace.id


def test_trace_store_get_by_decision_id() -> None:
    """TraceStore.get_by_decision_id() returns the trace linked to the DecisionResult."""
    store = TraceStore()
    signal = _signal()
    flow = _flow()
    result = _result()
    trace = TraceBuilder().create_trace(signal, flow, result, [])

    store.save(trace)
    retrieved = store.get_by_decision_id(str(result.id))

    assert retrieved.id == trace.id


def test_trace_not_found_raises() -> None:
    """TraceStore.get() and get_by_decision_id() raise TraceNotFoundError for unknown IDs."""
    store = TraceStore()

    with pytest.raises(TraceNotFoundError):
        store.get("no-such-trace")

    with pytest.raises(TraceNotFoundError):
        store.get_by_decision_id("no-such-decision")
