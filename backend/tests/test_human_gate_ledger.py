"""
Tests for Human Gate Ledger Recording (Step 11).

Contract under test:
    1.  approve success → human LedgerEvent with action="approve" is appended.
    2.  reject success  → human LedgerEvent with action="reject" is appended.
    3.  403 (role mismatch) → no LedgerEvent appended.
    4.  404 (not found)     → no LedgerEvent appended.
    5.  parallel mode: ledger append raises → approve still returns 200.
    6.  strict mode:  ledger append raises → 500 returned.
    7.  payload contains actor_id, actor_roles, required_role, comment.
    8.  approve event_id is deterministic (same gate+action = same UUID).
    9.  approve and reject produce distinct event_ids.
    10. append_human_gate_event returns DUPLICATE on retry (idempotent).
    11. RuntimeLedgerAdapter.append_human_gate_event unit test — ACCEPTED path.
    12. RuntimeLedgerAdapter.append_human_gate_event — trace_id=None → INVALID.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.config import settings
from backend.app.integrations.ledger_client import LedgerAppendStatus, LedgerClient
from backend.app.integrations.runtime_ledger_adapter import RuntimeLedgerAdapter, StepType
from backend.app.main import app
from backend.app.models.decision import DecisionOutcome, DecisionResult, DecisionStatus
from backend.app.models.human_gate import HumanGateRequest
from backend.app.models.runtime import RuntimeState
from backend.app.runtime.human_gate_manager import HumanGateManager

_TEST_FLOWS_DIR = os.path.join(os.path.dirname(__file__), "flows")
BASE = "/api/runtime"

_API_KEY_MAP = {
    "key-supervisor": {"actor_id": "supervisor_01", "roles": ["supervisor"]},
    "key-analyst": {"actor_id": "analyst_01", "roles": ["analyst"]},
}


# ------------------------------------------------------------------ #
# Shared factories                                                     #
# ------------------------------------------------------------------ #


def _pending_human_body() -> dict[str, Any]:
    return {
        "flow_id": "escalate_flow",
        "signal": {
            "type": "test_event",
            "confidence": 0.95,
            "payload": {"should_escalate": True},
            "source": "test",
        },
    }


def _pending_result(flow_version: str = "1.0.0") -> DecisionResult:
    now = datetime.now(timezone.utc)
    return DecisionResult(
        trace_id=uuid4(),
        flow_id=uuid4(),
        flow_version=flow_version,
        selected_node_id="boundary_node",
        source_signal_id=uuid4(),
        state=RuntimeState.CONFIRMED,
        status=DecisionStatus.PENDING_HUMAN,
        outcome=DecisionOutcome.PASS,
        action={"type": "route", "target": "queue"},
        evaluated_at=now,
        created_at=now,
        updated_at=now,
    )


def _gate_for(result: DecisionResult, required_role: str | None = None) -> HumanGateRequest:
    manager = HumanGateManager()
    return manager.create_request(result, required_role=required_role)


# ------------------------------------------------------------------ #
# 11–12. RuntimeLedgerAdapter.append_human_gate_event (unit)         #
# ------------------------------------------------------------------ #


def test_append_human_gate_event_accepted() -> None:
    client = LedgerClient()
    adapter = RuntimeLedgerAdapter(ledger_client=client)
    result = _pending_result()
    gate = _gate_for(result)
    now = datetime.now(timezone.utc)

    append_result = adapter.append_human_gate_event(
        gate_request=gate,
        decision_result=result,
        actor_id="user_01",
        action="approve",
        occurred_at=now,
        actor_roles=["supervisor"],
        comment="Looks good",
    )

    assert append_result.status == LedgerAppendStatus.ACCEPTED
    events = client.get_events()
    assert len(events) == 1
    ev = events[0]
    assert ev.step_type == StepType.HUMAN
    assert ev.step_id == str(gate.id)


def test_append_human_gate_event_trace_id_none_returns_invalid() -> None:
    client = LedgerClient()
    adapter = RuntimeLedgerAdapter(ledger_client=client)
    result = _pending_result()
    gate = _gate_for(result)

    # Manually clear trace_id
    gate = gate.model_copy(update={"trace_id": None})
    result_no_trace = result.model_copy(update={"trace_id": None})

    now = datetime.now(timezone.utc)
    append_result = adapter.append_human_gate_event(
        gate_request=gate,
        decision_result=result_no_trace,
        actor_id="user_01",
        action="approve",
        occurred_at=now,
    )

    assert append_result.status == LedgerAppendStatus.INVALID
    assert client.get_events() == []


# ------------------------------------------------------------------ #
# 8–10. Determinism and idempotency                                   #
# ------------------------------------------------------------------ #


def test_approve_event_id_is_deterministic() -> None:
    client = LedgerClient()
    adapter = RuntimeLedgerAdapter(ledger_client=client)
    result = _pending_result()
    gate = _gate_for(result)
    now = datetime.now(timezone.utc)

    r1 = adapter.append_human_gate_event(
        gate_request=gate, decision_result=result,
        actor_id="u1", action="approve", occurred_at=now,
    )
    r2 = adapter.append_human_gate_event(
        gate_request=gate, decision_result=result,
        actor_id="u1", action="approve", occurred_at=now,
    )

    assert r1.event_id == r2.event_id
    assert r1.status == LedgerAppendStatus.ACCEPTED
    assert r2.status == LedgerAppendStatus.DUPLICATE
    assert len(client.get_events()) == 1


def test_approve_and_reject_produce_distinct_event_ids() -> None:
    client = LedgerClient()
    adapter = RuntimeLedgerAdapter(ledger_client=client)
    result = _pending_result()
    gate = _gate_for(result)
    now = datetime.now(timezone.utc)

    r_approve = adapter.append_human_gate_event(
        gate_request=gate, decision_result=result,
        actor_id="u1", action="approve", occurred_at=now,
    )
    r_reject = adapter.append_human_gate_event(
        gate_request=gate, decision_result=result,
        actor_id="u1", action="reject", occurred_at=now,
    )

    assert r_approve.event_id != r_reject.event_id
    assert len(client.get_events()) == 2


# ------------------------------------------------------------------ #
# 1–2. HTTP approve/reject appends event (integration)               #
# ------------------------------------------------------------------ #


def _evaluate_to_gate(client: TestClient) -> str:
    """Evaluate to PENDING_HUMAN and return the gate_id."""
    resp = client.post(f"{BASE}/evaluate", json=_pending_human_body())
    assert resp.status_code == 200
    return resp.json()["human_gate"]["id"]


def _ledger_human_events(client: TestClient) -> list[dict]:
    """Return all LedgerEvents with step_type=human from app.state.ledger_client."""
    return [
        e for e in client.app.state.ledger_client.get_events()
        if e.step_type == StepType.HUMAN
    ]


def test_approve_appends_human_ledger_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "flow_dir", _TEST_FLOWS_DIR)
    monkeypatch.setattr(settings, "ledger_enabled", True)
    monkeypatch.setattr(settings, "ledger_mode", "parallel")

    with TestClient(app) as client:
        gate_id = _evaluate_to_gate(client)
        human_events_before = len(_ledger_human_events(client))

        resp = client.post(
            f"{BASE}/human-gates/{gate_id}/approve",
            json={"actor_id": "supervisor_01", "comment": "Approved"},
        )
        assert resp.status_code == 200

        new_events = _ledger_human_events(client)
        action_events = [
            e for e in new_events
            if e.payload.get("action") == "approve"
        ]
        assert len(action_events) == len(new_events) - human_events_before
        assert len(action_events) == 1
        ev = action_events[0]
        assert ev.payload["actor_id"] == "supervisor_01"
        assert ev.payload["action"] == "approve"


def test_reject_appends_human_ledger_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "flow_dir", _TEST_FLOWS_DIR)
    monkeypatch.setattr(settings, "ledger_enabled", True)
    monkeypatch.setattr(settings, "ledger_mode", "parallel")

    with TestClient(app) as client:
        gate_id = _evaluate_to_gate(client)

        resp = client.post(
            f"{BASE}/human-gates/{gate_id}/reject",
            json={"actor_id": "supervisor_02", "comment": "Not approved"},
        )
        assert resp.status_code == 200

        action_events = [
            e for e in _ledger_human_events(client)
            if e.payload.get("action") == "reject"
        ]
        assert len(action_events) == 1
        assert action_events[0].payload["actor_id"] == "supervisor_02"
        assert action_events[0].payload["comment"] == "Not approved"


# ------------------------------------------------------------------ #
# 7. Payload completeness                                             #
# ------------------------------------------------------------------ #


def test_approve_payload_contains_required_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "flow_dir", _TEST_FLOWS_DIR)
    monkeypatch.setattr(settings, "ledger_enabled", True)
    monkeypatch.setattr(settings, "ledger_mode", "parallel")
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "api_key_role_map", _API_KEY_MAP)

    with TestClient(app) as client:
        gate_id = _evaluate_to_gate(client)

        # Patch required_role onto the stored gate for this test
        manager: HumanGateManager = client.app.state.human_gate_manager
        existing = manager._store[gate_id]
        manager._store[gate_id] = existing.model_copy(update={"required_role": "supervisor"})

        resp = client.post(
            f"{BASE}/human-gates/{gate_id}/approve",
            json={"comment": "LGTM"},
            headers={"X-Api-Key": "key-supervisor"},
        )
        assert resp.status_code == 200

        action_events = [
            e for e in _ledger_human_events(client)
            if e.payload.get("action") == "approve"
        ]
        assert len(action_events) == 1
        payload = action_events[0].payload
        assert payload["actor_id"] == "supervisor_01"
        assert "supervisor" in payload["actor_roles"]
        assert payload["required_role"] == "supervisor"
        assert payload["comment"] == "LGTM"
        assert payload["event_type"] == "runtime.human_gate.approved"
        assert "approved_at" in payload


# ------------------------------------------------------------------ #
# 3–4. Error paths do not write to ledger                            #
# ------------------------------------------------------------------ #


def test_role_mismatch_does_not_append_ledger_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "flow_dir", _TEST_FLOWS_DIR)
    monkeypatch.setattr(settings, "ledger_enabled", True)
    monkeypatch.setattr(settings, "ledger_mode", "parallel")
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "api_key_role_map", _API_KEY_MAP)

    with TestClient(app) as client:
        gate_id = _evaluate_to_gate(client)
        manager: HumanGateManager = client.app.state.human_gate_manager
        existing = manager._store[gate_id]
        manager._store[gate_id] = existing.model_copy(update={"required_role": "supervisor"})

        events_before = len(_ledger_human_events(client))

        resp = client.post(
            f"{BASE}/human-gates/{gate_id}/approve",
            json={},
            headers={"X-Api-Key": "key-analyst"},  # analyst lacks supervisor role
        )
        assert resp.status_code == 403
        assert len(_ledger_human_events(client)) == events_before


def test_not_found_does_not_append_ledger_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "flow_dir", _TEST_FLOWS_DIR)
    monkeypatch.setattr(settings, "ledger_enabled", True)
    monkeypatch.setattr(settings, "ledger_mode", "parallel")

    with TestClient(app) as client:
        events_before = len(_ledger_human_events(client))

        resp = client.post(
            f"{BASE}/human-gates/nonexistent-id/approve",
            json={"actor_id": "someone"},
        )
        assert resp.status_code == 404
        assert len(_ledger_human_events(client)) == events_before


# ------------------------------------------------------------------ #
# 5–6. Parallel vs strict mode on ledger append failure              #
# ------------------------------------------------------------------ #


def test_parallel_mode_ledger_failure_does_not_block_approve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "flow_dir", _TEST_FLOWS_DIR)
    monkeypatch.setattr(settings, "ledger_enabled", True)
    monkeypatch.setattr(settings, "ledger_mode", "parallel")

    with TestClient(app) as client:
        gate_id = _evaluate_to_gate(client)

        # Replace ledger_adapter with a mock that raises on append_human_gate_event
        mock_adapter = MagicMock()
        mock_adapter.append_human_gate_event.side_effect = RuntimeError("ledger down")
        client.app.state.ledger_adapter = mock_adapter

        resp = client.post(
            f"{BASE}/human-gates/{gate_id}/approve",
            json={"actor_id": "supervisor_01"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"


def test_strict_mode_ledger_failure_returns_500(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "flow_dir", _TEST_FLOWS_DIR)
    monkeypatch.setattr(settings, "ledger_enabled", True)
    monkeypatch.setattr(settings, "ledger_mode", "strict")

    with TestClient(app) as client:
        gate_id = _evaluate_to_gate(client)

        mock_adapter = MagicMock()
        mock_adapter.append_human_gate_event.side_effect = RuntimeError("ledger down")
        client.app.state.ledger_adapter = mock_adapter

        resp = client.post(
            f"{BASE}/human-gates/{gate_id}/approve",
            json={"actor_id": "supervisor_01"},
        )
        assert resp.status_code == 500
