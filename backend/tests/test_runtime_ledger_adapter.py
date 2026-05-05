"""
Tests for RuntimeLedgerAdapter and its integration with DecisionRuntimeEngine.

Coverage:
  - ledger disabled (no adapter injected) → existing behaviour unchanged
  - ledger enabled → LedgerEvents are appended
  - node type → StepType mapping (decision / boundary / fallback / human / action)
  - duplicate commit → DUPLICATE status, no new events written
  - parallel mode → ledger exception does not propagate to caller
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from backend.app.integrations.ledger_client import LedgerAppendResult, LedgerAppendStatus, LedgerClient
from backend.app.integrations.runtime_ledger_adapter import (
    LedgerCommitStatus,
    RuntimeLedgerAdapter,
    StepType,
    _event_id,
    decision_result_to_ledger_event,
)
from backend.app.models.decision import DecisionOutcome, DecisionResult, DecisionStatus
from backend.app.models.flow import DecisionFlow, DecisionNode, NodeType
from backend.app.models.human_gate import HumanGateOption, HumanGateRequest, HumanGateStatus
from backend.app.models.runtime import RuntimeState
from backend.app.models.signal import Signal, SignalValueType
from backend.app.models.trace import DecisionTrace
from backend.app.runtime.engine import DecisionRuntimeEngine
from backend.app.runtime.trace_store import TraceStore


# ------------------------------------------------------------------ #
# Factories                                                            #
# ------------------------------------------------------------------ #


def _signal(type_: str = "test_event", confidence: float = 1.0) -> Signal:
    return Signal(
        name="test_signal",
        value_type=SignalValueType.JSON,
        type=type_,
        confidence=confidence,
        payload={},
        source="test_source",
    )


def _decision_node(id_: str, condition: str | None = None) -> DecisionNode:
    return DecisionNode(
        id=id_, name=id_, node_type=NodeType.DECISION,
        condition=condition,
        config={"contract_type": "test", "contract_version": "1.0.0"},
    )


def _boundary_node(id_: str) -> DecisionNode:
    return DecisionNode(
        id=id_, name=id_, node_type=NodeType.BOUNDARY,
        effect="block", severity="high",
        condition="confidence < 0.1",
        config={},
    )


def _fallback_node(id_: str = "fallback") -> DecisionNode:
    return DecisionNode(
        id=id_, name=id_, node_type=NodeType.FALLBACK,
        config={"contract_type": "fallback", "contract_version": "1.0.0"},
    )


def _flow(nodes: list[DecisionNode], strategy: str = "first_match") -> DecisionFlow:
    return DecisionFlow(
        flow_id="test_flow",
        name="Test Flow",
        version="1.0.0",
        entry_node_id=nodes[0].id,
        nodes=nodes,
        edges=[],
        metadata={"resolution_policy": {"strategy": strategy}},
    )


def _minimal_result(trace_id=None, flow_id=None) -> DecisionResult:
    now = datetime.now(timezone.utc)
    return DecisionResult(
        trace_id=trace_id or uuid4(),
        flow_id=flow_id or uuid4(),
        flow_version="1.0.0",
        selected_node_id="node_a",
        source_signal_id=uuid4(),
        state=RuntimeState.CONFIRMED,
        status=DecisionStatus.CONFIRMED,
        outcome=DecisionOutcome.PASS,
        confidence=0.9,
        evaluated_at=now,
        created_at=now,
        updated_at=now,
    )


def _minimal_trace(
    decision_result: DecisionResult,
    evaluated_nodes: list[dict[str, Any]] | None = None,
    human_gate_requests: list[HumanGateRequest] | None = None,
) -> DecisionTrace:
    return DecisionTrace(
        id=decision_result.trace_id,
        flow_id=decision_result.flow_id,
        flow_version=decision_result.flow_version,
        state=RuntimeState.COMPLETED,
        decision_id=decision_result.id,
        signal_id=decision_result.source_signal_id,
        evaluated_nodes=evaluated_nodes or [],
        decision_results=[decision_result],
        human_gate_requests=human_gate_requests or [],
        started_at=decision_result.created_at,
        completed_at=decision_result.evaluated_at,
    )


# ------------------------------------------------------------------ #
# Adapter unit tests                                                   #
# ------------------------------------------------------------------ #


def test_commit_returns_accepted_for_valid_trace() -> None:
    """commit() on a valid trace returns ACCEPTED and appended > 0."""
    result_obj = _minimal_result()
    trace = _minimal_trace(
        result_obj,
        evaluated_nodes=[
            {"node_id": "node_a", "node_type": "decision", "matched": True, "condition": None, "reason": "matched"},
        ],
    )
    adapter = RuntimeLedgerAdapter()
    commit = adapter.commit(trace, result_obj)

    assert commit.status == LedgerCommitStatus.ACCEPTED
    assert commit.appended > 0
    assert commit.duplicates == 0
    assert commit.errors == []


def test_signal_event_is_included() -> None:
    """A SIGNAL-typed event is emitted when trace.signal_id is set."""
    result_obj = _minimal_result()
    trace = _minimal_trace(result_obj)

    client = LedgerClient()
    adapter = RuntimeLedgerAdapter(ledger_client=client)
    adapter.commit(trace, result_obj)

    signal_events = [e for e in client.get_events() if e.step_type == StepType.SIGNAL]
    assert len(signal_events) == 1
    assert signal_events[0].step_id == str(trace.signal_id)


def test_decision_node_maps_to_decision_step() -> None:
    """evaluated_nodes with node_type='decision' produce StepType.DECISION events."""
    result_obj = _minimal_result()
    trace = _minimal_trace(
        result_obj,
        evaluated_nodes=[
            {"node_id": "dec_1", "node_type": "decision", "matched": True, "condition": "x > 0", "reason": "matched"},
        ],
    )
    client = LedgerClient()
    adapter = RuntimeLedgerAdapter(ledger_client=client)
    adapter.commit(trace, result_obj)

    decision_events = [e for e in client.get_events() if e.step_type == StepType.DECISION]
    assert len(decision_events) == 1
    assert decision_events[0].step_id == "dec_1"
    assert decision_events[0].payload["matched"] is True


def test_boundary_node_maps_to_boundary_step() -> None:
    """evaluated_nodes with node_type='boundary' produce StepType.BOUNDARY events."""
    result_obj = _minimal_result()
    trace = _minimal_trace(
        result_obj,
        evaluated_nodes=[
            {"node_id": "bound_1", "node_type": "boundary", "matched": True, "condition": "amount > 1000", "reason": "blocked"},
        ],
    )
    client = LedgerClient()
    adapter = RuntimeLedgerAdapter(ledger_client=client)
    adapter.commit(trace, result_obj)

    boundary_events = [e for e in client.get_events() if e.step_type == StepType.BOUNDARY]
    assert len(boundary_events) == 1
    assert boundary_events[0].step_id == "bound_1"


def test_fallback_node_maps_to_outcome_step() -> None:
    """evaluated_nodes with node_type='fallback' produce StepType.OUTCOME events."""
    result_obj = _minimal_result()
    trace = _minimal_trace(
        result_obj,
        evaluated_nodes=[
            {"node_id": "fallback", "node_type": "fallback", "matched": True, "condition": "", "reason": "no match"},
        ],
    )
    client = LedgerClient()
    adapter = RuntimeLedgerAdapter(ledger_client=client)
    adapter.commit(trace, result_obj)

    outcome_events = [
        e for e in client.get_events()
        if e.step_type == StepType.OUTCOME and e.step_id == "fallback"
    ]
    assert len(outcome_events) == 1


def test_human_gate_request_maps_to_human_step() -> None:
    """HumanGateRequest entries in trace.human_gate_requests produce StepType.HUMAN events."""
    result_obj = _minimal_result()
    gate = HumanGateRequest(
        flow_id=result_obj.flow_id,
        decision_id=result_obj.id,
        trace_id=result_obj.trace_id,
        node_id="gate_node",
        title="Review required",
        question="Approve or reject?",
        options=[HumanGateOption(value="approve", label="Approve", is_default=True)],
        status=HumanGateStatus.PENDING,
    )
    trace = _minimal_trace(result_obj, human_gate_requests=[gate])

    client = LedgerClient()
    adapter = RuntimeLedgerAdapter(ledger_client=client)
    adapter.commit(trace, result_obj)

    human_events = [e for e in client.get_events() if e.step_type == StepType.HUMAN]
    assert len(human_events) == 1
    assert human_events[0].step_id == str(gate.id)
    assert human_events[0].payload["node_id"] == "gate_node"


def test_outcome_event_always_present() -> None:
    """A final OUTCOME event capturing the DecisionResult is always appended."""
    result_obj = _minimal_result()
    trace = _minimal_trace(result_obj)

    client = LedgerClient()
    adapter = RuntimeLedgerAdapter(ledger_client=client)
    adapter.commit(trace, result_obj)

    outcome_events = [
        e for e in client.get_events()
        if e.step_type == StepType.OUTCOME and e.step_id == str(result_obj.id)
    ]
    assert len(outcome_events) == 1
    assert outcome_events[0].payload["status"] == result_obj.status.value
    assert outcome_events[0].payload["selected_node_id"] == result_obj.selected_node_id


def test_duplicate_commit_returns_duplicate_status() -> None:
    """Committing the same trace twice returns DUPLICATE on the second call."""
    result_obj = _minimal_result()
    trace = _minimal_trace(result_obj)

    client = LedgerClient()
    adapter = RuntimeLedgerAdapter(ledger_client=client)

    first = adapter.commit(trace, result_obj)
    second = adapter.commit(trace, result_obj)

    assert first.status == LedgerCommitStatus.ACCEPTED
    assert second.status == LedgerCommitStatus.DUPLICATE
    assert second.appended == 0
    assert second.duplicates > 0

    # Ledger must not contain duplicate records.
    all_ids = [e.event_id for e in client.get_events()]
    assert len(all_ids) == len(set(all_ids))


def test_event_ids_are_deterministic() -> None:
    """Two commits of the same trace produce identical event_id sets."""
    result_obj = _minimal_result()
    trace = _minimal_trace(
        result_obj,
        evaluated_nodes=[
            {"node_id": "n1", "node_type": "decision", "matched": True, "condition": None, "reason": "ok"},
        ],
    )
    adapter1 = RuntimeLedgerAdapter(ledger_client=LedgerClient())
    adapter2 = RuntimeLedgerAdapter(ledger_client=LedgerClient())

    r1 = adapter1.commit(trace, result_obj)
    r2 = adapter2.commit(trace, result_obj)

    assert r1.event_ids == r2.event_ids


def test_multiple_node_types_all_mapped() -> None:
    """decision, boundary, and fallback nodes each appear as the correct StepType."""
    result_obj = _minimal_result()
    trace = _minimal_trace(
        result_obj,
        evaluated_nodes=[
            {"node_id": "d1", "node_type": "decision", "matched": False, "condition": "x>0", "reason": "no match"},
            {"node_id": "b1", "node_type": "boundary", "matched": True, "condition": "y>5", "reason": "blocked"},
            {"node_id": "fb", "node_type": "fallback", "matched": True, "condition": "", "reason": "fallback used"},
        ],
    )
    client = LedgerClient()
    adapter = RuntimeLedgerAdapter(ledger_client=client)
    adapter.commit(trace, result_obj)

    events_by_step_id = {e.step_id: e.step_type for e in client.get_events()}
    assert events_by_step_id["d1"] == StepType.DECISION
    assert events_by_step_id["b1"] == StepType.BOUNDARY
    assert events_by_step_id["fb"] == StepType.OUTCOME


# ------------------------------------------------------------------ #
# Engine integration tests                                             #
# ------------------------------------------------------------------ #


def test_engine_without_adapter_unchanged() -> None:
    """Engine without a ledger_adapter produces an identical DecisionResult."""
    signal = _signal(type_="vip", confidence=0.9)
    nodes = [
        _decision_node("node_a", condition="confidence > 0.5"),
        _fallback_node(),
    ]
    engine = DecisionRuntimeEngine()
    result = engine.evaluate(signal, _flow(nodes))

    assert result.selected_node_id == "node_a"
    assert result.status == DecisionStatus.CONFIRMED


def test_engine_with_adapter_appends_events_on_evaluate() -> None:
    """Engine with adapter calls commit() and events appear in the ledger."""
    signal = _signal(type_="order", confidence=0.95)
    nodes = [
        _decision_node("approve", condition="confidence > 0.5"),
        _fallback_node(),
    ]
    client = LedgerClient()
    adapter = RuntimeLedgerAdapter(ledger_client=client)
    store = TraceStore()

    engine = DecisionRuntimeEngine(
        trace_store=store,
        ledger_adapter=adapter,
    )
    result = engine.evaluate(signal, _flow(nodes))

    assert result.status == DecisionStatus.CONFIRMED
    assert len(client.get_events()) > 0

    # Verify at least one DECISION-typed event was appended.
    step_types = {e.step_type for e in client.get_events()}
    assert StepType.DECISION in step_types
    assert StepType.OUTCOME in step_types


def test_engine_with_adapter_no_trace_store() -> None:
    """Adapter can operate even when trace_store is not injected."""
    signal = _signal(confidence=0.8)
    nodes = [
        _decision_node("node_a", condition="confidence > 0.5"),
        _fallback_node(),
    ]
    client = LedgerClient()
    adapter = RuntimeLedgerAdapter(ledger_client=client)

    engine = DecisionRuntimeEngine(ledger_adapter=adapter)
    result = engine.evaluate(signal, _flow(nodes))

    assert result.status == DecisionStatus.CONFIRMED
    assert len(client.get_events()) > 0


def test_engine_parallel_mode_ledger_exception_does_not_propagate() -> None:
    """In parallel mode, a ledger exception must not propagate to the caller."""
    signal = _signal(confidence=0.9)
    nodes = [_decision_node("n", condition="confidence > 0"), _fallback_node()]

    broken_client = LedgerClient()
    broken_client.append = MagicMock(side_effect=RuntimeError("ledger offline"))  # type: ignore[method-assign]
    adapter = RuntimeLedgerAdapter(ledger_client=broken_client)

    engine = DecisionRuntimeEngine(ledger_adapter=adapter, ledger_mode="parallel")
    result = engine.evaluate(signal, _flow(nodes))

    # Decision result returned successfully despite ledger failure.
    assert result.status == DecisionStatus.CONFIRMED


def test_engine_strict_mode_ledger_exception_propagates() -> None:
    """In strict mode, a ledger exception must propagate to the caller."""
    signal = _signal(confidence=0.9)
    nodes = [_decision_node("n", condition="confidence > 0"), _fallback_node()]

    broken_client = LedgerClient()
    broken_client.append = MagicMock(side_effect=RuntimeError("ledger offline"))  # type: ignore[method-assign]
    adapter = RuntimeLedgerAdapter(ledger_client=broken_client)

    engine = DecisionRuntimeEngine(ledger_adapter=adapter, ledger_mode="strict")

    with pytest.raises(RuntimeError, match="ledger offline"):
        engine.evaluate(signal, _flow(nodes))


def test_engine_evaluate_twice_second_commit_is_duplicate() -> None:
    """Two evaluate() calls with the same trace produce DUPLICATE on the second ledger commit."""
    signal = _signal(confidence=0.9)
    nodes = [_decision_node("n", condition="confidence > 0"), _fallback_node()]
    flow = _flow(nodes)

    client = LedgerClient()
    adapter = RuntimeLedgerAdapter(ledger_client=client)
    store = TraceStore()

    engine = DecisionRuntimeEngine(trace_store=store, ledger_adapter=adapter)
    engine.evaluate(signal, flow)

    events_after_first = len(client.get_events())
    assert events_after_first > 0

    engine.evaluate(signal, flow)

    # Second evaluate creates a new trace (different trace_id / decision_id),
    # so the ledger should have more events — not duplicates.
    assert len(client.get_events()) > events_after_first


# ------------------------------------------------------------------ #
# decision_result_to_ledger_event tests (canonical standalone fn)     #
# ------------------------------------------------------------------ #


def _decision_result(
    status: DecisionStatus = DecisionStatus.CONFIRMED,
    outcome: DecisionOutcome = DecisionOutcome.PASS,
) -> DecisionResult:
    from backend.app.models.runtime import RuntimeState
    return DecisionResult(
        trace_id=uuid4(),
        flow_id=uuid4(),
        flow_version="1.0.0",
        selected_node_id="some_node",
        source_signal_id=uuid4(),
        state=RuntimeState.CONFIRMED,
        status=status,
        outcome=outcome,
    )


def test_decision_result_to_ledger_event_required_keys() -> None:
    """decision_result_to_ledger_event returns all required canonical keys."""
    result = _decision_result(
        status=DecisionStatus.CONFIRMED,
        outcome=DecisionOutcome.PASS,
    )
    event = decision_result_to_ledger_event(result)

    required_keys = {
        "event_type", "schema_version", "decision_id", "signal_id",
        "decision_type", "selected_flow_id", "outcome", "status",
        "confidence", "reason", "human_gate_required", "payload",
        "timestamp", "boundary_results",
    }
    assert required_keys.issubset(event.keys())


def test_decision_result_to_ledger_event_type() -> None:
    """event_type is always 'runtime.decision.produced'."""
    result = _decision_result(status=DecisionStatus.CONFIRMED, outcome=DecisionOutcome.PASS)
    event = decision_result_to_ledger_event(result)
    assert event["event_type"] == "runtime.decision.produced"


def test_decision_result_to_ledger_event_canonical_ids() -> None:
    """decision_id matches DecisionResult.decision_id (canonical str form)."""
    result = _decision_result(status=DecisionStatus.CONFIRMED, outcome=DecisionOutcome.PASS)
    event = decision_result_to_ledger_event(result)
    assert event["decision_id"] == result.decision_id
    assert event["decision_id"] == str(result.id)


def test_decision_result_to_ledger_event_schema_version() -> None:
    """schema_version is 'decision-result/v1'."""
    result = _decision_result(status=DecisionStatus.CONFIRMED, outcome=DecisionOutcome.PASS)
    event = decision_result_to_ledger_event(result)
    assert event["schema_version"] == "decision-result/v1"


def test_decision_result_to_ledger_event_outcome_values() -> None:
    """outcome and status are string values, not enum objects."""
    result = _decision_result(status=DecisionStatus.FALLBACK, outcome=DecisionOutcome.FAIL)
    event = decision_result_to_ledger_event(result)
    assert event["outcome"] == "fail"
    assert event["status"] == "fallback"
    assert isinstance(event["outcome"], str)
    assert isinstance(event["status"], str)


def test_decision_result_to_ledger_event_boundary_results_serializable() -> None:
    """boundary_results in ledger event are plain dicts, not model objects."""
    from backend.app.models.boundary import BoundaryResult
    from datetime import datetime, timezone

    br = BoundaryResult(
        boundary_id="b1",
        triggered=True,
        severity="high",
        effect="block",
        reason="Value exceeded limit",
    )
    result = _decision_result(status=DecisionStatus.BLOCKED, outcome=DecisionOutcome.FAIL)
    result = result.model_copy(update={"boundary_results": [br]})
    event = decision_result_to_ledger_event(result)

    assert len(event["boundary_results"]) == 1
    br_dict = event["boundary_results"][0]
    assert isinstance(br_dict, dict)
    assert br_dict["boundary_id"] == "b1"
    assert br_dict["triggered"] is True
    assert br_dict["effect"] == "block"
    assert br_dict["severity"] == "high"
