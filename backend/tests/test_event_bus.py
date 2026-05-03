from __future__ import annotations

from uuid import uuid4

from backend.app.integrations.event_bus import EventBus
from backend.app.models.event import EventType, RuntimeEvent


def _event(event_type: EventType = EventType.EXECUTION_REQUESTED) -> RuntimeEvent:
    return RuntimeEvent(
        event_type=event_type,
        flow_id=uuid4(),
        trace_id=uuid4(),
    )


def test_publish_event() -> None:
    """A published event is stored and retrievable via get_events()."""
    bus = EventBus()
    event = _event()

    bus.publish(event)
    events = bus.get_events()

    assert len(events) == 1
    assert events[0].id == event.id
    assert events[0].event_type == EventType.EXECUTION_REQUESTED


def test_get_events_returns_all() -> None:
    """get_events() returns every event that was published."""
    bus = EventBus()
    e1 = _event(EventType.EXECUTION_REQUESTED)
    e2 = _event(EventType.EXECUTION_COMPLETED)
    e3 = _event(EventType.DECISION_MADE)

    bus.publish(e1)
    bus.publish(e2)
    bus.publish(e3)

    events = bus.get_events()
    assert len(events) == 3
    ids = [e.id for e in events]
    assert e1.id in ids
    assert e2.id in ids
    assert e3.id in ids


def test_event_order_preserved() -> None:
    """Events are returned in publication order (FIFO)."""
    bus = EventBus()
    events_in = [_event() for _ in range(5)]
    for e in events_in:
        bus.publish(e)

    events_out = bus.get_events()

    assert [e.id for e in events_out] == [e.id for e in events_in]
