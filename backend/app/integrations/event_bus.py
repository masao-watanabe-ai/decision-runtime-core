"""
Event Bus — in-memory publish/subscribe for RuntimeEvents.

Designed for single-process use.  Events are appended in publication order
and returned in the same order from get_events().

Swap this class for a Kafka, Redis Streams, or any broker-backed
implementation without changing callers.
"""
from __future__ import annotations

from typing import Optional

from backend.app.models.event import RuntimeEvent

_MAX_LIMIT = 1000


class EventBus:
    """In-memory event bus that stores events in publication order.

    Thread-safety: not guaranteed; suitable for single-threaded use and tests.
    """

    def __init__(self) -> None:
        self._events: list[RuntimeEvent] = []

    def publish(self, event: RuntimeEvent) -> None:
        """Append an event to the store."""
        self._events.append(event)

    def get_events(
        self,
        since_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[RuntimeEvent]:
        """Return stored events in publication order (oldest first).

        Args:
            since_id: When provided, return only events published AFTER the
                      event whose ``id`` matches this string.  Unknown IDs
                      fall back to returning from the beginning.
            limit:    Maximum number of events to return.  Capped at 1000.
                      None returns all matching events (up to 1000).
        """
        events: list[RuntimeEvent] = self._events

        if since_id is not None:
            start = 0
            for idx, ev in enumerate(events):
                if str(ev.id) == since_id:
                    start = idx + 1
                    break
            events = events[start:]

        if limit is not None:
            events = events[:min(limit, _MAX_LIMIT)]

        return list(events)
