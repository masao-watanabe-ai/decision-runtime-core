"""
ExecutionPublisher — interface for handing committed execution requests to external orchestrators.

Separation of concerns:
    EventBus         records internal runtime events for observability and audit.
    ExecutionPublisher   delivers EXECUTION_REQUESTED events to external systems.

The engine depends on this interface, not on any concrete transport (Kafka, HTTP, etc.).
Inject NoopExecutionPublisher in tests or when no external orchestrator is configured.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from backend.app.models.event import RuntimeEvent


@runtime_checkable
class ExecutionPublisher(Protocol):
    """Structural interface for publishing execution requests to external systems.

    Implementations must not raise for events they choose to skip (e.g. non-EXECUTION_REQUESTED).
    They must not mutate the event they receive.
    """

    def publish(self, event: RuntimeEvent) -> None:
        """Deliver the event to an external execution system."""
        ...


class NoopExecutionPublisher:
    """ExecutionPublisher that silently discards all events.

    Use as the default when no external orchestrator is configured,
    and in unit tests that do not need to assert on external delivery.
    """

    def publish(self, event: RuntimeEvent) -> None:
        pass
