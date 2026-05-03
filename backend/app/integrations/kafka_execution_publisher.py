"""
KafkaExecutionPublisher — delivers EXECUTION_REQUESTED events to a Kafka topic.

Only EventType.EXECUTION_REQUESTED events are forwarded; all others are silently
dropped.  This is a safety guard: the engine already only calls publish() for
EXECUTION_REQUESTED, but explicit filtering prevents accidents if the publisher
is ever called from a different context.

Kafka delivery semantics:
    at-least-once — a single send + flush is performed per event.
    Consumers must be idempotent; the ExecutionRequest.execution_id (UUID) serves
    as the idempotency key for deduplication on the orchestrator side.

Message format:
    topic:   settings.kafka_execution_topic  (default: "runtime.execution.requested")
    key:     str(event.flow_id).encode("utf-8")   — partitions by flow for ordering
    value:   event.model_dump_json().encode("utf-8")  — full RuntimeEvent JSON

kafka-python is imported lazily; the module is importable without the package installed.
Constructing KafkaExecutionPublisher without kafka-python raises RuntimeError immediately.
Inject a producer_factory in tests to avoid a real broker connection.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from backend.app.models.event import EventType, RuntimeEvent


class KafkaExecutionPublisher:
    """Kafka-backed ExecutionPublisher.

    Args:
        bootstrap_servers:  Comma-separated "host:port" string, e.g. "localhost:9092".
                            Ignored when producer_factory is provided.
        topic:              Kafka topic to produce to.
        producer_factory:   Optional callable that returns a kafka.KafkaProducer-compatible
                            object.  Inject a MagicMock in tests to avoid a real broker.
    """

    def __init__(
        self,
        bootstrap_servers: str,
        topic: str,
        producer_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._topic = topic
        if producer_factory is not None:
            self._producer = producer_factory()
        else:
            try:
                from kafka import KafkaProducer  # type: ignore[import]
                self._producer = KafkaProducer(bootstrap_servers=bootstrap_servers)
            except ImportError as exc:
                raise RuntimeError(
                    "kafka-python is required for the kafka execution publisher. "
                    "Install it with: pip install kafka-python"
                ) from exc

    # ------------------------------------------------------------------ #
    # ExecutionPublisher interface                                          #
    # ------------------------------------------------------------------ #

    def is_ready(self) -> bool:
        """Return True if the Kafka producer was initialised successfully."""
        return self._producer is not None

    def publish(self, event: RuntimeEvent) -> None:
        """Send the event to Kafka.  Only EXECUTION_REQUESTED events are forwarded."""
        if event.event_type != EventType.EXECUTION_REQUESTED:
            return

        self._producer.send(
            self._topic,
            key=str(event.flow_id).encode("utf-8"),
            value=event.model_dump_json().encode("utf-8"),
        )
        self._producer.flush()
