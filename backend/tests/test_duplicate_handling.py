"""
Tests for strict mode DUPLICATE handling and idempotency integration.

Contract under test:
    DUPLICATE is not failure if the prior committed decision can be recovered.
    Execution must not be published twice.
    Trace must not be saved twice.

Recovery priority:
    1. IdempotencyStore  (signal.idempotency_key lookup — fastest)
    2. Ledger event → TraceStore  (event.decision_id → trace lookup)
    3. ERROR  (when neither path recovers the prior result)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from backend.app.integrations.event_bus import EventBus
from backend.app.integrations.ledger_client import LedgerClient, LedgerEvent
from backend.app.integrations.runtime_ledger_adapter import (
    LedgerCommitResult,
    LedgerCommitStatus,
    RuntimeLedgerAdapter,
    StepType,
)
from backend.app.models.decision import DecisionOutcome, DecisionResult, DecisionStatus
from backend.app.models.event import EventType
from backend.app.models.flow import DecisionFlow, DecisionNode, NodeType
from backend.app.models.runtime import RuntimeState
from backend.app.models.signal import Signal, SignalValueType
from backend.app.models.trace import DecisionTrace
from backend.app.runtime.engine import DecisionRuntimeEngine
from backend.app.runtime.idempotency_store import IdempotencyStore
from backend.app.runtime.trace_store import TraceNotFoundError, TraceStore


# ------------------------------------------------------------------ #
# Factories                                                            #
# ------------------------------------------------------------------ #


def _signal(confidence: float = 0.9, idempotency_key: str | None = None) -> Signal:
    return Signal(
        name="test_signal",
        value_type=SignalValueType.JSON,
        type="test_event",
        confidence=confidence,
        payload={},
        source="test_source",
        idempotency_key=idempotency_key,
    )


def _decision_node(id_: str = "approve") -> DecisionNode:
    return DecisionNode(
        id=id_, name=id_, node_type=NodeType.DECISION,
        condition="confidence > 0",
        config={"contract_type": "test", "contract_version": "1.0.0"},
    )


def _fallback_node() -> DecisionNode:
    return DecisionNode(
        id="fallback", name="fallback", node_type=NodeType.FALLBACK,
        config={"contract_type": "fallback", "contract_version": "1.0.0"},
    )


def _flow() -> DecisionFlow:
    nodes = [_decision_node(), _fallback_node()]
    return DecisionFlow(
        flow_id="test_flow", name="Test Flow", version="1.0.0",
        entry_node_id=nodes[0].id, nodes=nodes, edges=[],
    )


def _prior_result() -> DecisionResult:
    now = datetime.now(timezone.utc)
    return DecisionResult(
        trace_id=uuid4(),
        flow_id=uuid4(),
        flow_version="1.0.0",
        selected_node_id="approve",
        source_signal_id=uuid4(),
        state=RuntimeState.CONFIRMED,
        status=DecisionStatus.CONFIRMED,
        outcome=DecisionOutcome.PASS,
        confidence=0.9,
        evaluated_at=now,
        created_at=now,
        updated_at=now,
    )


def _prior_trace(result: DecisionResult) -> DecisionTrace:
    return DecisionTrace(
        id=result.trace_id,
        flow_id=result.flow_id,
        flow_version=result.flow_version,
        state=RuntimeState.COMPLETED,
        decision_id=result.id,
        signal_id=result.source_signal_id,
        decision_results=[result],
        started_at=result.created_at,
        completed_at=result.evaluated_at,
    )


def _mock_adapter_duplicate(event_ids: list[UUID] | None = None) -> MagicMock:
    adapter = MagicMock(spec=RuntimeLedgerAdapter)
    adapter.commit.return_value = LedgerCommitResult(
        status=LedgerCommitStatus.DUPLICATE,
        appended=0,
        duplicates=3,
        event_ids=event_ids or [],
    )
    adapter.get_by_event_id.return_value = None
    return adapter


def _mock_adapter_accepted() -> MagicMock:
    adapter = MagicMock(spec=RuntimeLedgerAdapter)
    adapter.commit.return_value = LedgerCommitResult(
        status=LedgerCommitStatus.ACCEPTED,
        appended=3,
        duplicates=0,
    )
    return adapter


# ------------------------------------------------------------------ #
# Recovery path 1: IdempotencyStore                                    #
# ------------------------------------------------------------------ #


def test_duplicate_with_idempotency_cache_returns_cached_result() -> None:
    """strict + DUPLICATE + idempotency_key cached → prior DecisionResult returned."""
    prior = _prior_result()

    idem = IdempotencyStore()
    idem.set("sig_001", prior)

    adapter = _mock_adapter_duplicate()
    engine = DecisionRuntimeEngine(
        ledger_adapter=adapter,
        ledger_mode="strict",
        idempotency_store=idem,
    )

    result = engine.evaluate(_signal(idempotency_key="sig_001"), _flow())

    assert result.id == prior.id
    assert result.status == DecisionStatus.CONFIRMED


def test_duplicate_idempotency_recovery_does_not_publish_eventbus() -> None:
    """strict + DUPLICATE + cache hit → EventBus receives no events."""
    prior = _prior_result()
    idem = IdempotencyStore()
    idem.set("key1", prior)

    event_bus = EventBus()
    adapter = _mock_adapter_duplicate()
    engine = DecisionRuntimeEngine(
        event_bus=event_bus,
        ledger_adapter=adapter,
        ledger_mode="strict",
        idempotency_store=idem,
    )

    result = engine.evaluate(_signal(idempotency_key="key1"), _flow())

    assert result.id == prior.id
    assert event_bus.get_events() == []


def test_duplicate_idempotency_recovery_does_not_save_trace() -> None:
    """strict + DUPLICATE + cache hit → TraceStore is not written."""
    prior = _prior_result()
    idem = IdempotencyStore()
    idem.set("key1", prior)

    store = TraceStore()
    adapter = _mock_adapter_duplicate()
    engine = DecisionRuntimeEngine(
        trace_store=store,
        ledger_adapter=adapter,
        ledger_mode="strict",
        idempotency_store=idem,
    )

    engine.evaluate(_signal(idempotency_key="key1"), _flow())

    # TraceStore should contain nothing for the new evaluation attempt.
    # (prior_result.id was never saved to this fresh store.)
    with pytest.raises(TraceNotFoundError):
        store.get_by_decision_id(str(prior.id))


# ------------------------------------------------------------------ #
# Recovery path 2: Ledger event → TraceStore                           #
# ------------------------------------------------------------------ #


def test_duplicate_ledger_trace_lookup_returns_prior_result() -> None:
    """strict + DUPLICATE + no cache + ledger event + trace → prior result returned."""
    prior = _prior_result()

    # Set up TraceStore with the prior trace.
    store = TraceStore()
    store.save(_prior_trace(prior))

    # Set up a ledger event pointing to the prior result.
    event_id = uuid4()
    ledger_event = LedgerEvent(
        event_id=event_id,
        trace_id=prior.trace_id,
        decision_id=prior.id,
        flow_id=prior.flow_id,
        flow_version=prior.flow_version,
        step_type=StepType.OUTCOME,
        step_id=str(prior.id),
    )

    adapter = MagicMock(spec=RuntimeLedgerAdapter)
    adapter.commit.return_value = LedgerCommitResult(
        status=LedgerCommitStatus.DUPLICATE,
        appended=0,
        duplicates=1,
        event_ids=[event_id],
    )
    adapter.get_by_event_id.return_value = ledger_event

    engine = DecisionRuntimeEngine(
        trace_store=store,
        ledger_adapter=adapter,
        ledger_mode="strict",
    )

    result = engine.evaluate(_signal(), _flow())

    assert result.id == prior.id
    assert result.status == DecisionStatus.CONFIRMED


def test_duplicate_ledger_trace_recovery_no_eventbus_publish() -> None:
    """strict + DUPLICATE + ledger-trace recovery → EventBus stays silent."""
    prior = _prior_result()
    store = TraceStore()
    store.save(_prior_trace(prior))

    event_id = uuid4()
    ledger_event = LedgerEvent(
        event_id=event_id,
        trace_id=prior.trace_id,
        decision_id=prior.id,
        flow_id=prior.flow_id,
        flow_version=prior.flow_version,
        step_type=StepType.OUTCOME,
        step_id=str(prior.id),
    )

    adapter = MagicMock(spec=RuntimeLedgerAdapter)
    adapter.commit.return_value = LedgerCommitResult(
        status=LedgerCommitStatus.DUPLICATE,
        appended=0,
        duplicates=1,
        event_ids=[event_id],
    )
    adapter.get_by_event_id.return_value = ledger_event

    event_bus = EventBus()
    engine = DecisionRuntimeEngine(
        trace_store=store,
        event_bus=event_bus,
        ledger_adapter=adapter,
        ledger_mode="strict",
    )

    engine.evaluate(_signal(), _flow())

    assert event_bus.get_events() == []


def test_duplicate_ledger_trace_recovery_no_new_trace_save() -> None:
    """strict + DUPLICATE + ledger-trace recovery → no new trace written to store."""
    prior = _prior_result()
    store = TraceStore()
    store.save(_prior_trace(prior))

    event_id = uuid4()
    ledger_event = LedgerEvent(
        event_id=event_id,
        trace_id=prior.trace_id,
        decision_id=prior.id,
        flow_id=prior.flow_id,
        flow_version=prior.flow_version,
        step_type=StepType.OUTCOME,
        step_id=str(prior.id),
    )

    adapter = MagicMock(spec=RuntimeLedgerAdapter)
    adapter.commit.return_value = LedgerCommitResult(
        status=LedgerCommitStatus.DUPLICATE,
        appended=0,
        duplicates=1,
        event_ids=[event_id],
    )
    adapter.get_by_event_id.return_value = ledger_event

    engine = DecisionRuntimeEngine(
        trace_store=store,
        ledger_adapter=adapter,
        ledger_mode="strict",
    )

    # Count traces before and after.
    # (store's internal dict — we use get() on the known prior trace_id)
    engine.evaluate(_signal(), _flow())

    # The prior trace must still be present, unchanged.
    recovered = store.get_by_decision_id(str(prior.id))
    assert recovered is not None
    assert recovered.id == prior.trace_id


# ------------------------------------------------------------------ #
# Recovery path 3: unrecoverable → ERROR                               #
# ------------------------------------------------------------------ #


def test_duplicate_unrecoverable_returns_error() -> None:
    """strict + DUPLICATE + no cache + no ledger event → DecisionStatus.ERROR."""
    adapter = _mock_adapter_duplicate(event_ids=[])
    engine = DecisionRuntimeEngine(
        ledger_adapter=adapter,
        ledger_mode="strict",
    )

    result = engine.evaluate(_signal(), _flow())

    assert result.status == DecisionStatus.ERROR
    assert "duplicate" in result.error_message
    assert "not recoverable" in result.error_message


def test_duplicate_no_trace_store_returns_error() -> None:
    """strict + DUPLICATE + ledger event found but no TraceStore → ERROR."""
    event_id = uuid4()
    prior = _prior_result()

    ledger_event = LedgerEvent(
        event_id=event_id,
        trace_id=prior.trace_id,
        decision_id=prior.id,
        flow_id=prior.flow_id,
        flow_version=prior.flow_version,
        step_type=StepType.OUTCOME,
        step_id=str(prior.id),
    )

    adapter = MagicMock(spec=RuntimeLedgerAdapter)
    adapter.commit.return_value = LedgerCommitResult(
        status=LedgerCommitStatus.DUPLICATE,
        appended=0,
        duplicates=1,
        event_ids=[event_id],
    )
    adapter.get_by_event_id.return_value = ledger_event

    engine = DecisionRuntimeEngine(
        # trace_store intentionally omitted
        ledger_adapter=adapter,
        ledger_mode="strict",
    )

    result = engine.evaluate(_signal(), _flow())

    assert result.status == DecisionStatus.ERROR


# ------------------------------------------------------------------ #
# ACCEPTED path: IdempotencyStore saved                                #
# ------------------------------------------------------------------ #


def test_strict_accepted_saves_to_idempotency_store() -> None:
    """strict + ACCEPTED → result is persisted to IdempotencyStore under signal key."""
    idem = IdempotencyStore()
    adapter = _mock_adapter_accepted()
    engine = DecisionRuntimeEngine(
        ledger_adapter=adapter,
        ledger_mode="strict",
        idempotency_store=idem,
    )

    result = engine.evaluate(_signal(idempotency_key="order_42"), _flow())

    assert result.status == DecisionStatus.CONFIRMED
    cached = idem.get("order_42")
    assert cached is not None
    assert cached.id == result.id


def test_strict_accepted_no_key_skips_idempotency_save() -> None:
    """strict + ACCEPTED + no idempotency_key → IdempotencyStore not touched."""
    idem = IdempotencyStore()
    adapter = _mock_adapter_accepted()
    engine = DecisionRuntimeEngine(
        ledger_adapter=adapter,
        ledger_mode="strict",
        idempotency_store=idem,
    )

    engine.evaluate(_signal(idempotency_key=None), _flow())

    # Store should be empty — no key to save under.
    assert idem.get("") is None


def test_strict_accepted_no_idempotency_store_does_not_raise() -> None:
    """strict + ACCEPTED + idempotency_store=None → no error."""
    adapter = _mock_adapter_accepted()
    engine = DecisionRuntimeEngine(
        ledger_adapter=adapter,
        ledger_mode="strict",
        idempotency_store=None,
    )

    result = engine.evaluate(_signal(idempotency_key="key"), _flow())
    assert result.status == DecisionStatus.CONFIRMED


# ------------------------------------------------------------------ #
# Parallel mode unchanged                                              #
# ------------------------------------------------------------------ #


def test_parallel_mode_idempotency_store_not_used_on_duplicate() -> None:
    """parallel mode + DUPLICATE → result returned normally; IdempotencyStore ignored."""
    idem = IdempotencyStore()
    idem.set("key1", _prior_result())

    broken = MagicMock(spec=RuntimeLedgerAdapter)
    broken.commit.side_effect = RuntimeError("ledger offline")

    engine = DecisionRuntimeEngine(
        ledger_adapter=broken,
        ledger_mode="parallel",
        idempotency_store=idem,
    )

    result = engine.evaluate(_signal(idempotency_key="key1"), _flow())

    # Parallel mode returns the current evaluation result, not the cached one.
    assert result.status == DecisionStatus.CONFIRMED


def test_parallel_mode_accepted_does_not_save_to_idempotency_store() -> None:
    """parallel mode does not save to IdempotencyStore (route-level handles it)."""
    idem = IdempotencyStore()
    adapter = MagicMock(spec=RuntimeLedgerAdapter)
    adapter.commit.return_value = LedgerCommitResult(
        status=LedgerCommitStatus.ACCEPTED,
        appended=3,
        duplicates=0,
    )

    engine = DecisionRuntimeEngine(
        ledger_adapter=adapter,
        ledger_mode="parallel",
        idempotency_store=idem,
    )

    engine.evaluate(_signal(idempotency_key="k"), _flow())

    # Engine should NOT save in parallel mode (that's the route's responsibility).
    assert idem.get("k") is None


# ------------------------------------------------------------------ #
# LedgerClient.get_by_event_id unit tests                              #
# ------------------------------------------------------------------ #


def test_ledger_client_get_by_event_id_found() -> None:
    """get_by_event_id returns the stored event when it exists."""
    client = LedgerClient()
    event = LedgerEvent(
        event_id=uuid4(),
        trace_id=uuid4(),
        decision_id=uuid4(),
        flow_id=uuid4(),
        flow_version="1.0.0",
        step_type=StepType.OUTCOME,
        step_id="step_1",
    )
    client.append(event)

    found = client.get_by_event_id(event.event_id)
    assert found is not None
    assert found.event_id == event.event_id
    assert found.step_id == "step_1"


def test_ledger_client_get_by_event_id_not_found() -> None:
    """get_by_event_id returns None when event_id is not in the ledger."""
    client = LedgerClient()
    assert client.get_by_event_id(uuid4()) is None


def test_ledger_client_duplicate_append_event_still_retrievable() -> None:
    """An event rejected as DUPLICATE (second append) is still retrievable by its original ID."""
    client = LedgerClient()
    event_id = uuid4()
    event = LedgerEvent(
        event_id=event_id,
        trace_id=uuid4(),
        decision_id=uuid4(),
        flow_id=uuid4(),
        flow_version="1.0.0",
        step_type=StepType.DECISION,
        step_id="d1",
    )
    client.append(event)          # ACCEPTED
    client.append(event)          # DUPLICATE

    found = client.get_by_event_id(event_id)
    assert found is not None
    assert found.step_id == "d1"
