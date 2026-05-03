"""
Tests for strict commit semantics in DecisionRuntimeEngine.

Contract under test:
    strict mode + ACCEPTED     → original DecisionResult returned, EventBus fires
    strict mode + non-ACCEPTED → DecisionStatus.ERROR returned, EventBus silent
    parallel mode              → EventBus fires regardless of ledger state

Design principle:
    Runtime evaluates.
    Ledger commits.
    Execution only follows committed decisions.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from backend.app.integrations.event_bus import EventBus
from backend.app.integrations.runtime_ledger_adapter import (
    LedgerCommitResult,
    LedgerCommitStatus,
    RuntimeLedgerAdapter,
)
from backend.app.models.decision import DecisionOutcome, DecisionStatus
from backend.app.models.event import EventType
from backend.app.models.flow import DecisionFlow, DecisionNode, NodeType
from backend.app.models.signal import Signal, SignalValueType
from backend.app.runtime.engine import DecisionRuntimeEngine
from backend.app.runtime.trace_store import TraceStore


# ------------------------------------------------------------------ #
# Factories                                                            #
# ------------------------------------------------------------------ #


def _signal(confidence: float = 0.9) -> Signal:
    return Signal(
        name="test_signal",
        value_type=SignalValueType.JSON,
        type="test_event",
        confidence=confidence,
        payload={},
        source="test_source",
    )


def _decision_node(id_: str, condition: str | None = "confidence > 0") -> DecisionNode:
    return DecisionNode(
        id=id_, name=id_, node_type=NodeType.DECISION,
        condition=condition,
        config={"contract_type": "test", "contract_version": "1.0.0"},
    )


def _fallback_node() -> DecisionNode:
    return DecisionNode(
        id="fallback", name="fallback", node_type=NodeType.FALLBACK,
        config={"contract_type": "fallback", "contract_version": "1.0.0"},
    )


def _flow(nodes: list[DecisionNode]) -> DecisionFlow:
    return DecisionFlow(
        flow_id="test_flow", name="Test Flow", version="1.0.0",
        entry_node_id=nodes[0].id, nodes=nodes, edges=[],
    )


def _confirmed_flow() -> tuple[Signal, DecisionFlow]:
    """Returns a signal + flow that always resolves to CONFIRMED."""
    signal = _signal()
    flow = _flow([_decision_node("approve"), _fallback_node()])
    return signal, flow


def _mock_adapter(commit_status: LedgerCommitStatus) -> MagicMock:
    """Return a mock RuntimeLedgerAdapter whose commit() returns the given status."""
    adapter = MagicMock(spec=RuntimeLedgerAdapter)
    accepted = commit_status == LedgerCommitStatus.ACCEPTED
    adapter.commit.return_value = LedgerCommitResult(
        status=commit_status,
        appended=3 if accepted else 0,
        duplicates=1 if commit_status == LedgerCommitStatus.DUPLICATE else 0,
        errors=["invalid"] if commit_status == LedgerCommitStatus.FAILED else [],
    )
    return adapter


# ------------------------------------------------------------------ #
# Strict mode — ACCEPTED path                                          #
# ------------------------------------------------------------------ #


def test_strict_accepted_returns_confirmed() -> None:
    """strict mode + ACCEPTED → decision result is unchanged (CONFIRMED)."""
    signal, flow = _confirmed_flow()
    adapter = _mock_adapter(LedgerCommitStatus.ACCEPTED)
    engine = DecisionRuntimeEngine(ledger_adapter=adapter, ledger_mode="strict")

    result = engine.evaluate(signal, flow)

    assert result.status == DecisionStatus.CONFIRMED
    assert result.outcome == DecisionOutcome.PASS
    assert result.error_message is None


def test_strict_accepted_eventbus_publishes() -> None:
    """strict mode + ACCEPTED → EXECUTION_REQUESTED is published to EventBus."""
    signal, flow = _confirmed_flow()
    event_bus = EventBus()
    adapter = _mock_adapter(LedgerCommitStatus.ACCEPTED)
    engine = DecisionRuntimeEngine(
        event_bus=event_bus, ledger_adapter=adapter, ledger_mode="strict"
    )

    result = engine.evaluate(signal, flow)

    assert result.status == DecisionStatus.CONFIRMED
    event_types = [e.event_type for e in event_bus.get_events()]
    assert EventType.EXECUTION_REQUESTED in event_types


def test_strict_accepted_execution_id_set() -> None:
    """strict mode + ACCEPTED → execution_id is populated on the returned result."""
    signal, flow = _confirmed_flow()
    event_bus = EventBus()
    adapter = _mock_adapter(LedgerCommitStatus.ACCEPTED)
    engine = DecisionRuntimeEngine(
        event_bus=event_bus, ledger_adapter=adapter, ledger_mode="strict"
    )

    result = engine.evaluate(signal, flow)

    assert result.execution_id is not None


def test_strict_accepted_trace_saved() -> None:
    """strict mode + ACCEPTED → DecisionTrace is persisted to TraceStore."""
    signal, flow = _confirmed_flow()
    store = TraceStore()
    adapter = _mock_adapter(LedgerCommitStatus.ACCEPTED)
    engine = DecisionRuntimeEngine(
        trace_store=store, ledger_adapter=adapter, ledger_mode="strict"
    )

    result = engine.evaluate(signal, flow)

    trace = store.get_by_decision_id(str(result.id))
    assert trace is not None
    assert str(trace.decision_id) == str(result.id)


# ------------------------------------------------------------------ #
# Strict mode — non-ACCEPTED paths                                     #
# ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "bad_status",
    [
        LedgerCommitStatus.DUPLICATE,
        LedgerCommitStatus.FAILED,
        LedgerCommitStatus.PARTIAL,
    ],
)
def test_strict_non_accepted_returns_error(bad_status: LedgerCommitStatus) -> None:
    """strict mode + non-ACCEPTED ledger status → DecisionStatus.ERROR."""
    signal, flow = _confirmed_flow()
    adapter = _mock_adapter(bad_status)
    engine = DecisionRuntimeEngine(ledger_adapter=adapter, ledger_mode="strict")

    result = engine.evaluate(signal, flow)

    assert result.status == DecisionStatus.ERROR
    assert result.error_message is not None
    assert bad_status.value in result.error_message


def test_strict_duplicate_returns_error() -> None:
    """strict mode + DUPLICATE → ERROR (explicit case for duplicate handling)."""
    signal, flow = _confirmed_flow()
    adapter = _mock_adapter(LedgerCommitStatus.DUPLICATE)
    engine = DecisionRuntimeEngine(ledger_adapter=adapter, ledger_mode="strict")

    result = engine.evaluate(signal, flow)

    assert result.status == DecisionStatus.ERROR
    assert "duplicate" in result.error_message


def test_strict_failed_returns_error() -> None:
    """strict mode + FAILED (INVALID events from ledger) → ERROR."""
    signal, flow = _confirmed_flow()
    adapter = _mock_adapter(LedgerCommitStatus.FAILED)
    engine = DecisionRuntimeEngine(ledger_adapter=adapter, ledger_mode="strict")

    result = engine.evaluate(signal, flow)

    assert result.status == DecisionStatus.ERROR
    assert "failed" in result.error_message


def test_strict_error_result_preserves_selected_node() -> None:
    """strict mode ERROR result still carries the selected_node_id for audit."""
    signal, flow = _confirmed_flow()
    adapter = _mock_adapter(LedgerCommitStatus.DUPLICATE)
    engine = DecisionRuntimeEngine(ledger_adapter=adapter, ledger_mode="strict")

    result = engine.evaluate(signal, flow)

    # The routing decision is preserved — only the ledger gate failed.
    assert result.selected_node_id == "approve"
    assert result.status == DecisionStatus.ERROR


# ------------------------------------------------------------------ #
# Strict mode — EventBus silence on failure                            #
# ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "bad_status",
    [
        LedgerCommitStatus.DUPLICATE,
        LedgerCommitStatus.FAILED,
        LedgerCommitStatus.PARTIAL,
    ],
)
def test_strict_failure_no_execution_event(bad_status: LedgerCommitStatus) -> None:
    """strict mode + non-ACCEPTED → EXECUTION_REQUESTED is NOT published to EventBus."""
    signal, flow = _confirmed_flow()
    event_bus = EventBus()
    adapter = _mock_adapter(bad_status)
    engine = DecisionRuntimeEngine(
        event_bus=event_bus, ledger_adapter=adapter, ledger_mode="strict"
    )

    engine.evaluate(signal, flow)

    event_types = [e.event_type for e in event_bus.get_events()]
    assert EventType.EXECUTION_REQUESTED not in event_types


def test_strict_failure_eventbus_completely_silent() -> None:
    """strict mode failure → EventBus receives no events at all."""
    signal, flow = _confirmed_flow()
    event_bus = EventBus()
    adapter = _mock_adapter(LedgerCommitStatus.DUPLICATE)
    engine = DecisionRuntimeEngine(
        event_bus=event_bus, ledger_adapter=adapter, ledger_mode="strict"
    )

    engine.evaluate(signal, flow)

    assert event_bus.get_events() == []


def test_strict_failure_no_execution_id() -> None:
    """strict mode ERROR result never carries an execution_id."""
    signal, flow = _confirmed_flow()
    adapter = _mock_adapter(LedgerCommitStatus.DUPLICATE)
    engine = DecisionRuntimeEngine(
        event_bus=EventBus(), ledger_adapter=adapter, ledger_mode="strict"
    )

    result = engine.evaluate(signal, flow)

    assert result.execution_id is None


# ------------------------------------------------------------------ #
# Parallel mode — EventBus unaffected by ledger                        #
# ------------------------------------------------------------------ #


def test_parallel_broken_ledger_eventbus_still_publishes() -> None:
    """parallel mode + broken ledger → EXECUTION_REQUESTED still published."""
    signal, flow = _confirmed_flow()
    event_bus = EventBus()
    broken = MagicMock(spec=RuntimeLedgerAdapter)
    broken.commit.side_effect = RuntimeError("ledger offline")
    engine = DecisionRuntimeEngine(
        event_bus=event_bus, ledger_adapter=broken, ledger_mode="parallel"
    )

    result = engine.evaluate(signal, flow)

    assert result.status == DecisionStatus.CONFIRMED
    event_types = [e.event_type for e in event_bus.get_events()]
    assert EventType.EXECUTION_REQUESTED in event_types


def test_parallel_any_non_accepted_status_still_returns_result() -> None:
    """parallel mode + non-ACCEPTED ledger → result unchanged (observational only)."""
    signal, flow = _confirmed_flow()
    for bad_status in (LedgerCommitStatus.DUPLICATE, LedgerCommitStatus.FAILED, LedgerCommitStatus.PARTIAL):
        adapter = _mock_adapter(bad_status)
        engine = DecisionRuntimeEngine(ledger_adapter=adapter, ledger_mode="parallel")
        result = engine.evaluate(signal, flow)
        assert result.status == DecisionStatus.CONFIRMED, (
            f"parallel mode with {bad_status} should not affect result status"
        )


# ------------------------------------------------------------------ #
# Mode comparison                                                       #
# ------------------------------------------------------------------ #


def test_same_signal_strict_vs_parallel_differ_on_ledger_failure() -> None:
    """The same signal + DUPLICATE ledger produces ERROR in strict but CONFIRMED in parallel."""
    signal, flow = _confirmed_flow()

    strict_engine = DecisionRuntimeEngine(
        ledger_adapter=_mock_adapter(LedgerCommitStatus.DUPLICATE),
        ledger_mode="strict",
    )
    parallel_engine = DecisionRuntimeEngine(
        ledger_adapter=_mock_adapter(LedgerCommitStatus.DUPLICATE),
        ledger_mode="parallel",
    )

    strict_result = strict_engine.evaluate(signal, flow)
    parallel_result = parallel_engine.evaluate(signal, flow)

    assert strict_result.status == DecisionStatus.ERROR
    assert parallel_result.status == DecisionStatus.CONFIRMED
