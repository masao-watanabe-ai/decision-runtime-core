"""
Tests for Ledger Replay / Projection (Step 6).

Contract under test:
    1. GET /traces/{trace_id} returns from TraceStore when the trace is cached.
    2. GET /traces/{trace_id} falls back to LedgerProjector when TraceStore is cold.
    3. GET /traces/{trace_id} returns 404 when neither source has the trace.
    4. LedgerProjector.project() returns None when no events exist for trace_id.
    5. LedgerProjector.project() reconstructs trace fields from ledger events.
    6. Projection never mutates the ledger.
    7. Events are returned in append order by get_events_by_trace_id.
    8. Projection works correctly for a complete trace (signal + nodes + outcome).
    9. Projection with no SIGNAL event still returns a valid trace.
   10. Projection with no OUTCOME event marks state as EVALUATING.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.integrations.ledger_client import LedgerClient, LedgerEvent
from backend.app.integrations.ledger_projector import LedgerProjector
from backend.app.integrations.runtime_ledger_adapter import StepType
from backend.app.models.decision import DecisionOutcome, DecisionStatus
from backend.app.models.runtime import RuntimeState


# ------------------------------------------------------------------ #
# Factories                                                            #
# ------------------------------------------------------------------ #


def _make_event(
    trace_id: UUID,
    decision_id: UUID,
    flow_id: UUID,
    step_type: StepType,
    step_id: str,
    payload: dict | None = None,
    occurred_at: datetime | None = None,
) -> LedgerEvent:
    return LedgerEvent(
        trace_id=trace_id,
        decision_id=decision_id,
        flow_id=flow_id,
        flow_version="1.0.0",
        step_type=step_type,
        step_id=step_id,
        payload=payload or {},
        occurred_at=occurred_at or datetime.now(timezone.utc),
    )


def _populate_ledger(client: LedgerClient, trace_id: UUID) -> tuple[UUID, UUID]:
    """Add a full trace (signal + 2 nodes + outcome) to the ledger.

    Returns (signal_id, decision_id).
    """
    signal_id = uuid4()
    decision_id = uuid4()
    flow_id = uuid4()
    t0 = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2025, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
    t2 = datetime(2025, 1, 1, 0, 0, 2, tzinfo=timezone.utc)

    client.append(_make_event(
        trace_id, decision_id, flow_id,
        StepType.SIGNAL, str(signal_id),
        payload={"signal_id": str(signal_id)},
        occurred_at=t0,
    ))
    client.append(_make_event(
        trace_id, decision_id, flow_id,
        StepType.DECISION, "approve",
        payload={
            "node_id": "approve", "node_type": "decision",
            "matched": True, "condition": "confidence > 0.5", "reason": "condition matched",
        },
        occurred_at=t1,
    ))
    client.append(_make_event(
        trace_id, decision_id, flow_id,
        StepType.OUTCOME, str(decision_id),
        payload={
            "decision_id": str(decision_id),
            "status": "confirmed",
            "outcome": "pass",
            "selected_node_id": "approve",
            "confidence": 0.9,
        },
        occurred_at=t2,
    ))
    return signal_id, decision_id


# ------------------------------------------------------------------ #
# LedgerClient.get_events_by_trace_id                                 #
# ------------------------------------------------------------------ #


def test_get_events_by_trace_id_returns_events_in_append_order() -> None:
    client = LedgerClient()
    trace_id = uuid4()
    decision_id = uuid4()
    flow_id = uuid4()

    e1 = _make_event(trace_id, decision_id, flow_id, StepType.SIGNAL, "s1")
    e2 = _make_event(trace_id, decision_id, flow_id, StepType.DECISION, "d1")
    e3 = _make_event(trace_id, decision_id, flow_id, StepType.OUTCOME, "o1")
    for e in [e1, e2, e3]:
        client.append(e)

    events = client.get_events_by_trace_id(str(trace_id))
    assert len(events) == 3
    assert [ev.step_id for ev in events] == ["s1", "d1", "o1"]


def test_get_events_by_trace_id_returns_empty_for_unknown_trace() -> None:
    client = LedgerClient()
    assert client.get_events_by_trace_id(str(uuid4())) == []


def test_get_events_by_trace_id_isolates_traces() -> None:
    """Events from different traces do not appear in each other's results."""
    client = LedgerClient()
    t1, t2 = uuid4(), uuid4()
    d = uuid4()
    f = uuid4()
    client.append(_make_event(t1, d, f, StepType.SIGNAL, "s1"))
    client.append(_make_event(t2, d, f, StepType.SIGNAL, "s2"))

    assert len(client.get_events_by_trace_id(str(t1))) == 1
    assert len(client.get_events_by_trace_id(str(t2))) == 1


# ------------------------------------------------------------------ #
# LedgerProjector.project()                                           #
# ------------------------------------------------------------------ #


def test_project_returns_none_for_unknown_trace() -> None:
    projector = LedgerProjector(LedgerClient())
    assert projector.project(str(uuid4())) is None


def test_project_reconstructs_full_trace() -> None:
    client = LedgerClient()
    trace_id = uuid4()
    signal_id, decision_id = _populate_ledger(client, trace_id)

    projector = LedgerProjector(client)
    trace = projector.project(str(trace_id))

    assert trace is not None
    assert trace.id == trace_id
    assert trace.signal_id == signal_id
    assert trace.decision_id == decision_id
    assert trace.state == RuntimeState.COMPLETED
    assert trace.completed_at is not None


def test_project_reconstructs_evaluated_nodes() -> None:
    client = LedgerClient()
    trace_id = uuid4()
    _populate_ledger(client, trace_id)

    trace = LedgerProjector(client).project(str(trace_id))
    assert trace is not None
    node_ids = [n["node_id"] for n in trace.evaluated_nodes]
    assert "approve" in node_ids


def test_project_builds_decision_result_from_outcome_event() -> None:
    client = LedgerClient()
    trace_id = uuid4()
    signal_id, decision_id = _populate_ledger(client, trace_id)

    trace = LedgerProjector(client).project(str(trace_id))
    assert trace is not None
    assert len(trace.decision_results) == 1
    result = trace.decision_results[0]
    assert result.id == decision_id
    assert result.status == DecisionStatus.CONFIRMED
    assert result.outcome == DecisionOutcome.PASS
    assert result.selected_node_id == "approve"
    assert result.confidence == 0.9


def test_project_no_signal_event_still_returns_trace() -> None:
    client = LedgerClient()
    trace_id = uuid4()
    decision_id = uuid4()
    flow_id = uuid4()

    client.append(_make_event(
        trace_id, decision_id, flow_id,
        StepType.OUTCOME, str(decision_id),
        payload={
            "decision_id": str(decision_id),
            "status": "confirmed",
            "outcome": "pass",
            "selected_node_id": "node1",
            "confidence": 0.7,
        },
    ))

    trace = LedgerProjector(client).project(str(trace_id))
    assert trace is not None
    assert trace.signal_id is None
    assert trace.state == RuntimeState.COMPLETED


def test_project_no_outcome_event_marks_state_evaluating() -> None:
    client = LedgerClient()
    trace_id = uuid4()
    decision_id = uuid4()
    flow_id = uuid4()

    client.append(_make_event(
        trace_id, decision_id, flow_id,
        StepType.SIGNAL, str(uuid4()),
    ))
    client.append(_make_event(
        trace_id, decision_id, flow_id,
        StepType.DECISION, "node1",
        payload={
            "node_id": "node1", "node_type": "decision",
            "matched": False, "condition": "", "reason": "not matched",
        },
    ))

    trace = LedgerProjector(client).project(str(trace_id))
    assert trace is not None
    assert trace.state == RuntimeState.EVALUATING
    assert trace.completed_at is None
    assert trace.decision_results == []


def test_project_does_not_mutate_ledger() -> None:
    client = LedgerClient()
    trace_id = uuid4()
    _populate_ledger(client, trace_id)

    event_count_before = len(client.get_events())
    LedgerProjector(client).project(str(trace_id))
    assert len(client.get_events()) == event_count_before


def test_project_metadata_marks_projection_source() -> None:
    client = LedgerClient()
    trace_id = uuid4()
    _populate_ledger(client, trace_id)

    trace = LedgerProjector(client).project(str(trace_id))
    assert trace is not None
    assert trace.metadata.get("projected_from_ledger") is True


# ------------------------------------------------------------------ #
# Route integration: GET /traces/{trace_id}                           #
# ------------------------------------------------------------------ #


def test_get_trace_returns_from_trace_store_when_cached(
    test_client: TestClient,
) -> None:
    """TraceStore hit → trace served without touching the projector."""
    resp = test_client.post(
        "/api/runtime/evaluate",
        json={
            "flow_id": "always_confirmed",
            "signal": {
                "type": "test_event",
                "confidence": 0.9,
                "payload": {},
                "source": "test",
            },
        },
    )
    assert resp.status_code == 200
    trace_id = resp.json()["trace_id"]

    resp2 = test_client.get(f"/api/runtime/traces/{trace_id}")
    assert resp2.status_code == 200
    assert resp2.json()["id"] == trace_id


def test_get_trace_returns_404_when_not_found(test_client: TestClient) -> None:
    resp = test_client.get(f"/api/runtime/traces/{uuid4()}")
    assert resp.status_code == 404


def test_get_trace_ledger_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """TraceStore cold + LedgerProjector has trace → 200 with projected trace."""
    import os
    from fastapi.testclient import TestClient
    from backend.app.config import settings
    from backend.app.main import app

    _flows_dir = os.path.join(os.path.dirname(__file__), "flows")
    monkeypatch.setattr(settings, "flow_dir", _flows_dir)
    monkeypatch.setattr(settings, "ledger_enabled", True)

    with TestClient(app) as client:
        # Commit a trace to the ledger via evaluate.
        resp = client.post(
            "/api/runtime/evaluate",
            json={
                "flow_id": "always_confirmed",
                "signal": {
                    "type": "test_event",
                    "confidence": 0.9,
                    "payload": {},
                    "source": "test",
                },
            },
        )
        assert resp.status_code == 200
        trace_id = resp.json()["trace_id"]

        # Clear the TraceStore so only the ledger has it.
        from backend.app.runtime.trace_store import TraceStore
        app.state.trace_store = TraceStore()

        resp2 = client.get(f"/api/runtime/traces/{trace_id}")
        assert resp2.status_code == 200
        body = resp2.json()
        assert body["id"] == trace_id
        assert body["metadata"].get("projected_from_ledger") is True
