"""
Event Bus — in-memory publish/subscribe for RuntimeEvents.

Designed for single-process use.  Events are appended in publication order
and returned in the same order from get_events().

Swap this class for a Kafka, Redis Streams, or any broker-backed
implementation without changing callers.
"""
from __future__ import annotations

from backend.app.models.event import RuntimeEvent


class EventBus:
    """In-memory event bus that stores events in publication order.

    Thread-safety: not guaranteed; suitable for single-threaded use and tests.
    """

    def __init__(self) -> None:
        self._events: list[RuntimeEvent] = []

    def publish(self, event: RuntimeEvent) -> None:
        """Append an event to the store."""
        self._events.append(event)

    def get_events(self) -> list[RuntimeEvent]:
        """Return all stored events in publication order (oldest first)."""
        return list(self._events)
