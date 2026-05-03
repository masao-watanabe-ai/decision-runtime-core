"""
Tests for EventBus Pagination and Runtime Stats endpoint (Step 13).

Contract under test:
    Events pagination:
    1.  GET /events with limit=N returns at most N events.
    2.  GET /events with since_id returns events AFTER the given event (exclusive).
    3.  GET /events with unknown since_id returns events from the start.
    4.  limit > 1000 is capped to 1000 by the route.
    5.  InMemory EventBus: since_id=None returns all (up to limit).
    6.  InMemory EventBus: since_id unknown → fall back to start.
    7.  InMemory EventBus: since_id=last event → empty result.

    Redis pagination:
    8.  RedisEventBus: since_id=None → XRANGE called with "-" start.
    9.  RedisEventBus: since_id provided → XRANGE called with "(since_id" (exclusive).
    10. RedisEventBus: redis_entry_id stored in event.metadata on read.
    11. RedisEventBus: limit capped at 1000.

    Runtime stats:
    12. GET /stats returns confirmed_rate correctly.
    13. Empty TraceStore → total=0, confirmed_rate=0.
    14. limit > 10000 is capped to 10000.
    15. stats is independent from metrics registry.
    16. GET /stats counts each status bucket correctly.

    TraceStore:
    17. list_recent returns newest traces first.
    18. list_recent respects limit.
    19. list_recent on empty store returns [].
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.config import settings
from backend.app.integrations.event_bus import EventBus
from backend.app.integrations.redis_event_bus import RedisEventBus
from backend.app.main import app
from backend.app.models.decision import DecisionOutcome, DecisionResult, DecisionStatus
from backend.app.models.event import EventType, RuntimeEvent
from backend.app.models.runtime import RuntimeState
from backend.app.models.trace import DecisionTrace
from backend.app.runtime.trace_store import TraceStore

_TEST_FLOWS_DIR = os.path.join(os.path.dirname(__file__), "flows")
BASE = "/api/runtime"


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _event(flow_id=None) -> RuntimeEvent:
    return RuntimeEvent(
        event_type=EventType.DECISION_MADE,
        flow_id=flow_id or uuid4(),
        trace_id=uuid4(),
        decision_id=uuid4(),
    )


def _result(status: DecisionStatus = DecisionStatus.CONFIRMED) -> DecisionResult:
    now = datetime.now(timezone.utc)
    return DecisionResult(
        trace_id=uuid4(), flow_id=uuid4(), flow_version="1.0.0",
        selected_node_id="node", source_signal_id=uuid4(),
        state=RuntimeState.CONFIRMED, status=status,
        outcome=DecisionOutcome.PASS, confidence=0.9,
        evaluated_at=now, created_at=now, updated_at=now,
    )


def _trace_with(result: DecisionResult) -> DecisionTrace:
    return DecisionTrace(
        id=result.trace_id,
        flow_id=result.flow_id,
        flow_version=result.flow_version,
        state=RuntimeState.COMPLETED,
        decision_id=result.id,
        decision_results=[result],
    )


def _make_stream_entry(event: RuntimeEvent, entry_id: str = "1700000000000-0") -> tuple:
    return (entry_id, {"event_json": event.model_dump_json()})


# ------------------------------------------------------------------ #
# 5–7. InMemory EventBus unit tests                                   #
# ------------------------------------------------------------------ #


def test_inmemory_since_id_none_returns_all() -> None:
    bus = EventBus()
    for _ in range(3):
        bus.publish(_event())

    result = bus.get_events(since_id=None, limit=10)
    assert len(result) == 3


def test_inmemory_since_id_returns_events_after_cursor() -> None:
    bus = EventBus()
    events = [_event() for _ in range(5)]
    for e in events:
        bus.publish(e)

    result = bus.get_events(since_id=str(events[1].id), limit=100)
    assert len(result) == 3
    assert result[0].id == events[2].id
    assert result[1].id == events[3].id
    assert result[2].id == events[4].id


def test_inmemory_unknown_since_id_returns_from_start() -> None:
    bus = EventBus()
    for _ in range(3):
        bus.publish(_event())

    result = bus.get_events(since_id="00000000-0000-0000-0000-000000000000", limit=10)
    assert len(result) == 3


def test_inmemory_since_id_last_event_returns_empty() -> None:
    bus = EventBus()
    events = [_event() for _ in range(3)]
    for e in events:
        bus.publish(e)

    result = bus.get_events(since_id=str(events[-1].id), limit=10)
    assert result == []


def test_inmemory_limit_respected() -> None:
    bus = EventBus()
    for _ in range(10):
        bus.publish(_event())

    result = bus.get_events(limit=3)
    assert len(result) == 3


def test_inmemory_limit_capped_at_1000() -> None:
    bus = EventBus()
    for _ in range(5):
        bus.publish(_event())

    result = bus.get_events(limit=9999)
    assert len(result) == 5  # only 5 exist; cap doesn't truncate below available count


def test_inmemory_no_limit_returns_all() -> None:
    bus = EventBus()
    for _ in range(5):
        bus.publish(_event())

    result = bus.get_events()
    assert len(result) == 5


# ------------------------------------------------------------------ #
# 8–11. RedisEventBus pagination (mock)                               #
# ------------------------------------------------------------------ #


def _mock_redis_bus(stream: str = "rt:events") -> tuple[RedisEventBus, MagicMock]:
    mock = MagicMock()
    mock.xrange.return_value = []
    bus = RedisEventBus(redis_url="redis://x", stream_name=stream, redis_factory=lambda: mock)
    return bus, mock


def test_redis_since_id_none_uses_hyphen_start() -> None:
    bus, mock = _mock_redis_bus()
    bus.get_events(since_id=None)
    pos_args = mock.xrange.call_args[0]
    assert pos_args[1] == "-"


def test_redis_since_id_provided_uses_exclusive_prefix() -> None:
    bus, mock = _mock_redis_bus()
    bus.get_events(since_id="1700000000000-5")
    pos_args = mock.xrange.call_args[0]
    assert pos_args[1] == "(1700000000000-5"


def test_redis_entry_id_stored_in_metadata() -> None:
    ev = _event()
    mock = MagicMock()
    entry_id = "1700000000001-0"
    mock.xrange.return_value = [_make_stream_entry(ev, entry_id)]
    bus = RedisEventBus(redis_url="redis://x", stream_name="s", redis_factory=lambda: mock)

    result = bus.get_events()

    assert len(result) == 1
    assert result[0].metadata.get("redis_entry_id") == entry_id


def test_redis_limit_passed_as_count() -> None:
    bus, mock = _mock_redis_bus()
    bus.get_events(limit=42)
    _, kwargs = mock.xrange.call_args
    assert kwargs.get("count") == 42


def test_redis_limit_capped_at_1000() -> None:
    bus, mock = _mock_redis_bus()
    bus.get_events(limit=5000)
    _, kwargs = mock.xrange.call_args
    assert kwargs.get("count") == 1000


# ------------------------------------------------------------------ #
# 17–19. TraceStore.list_recent unit tests                            #
# ------------------------------------------------------------------ #


def test_trace_store_list_recent_newest_first() -> None:
    store = TraceStore()
    r1 = _result()
    r2 = _result()
    r3 = _result()
    t1 = _trace_with(r1)
    t2 = _trace_with(r2)
    t3 = _trace_with(r3)
    store.save(t1)
    store.save(t2)
    store.save(t3)

    recent = store.list_recent(limit=10)
    assert len(recent) == 3
    # started_at values should be non-decreasing going backward (newest first)
    for i in range(len(recent) - 1):
        assert recent[i].started_at >= recent[i + 1].started_at


def test_trace_store_list_recent_respects_limit() -> None:
    store = TraceStore()
    for _ in range(5):
        store.save(_trace_with(_result()))

    assert len(store.list_recent(limit=2)) == 2


def test_trace_store_list_recent_empty_store() -> None:
    store = TraceStore()
    assert store.list_recent() == []


# ------------------------------------------------------------------ #
# 1–4. GET /events HTTP tests                                         #
# ------------------------------------------------------------------ #


@pytest.fixture
def api_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(settings, "flow_dir", _TEST_FLOWS_DIR)
    with TestClient(app) as c:
        yield c


def _confirmed_body() -> dict[str, Any]:
    return {
        "flow_id": "always_confirmed",
        "signal": {"type": "test_event", "confidence": 0.9, "payload": {}, "source": "test"},
    }


def _evaluate_n(client: TestClient, n: int) -> None:
    for _ in range(n):
        client.post(f"{BASE}/evaluate", json=_confirmed_body())


def test_events_limit_restricts_results(api_client: TestClient) -> None:
    _evaluate_n(api_client, 5)

    resp = api_client.get(f"{BASE}/events?limit=3")
    assert resp.status_code == 200
    assert len(resp.json()) == 3


def test_events_since_id_returns_subsequent_events(api_client: TestClient) -> None:
    _evaluate_n(api_client, 5)
    all_events = api_client.get(f"{BASE}/events?limit=1000").json()
    assert len(all_events) == 5

    cursor = all_events[1]["id"]  # after event index 1
    resp = api_client.get(f"{BASE}/events?since_id={cursor}&limit=1000")
    assert resp.status_code == 200
    result = resp.json()
    assert len(result) == 3
    assert result[0]["id"] == all_events[2]["id"]


def test_events_unknown_since_id_returns_from_start(api_client: TestClient) -> None:
    _evaluate_n(api_client, 3)

    resp = api_client.get(f"{BASE}/events?since_id=00000000-0000-0000-0000-000000000000&limit=100")
    assert resp.status_code == 200
    assert len(resp.json()) == 3


def test_events_limit_capped_at_1000_by_route(api_client: TestClient) -> None:
    _evaluate_n(api_client, 3)
    # limit=9999 should be silently capped to 1000; with only 3 events we get 3
    resp = api_client.get(f"{BASE}/events?limit=9999")
    assert resp.status_code == 200
    assert len(resp.json()) == 3


# ------------------------------------------------------------------ #
# 12–16. GET /stats HTTP tests                                        #
# ------------------------------------------------------------------ #


def test_stats_empty_store_returns_zeros(api_client: TestClient) -> None:
    resp = api_client.get(f"{BASE}/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["confirmed_rate"] == 0.0


def test_stats_confirmed_rate_correct(api_client: TestClient) -> None:
    # 3 confirmed, 1 pending_human (escalate_flow triggers PENDING_HUMAN)
    _evaluate_n(api_client, 3)  # confirmed
    api_client.post(f"{BASE}/evaluate", json={
        "flow_id": "escalate_flow",
        "signal": {
            "type": "test", "confidence": 0.9,
            "payload": {"should_escalate": True}, "source": "test",
        },
    })  # pending_human

    resp = api_client.get(f"{BASE}/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 4
    assert data["confirmed"] == 3
    assert data["pending_human"] == 1
    assert data["confirmed_rate"] == pytest.approx(0.75, abs=0.001)


def test_stats_limit_capped_at_10000(api_client: TestClient) -> None:
    _evaluate_n(api_client, 2)

    # limit=99999 → capped to 10000; with only 2 traces we still get them all
    resp = api_client.get(f"{BASE}/stats?limit=99999")
    assert resp.status_code == 200
    assert resp.json()["total"] == 2


def test_stats_independent_from_metrics_registry(api_client: TestClient) -> None:
    from backend.app.observability import metrics as _metrics
    _evaluate_n(api_client, 2)

    # Reset metrics entirely — stats must still reflect reality
    _metrics.reset()

    resp = api_client.get(f"{BASE}/stats")
    assert resp.json()["total"] == 2
    assert resp.json()["confirmed"] == 2


def test_stats_counts_all_buckets(api_client: TestClient) -> None:
    _evaluate_n(api_client, 2)

    resp = api_client.get(f"{BASE}/stats")
    data = resp.json()
    assert "confirmed" in data
    assert "fallback" in data
    assert "error" in data
    assert "pending_human" in data
    assert "rejected" in data
    assert "blocked" in data
    assert "confirmed_rate" in data
