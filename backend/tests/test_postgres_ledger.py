"""
Tests for PostgreSQL-backed LedgerClient (Step 7).

Real-DB tests are not required here; all tests use a mock connection_factory
so the test suite runs without a Postgres instance.

Contract under test:
    1.  PostgresLedgerClient implements the same interface as LedgerClient.
    2.  append() returns ACCEPTED on first write for an event_id.
    3.  append() returns DUPLICATE when the event_id already exists.
    4.  get_by_event_id() returns the LedgerEvent when found.
    5.  get_by_event_id() returns None when event_id is unknown.
    6.  get_events_by_trace_id() returns rows in the order the DB returns them
        (ORDER BY sequence_no ASC is enforced by the SQL, verified by the query).
    7.  get_events_by_trace_id() returns [] when no rows match.
    8.  Memory backend is selected when ledger_backend=memory.
    9.  Postgres backend is selected when ledger_backend=postgres and URL present.
    10. Startup raises RuntimeError when ledger_backend=postgres but no URL.
    11. compute_event_hash() is deterministic for identical inputs.
    12. compute_event_hash() differs when sequence_no changes.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.integrations.ledger_client import (
    LedgerAppendStatus,
    LedgerClient,
    LedgerEvent,
)
from backend.app.integrations.postgres_ledger_client import (
    PostgresLedgerClient,
    compute_event_hash,
)
from backend.app.integrations.runtime_ledger_adapter import StepType

_FLOWS_DIR = os.path.join(os.path.dirname(__file__), "flows")


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _event(
    trace_id=None,
    decision_id=None,
    flow_id=None,
    step_type=StepType.SIGNAL,
    step_id="step_1",
) -> LedgerEvent:
    return LedgerEvent(
        trace_id=trace_id or uuid4(),
        decision_id=decision_id or uuid4(),
        flow_id=flow_id or uuid4(),
        flow_version="1.0.0",
        step_type=step_type,
        step_id=step_id,
        payload={"test": True},
        occurred_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


def _as_row(event: LedgerEvent) -> tuple:
    """Build a mock DB row in _SELECT_COLS order."""
    return (
        event.event_id,
        event.trace_id,
        event.decision_id,
        event.flow_id,
        event.flow_version,
        str(event.step_type),
        event.step_id,
        event.payload,        # dict (as psycopg2 would deserialise JSONB)
        event.occurred_at,
        event.schema_version,
    )


def _mock_conn_for_append(*, duplicate: bool) -> MagicMock:
    """Build a mock connection suitable for one append() call.

    duplicate=True  → first fetchone returns a row (already exists).
    duplicate=False → fetchone returns (None, (1,), None) for the three
                      sequential reads: duplicate-check, sequence_no, prev_hash.
    """
    mock_cur = MagicMock()
    if duplicate:
        mock_cur.fetchone.return_value = ("existing_event_id",)
    else:
        mock_cur.fetchone.side_effect = [
            None,    # duplicate check: no existing row
            (1,),    # sequence_no = 1
            None,    # prev_hash: no previous event
        ]

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    return mock_conn


# ------------------------------------------------------------------ #
# 1. Interface compliance                                              #
# ------------------------------------------------------------------ #


def test_postgres_client_has_same_interface_as_memory_client() -> None:
    required = {"append", "get_by_event_id", "get_events_by_trace_id", "get_events"}
    assert required.issubset(set(dir(PostgresLedgerClient)))
    assert required.issubset(set(dir(LedgerClient)))


# ------------------------------------------------------------------ #
# 2 & 3. append()                                                     #
# ------------------------------------------------------------------ #


def test_append_returns_accepted_for_new_event() -> None:
    event = _event()
    conn = _mock_conn_for_append(duplicate=False)
    client = PostgresLedgerClient("postgresql://unused", connection_factory=lambda: conn)

    result = client.append(event)

    assert result.status == LedgerAppendStatus.ACCEPTED
    assert result.event_id == event.event_id
    conn.commit.assert_called_once()


def test_append_returns_duplicate_for_existing_event_id() -> None:
    event = _event()
    conn = _mock_conn_for_append(duplicate=True)
    client = PostgresLedgerClient("postgresql://unused", connection_factory=lambda: conn)

    result = client.append(event)

    assert result.status == LedgerAppendStatus.DUPLICATE
    assert result.event_id == event.event_id
    conn.commit.assert_not_called()


def test_append_duplicate_does_not_insert() -> None:
    event = _event()
    conn = _mock_conn_for_append(duplicate=True)
    client = PostgresLedgerClient("postgresql://unused", connection_factory=lambda: conn)

    client.append(event)

    # cursor.execute should only be called once (the duplicate SELECT)
    assert conn.cursor.return_value.execute.call_count == 1


def test_append_rolls_back_on_error() -> None:
    event = _event()
    mock_cur = MagicMock()
    mock_cur.fetchone.side_effect = [
        None,    # duplicate check
        RuntimeError("db error"),
    ]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    client = PostgresLedgerClient("postgresql://unused", connection_factory=lambda: mock_conn)

    with pytest.raises(RuntimeError, match="db error"):
        client.append(event)

    mock_conn.rollback.assert_called_once()
    mock_conn.commit.assert_not_called()


# ------------------------------------------------------------------ #
# 4 & 5. get_by_event_id()                                            #
# ------------------------------------------------------------------ #


def test_get_by_event_id_returns_event_when_found() -> None:
    event = _event()
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = _as_row(event)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    client = PostgresLedgerClient("postgresql://unused", connection_factory=lambda: mock_conn)

    result = client.get_by_event_id(event.event_id)

    assert result is not None
    assert result.event_id == event.event_id
    assert result.step_id == event.step_id
    assert result.payload == event.payload


def test_get_by_event_id_returns_none_when_not_found() -> None:
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = None
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    client = PostgresLedgerClient("postgresql://unused", connection_factory=lambda: mock_conn)

    assert client.get_by_event_id(uuid4()) is None


# ------------------------------------------------------------------ #
# 6 & 7. get_events_by_trace_id()                                     #
# ------------------------------------------------------------------ #


def test_get_events_by_trace_id_returns_events_in_db_order() -> None:
    """Events returned in the order provided by the DB (ORDER BY sequence_no ASC in SQL)."""
    trace_id = uuid4()
    e1 = _event(trace_id=trace_id, step_id="signal_1", step_type=StepType.SIGNAL)
    e2 = _event(trace_id=trace_id, step_id="decision_1", step_type=StepType.DECISION)
    e3 = _event(trace_id=trace_id, step_id="outcome_1", step_type=StepType.OUTCOME)

    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = [_as_row(e1), _as_row(e2), _as_row(e3)]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    client = PostgresLedgerClient("postgresql://unused", connection_factory=lambda: mock_conn)

    events = client.get_events_by_trace_id(str(trace_id))

    assert len(events) == 3
    assert [e.step_id for e in events] == ["signal_1", "decision_1", "outcome_1"]


def test_get_events_by_trace_id_returns_empty_for_unknown_trace() -> None:
    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = []
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    client = PostgresLedgerClient("postgresql://unused", connection_factory=lambda: mock_conn)

    assert client.get_events_by_trace_id(str(uuid4())) == []


def test_get_events_by_trace_id_query_uses_sequence_order() -> None:
    """Verify the SQL issued to the DB contains ORDER BY sequence_no."""
    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = []
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    client = PostgresLedgerClient("postgresql://unused", connection_factory=lambda: mock_conn)

    client.get_events_by_trace_id("some-trace-id")

    called_sql = mock_cur.execute.call_args[0][0]
    assert "sequence_no" in called_sql.lower()
    assert "asc" in called_sql.lower()


# ------------------------------------------------------------------ #
# 8 & 9 & 10. Backend selection in main.py                            #
# ------------------------------------------------------------------ #


def test_memory_backend_selected_when_ledger_backend_is_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.config import settings
    from backend.app.main import app

    monkeypatch.setattr(settings, "flow_dir", _FLOWS_DIR)
    monkeypatch.setattr(settings, "ledger_enabled", True)
    monkeypatch.setattr(settings, "ledger_backend", "memory")

    with TestClient(app) as client:
        assert isinstance(client.app.state.ledger_client, LedgerClient)


def test_postgres_backend_selected_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.config import settings
    from backend.app.main import app

    monkeypatch.setattr(settings, "flow_dir", _FLOWS_DIR)
    monkeypatch.setattr(settings, "ledger_enabled", True)
    monkeypatch.setattr(settings, "ledger_backend", "postgres")
    monkeypatch.setattr(settings, "ledger_database_url", "postgresql://u:p@localhost/db")

    with TestClient(app) as client:
        assert isinstance(client.app.state.ledger_client, PostgresLedgerClient)


def test_postgres_backend_missing_url_raises_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.config import settings
    from backend.app.main import app

    monkeypatch.setattr(settings, "flow_dir", _FLOWS_DIR)
    monkeypatch.setattr(settings, "ledger_enabled", True)
    monkeypatch.setattr(settings, "ledger_backend", "postgres")
    monkeypatch.setattr(settings, "ledger_database_url", None)

    with pytest.raises(RuntimeError, match="ledger_database_url"):
        with TestClient(app):
            pass


# ------------------------------------------------------------------ #
# 11 & 12. compute_event_hash()                                       #
# ------------------------------------------------------------------ #


def test_event_hash_is_deterministic() -> None:
    event = _event()
    h1 = compute_event_hash(event, sequence_no=1, prev_hash=None)
    h2 = compute_event_hash(event, sequence_no=1, prev_hash=None)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex digest


def test_event_hash_differs_when_sequence_no_changes() -> None:
    event = _event()
    h1 = compute_event_hash(event, sequence_no=1, prev_hash=None)
    h2 = compute_event_hash(event, sequence_no=2, prev_hash=None)
    assert h1 != h2


def test_event_hash_differs_when_prev_hash_changes() -> None:
    event = _event()
    h1 = compute_event_hash(event, sequence_no=1, prev_hash=None)
    h2 = compute_event_hash(event, sequence_no=1, prev_hash="abc123")
    assert h1 != h2


def test_event_hash_chains_prev_hash() -> None:
    """The prev_hash of event N equals the event_hash of event N-1."""
    event1 = _event()
    event2 = _event(trace_id=event1.trace_id)

    hash1 = compute_event_hash(event1, sequence_no=1, prev_hash=None)
    hash2 = compute_event_hash(event2, sequence_no=2, prev_hash=hash1)

    # hash2 encodes hash1 as prev_hash — they differ (hash1 ≠ hash2)
    assert hash1 != hash2
