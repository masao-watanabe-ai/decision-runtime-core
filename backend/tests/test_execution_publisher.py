"""
Tests for ExecutionPublisher interface and KafkaExecutionPublisher (Step 9).

kafka-python is NOT installed in this environment; every test that needs a
Kafka producer uses producer_factory injection or sys.modules patching.

Contract under test:
    1.  NoopExecutionPublisher.publish() silently discards all events.
    2.  KafkaExecutionPublisher forwards EXECUTION_REQUESTED to the Kafka producer.
    3.  KafkaExecutionPublisher ignores events whose type is not EXECUTION_REQUESTED.
    4.  KafkaExecutionPublisher produces to the configured topic.
    5.  KafkaExecutionPublisher uses flow_id as the Kafka message key.
    6.  KafkaExecutionPublisher calls flush() after each send.
    7.  Constructing KafkaExecutionPublisher without kafka-python raises RuntimeError.
    8.  Startup raises RuntimeError when execution_publisher_backend=kafka but no servers.
    9.  noop backend is selected by default.
    10. kafka backend is selected when configured and servers are present.
    11. Engine calls both EventBus and ExecutionPublisher on a CONFIRMED decision.
    12. Engine calls ExecutionPublisher (without EventBus) on a CONFIRMED decision.
    13. Engine does NOT call ExecutionPublisher when strict Ledger commit fails.
    14. Engine does NOT call ExecutionPublisher on duplicate-recovery return.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.integrations.event_bus import EventBus
from backend.app.integrations.execution_publisher import (
    ExecutionPublisher,
    NoopExecutionPublisher,
)
from backend.app.integrations.kafka_execution_publisher import KafkaExecutionPublisher
from backend.app.integrations.runtime_ledger_adapter import (
    LedgerCommitResult,
    LedgerCommitStatus,
    RuntimeLedgerAdapter,
)
from backend.app.models.decision import DecisionOutcome, DecisionResult, DecisionStatus
from backend.app.models.event import EventType, RuntimeEvent
from backend.app.models.flow import DecisionFlow, DecisionNode, NodeType
from backend.app.models.runtime import RuntimeState
from backend.app.models.signal import Signal, SignalValueType
from backend.app.models.trace import DecisionTrace
from backend.app.runtime.engine import DecisionRuntimeEngine
from backend.app.runtime.idempotency_store import IdempotencyStore

_FLOWS_DIR = os.path.join(os.path.dirname(__file__), "flows")


# ------------------------------------------------------------------ #
# Shared factories                                                     #
# ------------------------------------------------------------------ #


def _runtime_event(event_type: EventType = EventType.EXECUTION_REQUESTED) -> RuntimeEvent:
    return RuntimeEvent(
        event_type=event_type,
        flow_id=uuid4(),
        trace_id=uuid4(),
        decision_id=uuid4(),
        payload={"execution_id": str(uuid4())},
    )


def _signal(idempotency_key: str | None = None) -> Signal:
    return Signal(
        name="test", type="test_event", value_type=SignalValueType.JSON,
        confidence=0.9, payload={}, source="test",
        idempotency_key=idempotency_key,
    )


def _decision_node() -> DecisionNode:
    return DecisionNode(
        id="approve", name="approve", node_type=NodeType.DECISION,
        condition="confidence > 0",
        config={"contract_type": "test", "contract_version": "1.0.0"},
    )


def _fallback_node() -> DecisionNode:
    return DecisionNode(
        id="fallback", name="fallback", node_type=NodeType.FALLBACK,
        config={"contract_type": "fallback", "contract_version": "1.0.0"},
    )


def _confirmed_flow() -> DecisionFlow:
    nodes = [_decision_node(), _fallback_node()]
    return DecisionFlow(
        flow_id="test_flow", name="Test", version="1.0.0",
        entry_node_id="approve", nodes=nodes, edges=[],
    )


def _prior_result() -> DecisionResult:
    now = datetime.now(timezone.utc)
    return DecisionResult(
        trace_id=uuid4(), flow_id=uuid4(), flow_version="1.0.0",
        selected_node_id="approve", source_signal_id=uuid4(),
        state=RuntimeState.CONFIRMED, status=DecisionStatus.CONFIRMED,
        outcome=DecisionOutcome.PASS, confidence=0.9,
        evaluated_at=now, created_at=now, updated_at=now,
    )


def _mock_adapter(status: LedgerCommitStatus) -> MagicMock:
    adapter = MagicMock(spec=RuntimeLedgerAdapter)
    adapter.commit.return_value = LedgerCommitResult(
        status=status, appended=3 if status == LedgerCommitStatus.ACCEPTED else 0,
        duplicates=3 if status == LedgerCommitStatus.DUPLICATE else 0,
        event_ids=[],
    )
    adapter.get_by_event_id.return_value = None
    return adapter


# ------------------------------------------------------------------ #
# 1. NoopExecutionPublisher                                            #
# ------------------------------------------------------------------ #


def test_noop_publisher_publish_does_nothing() -> None:
    publisher = NoopExecutionPublisher()
    event = _runtime_event()
    publisher.publish(event)  # must not raise


def test_noop_publisher_satisfies_protocol() -> None:
    assert isinstance(NoopExecutionPublisher(), ExecutionPublisher)


def test_noop_publisher_publish_does_not_mutate_event() -> None:
    publisher = NoopExecutionPublisher()
    event = _runtime_event()
    original_id = event.id
    publisher.publish(event)
    assert event.id == original_id


# ------------------------------------------------------------------ #
# 2–6. KafkaExecutionPublisher behaviour                               #
# ------------------------------------------------------------------ #


def _kafka_publisher(topic: str = "runtime.execution.requested") -> tuple[KafkaExecutionPublisher, MagicMock]:
    mock_producer = MagicMock()
    publisher = KafkaExecutionPublisher(
        bootstrap_servers="localhost:9092",
        topic=topic,
        producer_factory=lambda: mock_producer,
    )
    return publisher, mock_producer


def test_kafka_publisher_sends_execution_requested() -> None:
    publisher, mock_producer = _kafka_publisher()
    event = _runtime_event(EventType.EXECUTION_REQUESTED)

    publisher.publish(event)

    mock_producer.send.assert_called_once()


def test_kafka_publisher_ignores_other_event_types() -> None:
    publisher, mock_producer = _kafka_publisher()

    for event_type in [
        EventType.FLOW_STARTED,
        EventType.DECISION_MADE,
        EventType.BOUNDARY_VIOLATED,
        EventType.HUMAN_GATE_OPENED,
    ]:
        publisher.publish(_runtime_event(event_type))

    mock_producer.send.assert_not_called()
    mock_producer.flush.assert_not_called()


def test_kafka_publisher_sends_to_correct_topic() -> None:
    publisher, mock_producer = _kafka_publisher(topic="my.execution.topic")
    event = _runtime_event()

    publisher.publish(event)

    assert mock_producer.send.call_args[0][0] == "my.execution.topic"


def test_kafka_publisher_uses_flow_id_as_key() -> None:
    publisher, mock_producer = _kafka_publisher()
    event = _runtime_event()

    publisher.publish(event)

    _, kwargs = mock_producer.send.call_args
    assert kwargs["key"] == str(event.flow_id).encode("utf-8")


def test_kafka_publisher_value_is_valid_event_json() -> None:
    publisher, mock_producer = _kafka_publisher()
    event = _runtime_event()

    publisher.publish(event)

    _, kwargs = mock_producer.send.call_args
    recovered = RuntimeEvent.model_validate_json(kwargs["value"].decode("utf-8"))
    assert recovered.id == event.id
    assert recovered.event_type == event.event_type


def test_kafka_publisher_calls_flush_after_send() -> None:
    publisher, mock_producer = _kafka_publisher()
    publisher.publish(_runtime_event())

    mock_producer.flush.assert_called_once()


def test_kafka_publisher_does_not_flush_when_event_ignored() -> None:
    publisher, mock_producer = _kafka_publisher()
    publisher.publish(_runtime_event(EventType.FLOW_STARTED))

    mock_producer.flush.assert_not_called()


# ------------------------------------------------------------------ #
# 7. kafka-python not installed → clear RuntimeError                  #
# ------------------------------------------------------------------ #


def test_kafka_not_installed_raises_runtime_error() -> None:
    with patch.dict(sys.modules, {"kafka": None}):
        with pytest.raises(RuntimeError, match="kafka-python is required"):
            KafkaExecutionPublisher("localhost:9092", "topic")


# ------------------------------------------------------------------ #
# 8–10. Backend selection via main.py / lifespan                      #
# ------------------------------------------------------------------ #


def test_noop_backend_selected_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.config import settings
    from backend.app.main import app

    monkeypatch.setattr(settings, "flow_dir", _FLOWS_DIR)
    monkeypatch.setattr(settings, "execution_publisher_backend", "noop")

    with TestClient(app) as client:
        assert isinstance(client.app.state.execution_publisher, NoopExecutionPublisher)


def test_kafka_backend_selected_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.config import settings
    from backend.app.main import app

    monkeypatch.setattr(settings, "flow_dir", _FLOWS_DIR)
    monkeypatch.setattr(settings, "execution_publisher_backend", "kafka")
    monkeypatch.setattr(settings, "kafka_bootstrap_servers", "localhost:9092")
    monkeypatch.setattr(settings, "kafka_execution_topic", "runtime.execution.requested")

    mock_kafka_module = MagicMock()
    mock_kafka_module.KafkaProducer.return_value = MagicMock()

    with patch.dict(sys.modules, {"kafka": mock_kafka_module}):
        with TestClient(app) as client:
            assert isinstance(client.app.state.execution_publisher, KafkaExecutionPublisher)


def test_kafka_missing_servers_raises_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.config import settings
    from backend.app.main import app

    monkeypatch.setattr(settings, "flow_dir", _FLOWS_DIR)
    monkeypatch.setattr(settings, "execution_publisher_backend", "kafka")
    monkeypatch.setattr(settings, "kafka_bootstrap_servers", None)

    with pytest.raises(RuntimeError, match="kafka_bootstrap_servers"):
        with TestClient(app):
            pass


# ------------------------------------------------------------------ #
# 11–12. Engine dispatches to publisher on CONFIRMED                  #
# ------------------------------------------------------------------ #


def test_engine_calls_both_event_bus_and_publisher_on_confirmed() -> None:
    """CONFIRMED decision → EventBus and ExecutionPublisher both receive the event."""
    bus = EventBus()
    mock_publisher = MagicMock()
    engine = DecisionRuntimeEngine(
        event_bus=bus,
        execution_publisher=mock_publisher,
    )

    result = engine.evaluate(_signal(), _confirmed_flow())

    assert result.status == DecisionStatus.CONFIRMED
    assert result.execution_id is not None

    # EventBus received the internal record.
    events = bus.get_events()
    assert len(events) == 1
    assert events[0].event_type == EventType.EXECUTION_REQUESTED

    # ExecutionPublisher received the external hand-off.
    mock_publisher.publish.assert_called_once()
    published_event = mock_publisher.publish.call_args[0][0]
    assert published_event.event_type == EventType.EXECUTION_REQUESTED
    assert published_event.payload["execution_id"] == result.execution_id


def test_engine_calls_publisher_without_event_bus() -> None:
    """Publisher is called even when EventBus is not configured."""
    mock_publisher = MagicMock()
    engine = DecisionRuntimeEngine(execution_publisher=mock_publisher)

    result = engine.evaluate(_signal(), _confirmed_flow())

    assert result.status == DecisionStatus.CONFIRMED
    mock_publisher.publish.assert_called_once()


def test_engine_publisher_receives_execution_requested_event_type() -> None:
    mock_publisher = MagicMock()
    engine = DecisionRuntimeEngine(execution_publisher=mock_publisher)

    engine.evaluate(_signal(), _confirmed_flow())

    event_arg = mock_publisher.publish.call_args[0][0]
    assert event_arg.event_type == EventType.EXECUTION_REQUESTED


# ------------------------------------------------------------------ #
# 13. Strict Ledger failure → publisher NOT called                    #
# ------------------------------------------------------------------ #


def test_engine_does_not_call_publisher_on_strict_ledger_failure() -> None:
    mock_publisher = MagicMock()
    adapter = _mock_adapter(LedgerCommitStatus.FAILED)
    engine = DecisionRuntimeEngine(
        ledger_adapter=adapter,
        ledger_mode="strict",
        execution_publisher=mock_publisher,
    )

    result = engine.evaluate(_signal(), _confirmed_flow())

    assert result.status == DecisionStatus.ERROR
    mock_publisher.publish.assert_not_called()


def test_engine_does_not_call_event_bus_on_strict_ledger_failure() -> None:
    bus = EventBus()
    adapter = _mock_adapter(LedgerCommitStatus.FAILED)
    engine = DecisionRuntimeEngine(
        event_bus=bus, ledger_adapter=adapter, ledger_mode="strict",
    )

    engine.evaluate(_signal(), _confirmed_flow())

    assert bus.get_events() == []


# ------------------------------------------------------------------ #
# 14. Duplicate recovery → publisher NOT called                       #
# ------------------------------------------------------------------ #


def test_engine_does_not_call_publisher_on_duplicate_recovery() -> None:
    prior = _prior_result()
    idem = IdempotencyStore()
    idem.set("dup_key", prior)

    mock_publisher = MagicMock()
    adapter = _mock_adapter(LedgerCommitStatus.DUPLICATE)
    engine = DecisionRuntimeEngine(
        ledger_adapter=adapter,
        ledger_mode="strict",
        idempotency_store=idem,
        execution_publisher=mock_publisher,
    )

    result = engine.evaluate(_signal(idempotency_key="dup_key"), _confirmed_flow())

    assert result.id == prior.id
    mock_publisher.publish.assert_not_called()


def test_engine_does_not_call_event_bus_on_duplicate_recovery() -> None:
    prior = _prior_result()
    idem = IdempotencyStore()
    idem.set("dup_key2", prior)

    bus = EventBus()
    adapter = _mock_adapter(LedgerCommitStatus.DUPLICATE)
    engine = DecisionRuntimeEngine(
        event_bus=bus,
        ledger_adapter=adapter,
        ledger_mode="strict",
        idempotency_store=idem,
    )

    engine.evaluate(_signal(idempotency_key="dup_key2"), _confirmed_flow())

    assert bus.get_events() == []
