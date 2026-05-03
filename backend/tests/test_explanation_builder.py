from __future__ import annotations

from uuid import uuid4

from backend.app.models.boundary import BoundaryResult
from backend.app.models.decision import DecisionOutcome, DecisionResult, DecisionStatus
from backend.app.models.flow import DecisionFlow, DecisionNode, NodeType
from backend.app.models.human_gate import HumanGateOption, HumanGateRequest
from backend.app.models.runtime import RuntimeState
from backend.app.models.signal import Signal, SignalValueType
from backend.app.models.trace import DecisionTrace
from backend.app.runtime.explanation_builder import ExplanationBuilder
from backend.app.runtime.trace_builder import TraceBuilder


# ------------------------------------------------------------------ #
# Test-object factories                                                #
# ------------------------------------------------------------------ #


def _signal() -> Signal:
    return Signal(
        name="sig",
        value_type=SignalValueType.JSON,
        type="event",
        source="src",
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
                id="fb", name="fb", node_type=NodeType.FALLBACK,
                config={"contract_type": "t", "contract_version": "1.0.0"},
            ),
        ],
        edges=[],
    )


def _result(
    status: DecisionStatus = DecisionStatus.CONFIRMED,
    action: dict | None = None,
    selected_node_id: str = "n1",
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


def _build_trace(
    result: DecisionResult,
    evaluated_nodes: list[dict] | None = None,
) -> DecisionTrace:
    return TraceBuilder().create_trace(_signal(), _flow(), result, evaluated_nodes or [])


# ------------------------------------------------------------------ #
# Tests                                                                #
# ------------------------------------------------------------------ #


def test_explain_confirmed_decision() -> None:
    """build() returns all expected keys for a confirmed decision."""
    result = _result()
    trace = _build_trace(result)

    explanation = ExplanationBuilder().build(trace)

    assert explanation["final_status"] == "confirmed"
    assert explanation["selected_node"] == result.selected_node_id
    assert explanation["decision_id"] == str(result.id)
    assert explanation["trace_id"] == str(trace.id)
    assert explanation["final_action"] == result.action
    assert explanation["human_gate"] is None
    assert explanation["matched_conditions"] == []
    assert explanation["unmatched_conditions"] == []
    assert explanation["boundary_effects"] == []


def test_explain_pending_human_decision() -> None:
    """build() includes a human_gate section when the decision is pending_human."""
    gate = HumanGateRequest(
        flow_id=uuid4(),
        node_id="b_esc",
        title="Review required",
        question="Approve?",
        options=[
            HumanGateOption(value="approve", label="Approve"),
            HumanGateOption(value="reject", label="Reject"),
        ],
    )
    result = _result(status=DecisionStatus.PENDING_HUMAN)
    result = result.model_copy(update={"human_gate": gate})
    trace = _build_trace(result)

    explanation = ExplanationBuilder().build(trace)

    assert explanation["final_status"] == "pending_human"
    assert explanation["human_gate"] is not None
    assert explanation["human_gate"]["required"] is True
    assert explanation["human_gate"]["status"] == "pending"
    assert explanation["human_gate"]["request_id"] == str(gate.id)


def test_explain_includes_matched_and_unmatched_conditions() -> None:
    """build() correctly splits evaluated_nodes into matched and unmatched lists."""
    evaluated_nodes = [
        {
            "node_id": "n1",
            "node_type": "decision",
            "matched": True,
            "condition": "confidence > 0.5",
            "reason": "condition matched",
        },
        {
            "node_id": "n2",
            "node_type": "decision",
            "matched": False,
            "condition": "payload.amount > 1000",
            "reason": "condition not matched",
        },
    ]
    result = _result()
    trace = _build_trace(result, evaluated_nodes)

    explanation = ExplanationBuilder().build(trace)

    assert len(explanation["matched_conditions"]) == 1
    assert explanation["matched_conditions"][0]["node_id"] == "n1"
    assert explanation["matched_conditions"][0]["condition"] == "confidence > 0.5"

    assert len(explanation["unmatched_conditions"]) == 1
    assert explanation["unmatched_conditions"][0]["node_id"] == "n2"
    assert explanation["unmatched_conditions"][0]["condition"] == "payload.amount > 1000"
