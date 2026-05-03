"""
Tests for Redis Streams-backed EventBus (Step 8).

redis-py is NOT installed in this environment; every test either injects a
mock Redis client via redis_factory or uses sys.modules patching to simulate
import failure.  No real Redis connection is ever opened.

Contract under test:
    1.  RedisEventBus implements the same public interface as EventBus.
    2.  publish() calls XADD on the correct stream with serialised event JSON.
    3.  get_events() calls XRANGE and deserialises entries back to RuntimeEvents.
    4.  get_events(limit=n) passes count=n to XRANGE.
    5.  get_events() with no limit passes count=None to XRANGE.
    6.  publish→get_events roundtrip preserves the full RuntimeEvent.
    7.  get_events() returns [] when the stream is empty.
    8.  get_events() handles bytes values (decode_responses=False fallback).
    9.  Memory backend is selected by default (event_bus_backend=memory).
    10. Redis backend is selected when event_bus_backend=redis and url present.
    11. Startup raises RuntimeError when event_bus_backend=redis but no url.
    12. Constructing RedisEventBus without redis-py raises a clear RuntimeError.
"""
from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.integrations.event_bus import EventBus
from backend.app.integrations.redis_event_bus import RedisEventBus
from backend.app.models.event import EventType, RuntimeEvent

_FLOWS_DIR = os.path.join(os.path.dirname(__file__), "flows")


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _mock_redis() -> MagicMock:
    """Return a MagicMock that mimics a redis.Redis client."""
    mock = MagicMock()
    mock.xrange.return_value = []
    return mock


def _event(**kwargs: Any) -> RuntimeEvent:
    return RuntimeEvent(
        event_type=EventType.EXECUTION_REQUESTED,
        flow_id=uuid4(),
        trace_id=uuid4(),
        decision_id=uuid4(),
        **kwargs,
    )


def _make_stream_entry(event: RuntimeEvent) -> tuple[str, dict]:
    """Simulate the (entry_id, fields) tuple XRANGE returns."""
    return ("1700000000000-0", {"event_json": event.model_dump_json()})


def _make_bus(mock: MagicMock | None = None, stream: str = "runtime:events") -> RedisEventBus:
    m = mock if mock is not None else _mock_redis()
    return RedisEventBus("redis://unused", stream, redis_factory=lambda: m)


# ------------------------------------------------------------------ #
# 1. Interface compliance                                              #
# ------------------------------------------------------------------ #


def test_redis_event_bus_has_same_interface_as_memory_bus() -> None:
    required = {"publish", "get_events"}
    assert required.issubset(set(dir(RedisEventBus)))
    assert required.issubset(set(dir(EventBus)))


# ------------------------------------------------------------------ #
# 2. publish() → XADD                                                 #
# ------------------------------------------------------------------ #


def test_publish_calls_xadd_on_correct_stream() -> None:
    mock = _mock_redis()
    bus = _make_bus(mock, stream="my:stream")
    event = _event()

    bus.publish(event)

    mock.xadd.assert_called_once()
    call_args = mock.xadd.call_args
    assert call_args[0][0] == "my:stream"


def test_publish_xadd_payload_contains_event_json() -> None:
    mock = _mock_redis()
    bus = _make_bus(mock)
    event = _event()

    bus.publish(event)

    fields = mock.xadd.call_args[0][1]
    assert "event_json" in fields
    recovered = RuntimeEvent.model_validate_json(fields["event_json"])
    assert recovered.id == event.id


def test_publish_does_not_alter_event() -> None:
    """publish must not modify the RuntimeEvent it receives."""
    mock = _mock_redis()
    bus = _make_bus(mock)
    event = _event(payload={"key": "value"})
    original_id = event.id

    bus.publish(event)

    assert event.id == original_id
    assert event.payload == {"key": "value"}


# ------------------------------------------------------------------ #
# 3–5. get_events() → XRANGE                                          #
# ------------------------------------------------------------------ #


def test_get_events_calls_xrange_on_correct_stream() -> None:
    mock = _mock_redis()
    bus = _make_bus(mock, stream="rt:events")

    bus.get_events()

    mock.xrange.assert_called_once()
    assert mock.xrange.call_args[0][0] == "rt:events"


def test_get_events_passes_count_none_when_no_limit() -> None:
    mock = _mock_redis()
    bus = _make_bus(mock)

    bus.get_events()

    _, kwargs = mock.xrange.call_args
    assert kwargs.get("count") is None


def test_get_events_passes_count_when_limit_given() -> None:
    mock = _mock_redis()
    bus = _make_bus(mock)

    bus.get_events(limit=25)

    _, kwargs = mock.xrange.call_args
    assert kwargs.get("count") == 25


def test_get_events_xrange_uses_full_range() -> None:
    """XRANGE must scan from "-" to "+" (the full stream)."""
    mock = _mock_redis()
    bus = _make_bus(mock)
    bus.get_events()

    pos_args = mock.xrange.call_args[0]
    assert pos_args[1] == "-"
    assert pos_args[2] == "+"


def test_get_events_returns_empty_for_empty_stream() -> None:
    mock = _mock_redis()
    mock.xrange.return_value = []
    bus = _make_bus(mock)

    assert bus.get_events() == []


def test_get_events_returns_runtime_events() -> None:
    event = _event()
    mock = _mock_redis()
    mock.xrange.return_value = [_make_stream_entry(event)]
    bus = _make_bus(mock)

    result = bus.get_events()

    assert len(result) == 1
    assert isinstance(result[0], RuntimeEvent)
    assert result[0].id == event.id


def test_get_events_preserves_publication_order() -> None:
    e1 = _event()
    e2 = _event()
    e3 = _event()
    mock = _mock_redis()
    mock.xrange.return_value = [
        _make_stream_entry(e1),
        _make_stream_entry(e2),
        _make_stream_entry(e3),
    ]
    bus = _make_bus(mock)

    result = bus.get_events()

    assert [r.id for r in result] == [e1.id, e2.id, e3.id]


# ------------------------------------------------------------------ #
# 6. publish → get_events roundtrip                                   #
# ------------------------------------------------------------------ #


def test_publish_get_events_roundtrip_preserves_event() -> None:
    """Full serialise → deserialise cycle through the stream."""
    event = _event(
        payload={"execution_id": "exec_abc", "decision_id": str(uuid4())},
    )

    captured: list[str] = []

    def capture_xadd(stream_name: str, fields: dict) -> None:
        captured.append(fields["event_json"])

    mock = _mock_redis()
    mock.xadd.side_effect = capture_xadd

    bus = _make_bus(mock)
    bus.publish(event)

    mock.xrange.return_value = [("entry-1", {"event_json": captured[0]})]
    recovered = bus.get_events()

    assert len(recovered) == 1
    r = recovered[0]
    assert r.id == event.id
    assert r.event_type == event.event_type
    assert r.flow_id == event.flow_id
    assert r.trace_id == event.trace_id
    assert r.decision_id == event.decision_id
    assert r.payload == event.payload


# ------------------------------------------------------------------ #
# 8. bytes values (decode_responses=False fallback)                   #
# ------------------------------------------------------------------ #


def test_get_events_handles_bytes_field_values() -> None:
    """When decode_responses is not set, redis-py may return bytes values."""
    event = _event()
    json_bytes = event.model_dump_json().encode("utf-8")

    mock = _mock_redis()
    mock.xrange.return_value = [("entry-1", {b"event_json": json_bytes})]
    bus = _make_bus(mock)

    result = bus.get_events()

    assert len(result) == 1
    assert result[0].id == event.id


# ------------------------------------------------------------------ #
# 9–11. Backend selection via main.py / lifespan                      #
# ------------------------------------------------------------------ #


def test_memory_backend_selected_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.config import settings
    from backend.app.main import app

    monkeypatch.setattr(settings, "flow_dir", _FLOWS_DIR)
    monkeypatch.setattr(settings, "event_bus_backend", "memory")

    with TestClient(app) as client:
        assert isinstance(client.app.state.event_bus, EventBus)


def test_redis_backend_selected_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.config import settings
    from backend.app.main import app

    monkeypatch.setattr(settings, "flow_dir", _FLOWS_DIR)
    monkeypatch.setattr(settings, "event_bus_backend", "redis")
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379")
    monkeypatch.setattr(settings, "redis_event_stream", "runtime:events")

    mock_redis_module = MagicMock()
    mock_redis_module.from_url.return_value = MagicMock()

    with patch.dict(sys.modules, {"redis": mock_redis_module}):
        with TestClient(app) as client:
            assert isinstance(client.app.state.event_bus, RedisEventBus)


def test_redis_backend_missing_url_raises_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.config import settings
    from backend.app.main import app

    monkeypatch.setattr(settings, "flow_dir", _FLOWS_DIR)
    monkeypatch.setattr(settings, "event_bus_backend", "redis")
    monkeypatch.setattr(settings, "redis_url", None)

    with pytest.raises(RuntimeError, match="redis_url"):
        with TestClient(app):
            pass


# ------------------------------------------------------------------ #
# 12. redis-py not installed → clear RuntimeError                     #
# ------------------------------------------------------------------ #


def test_redis_not_installed_raises_clear_runtime_error() -> None:
    with patch.dict(sys.modules, {"redis": None}):
        with pytest.raises(RuntimeError, match="redis is required"):
            RedisEventBus("redis://localhost:6379", "stream")
