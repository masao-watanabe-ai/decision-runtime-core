"""
Tests for Auth / RBAC Human Gate (Step 10).

Contract under test:
    1.  auth_enabled=False: approve/reject use body actor_id (existing behaviour).
    2.  auth_enabled=False: missing actor_id in body defaults to "anonymous".
    3.  auth_enabled=True, missing X-Api-Key header → 401.
    4.  auth_enabled=True, unrecognised X-Api-Key → 401.
    5.  auth_enabled=True, key with matching role → 200 approve.
    6.  auth_enabled=True, key with wrong role → 403 approve.
    7.  auth_enabled=True, key with matching role → 200 reject.
    8.  auth_enabled=True, key with wrong role → 403 reject.
    9.  Gate with no required_role passes any actor regardless of auth.
    10. Actor model satisfies expected fields.
    11. HumanGateManager.approve raises HumanGateInsufficientRoleError on role mismatch.
    12. HumanGateManager.reject raises HumanGateInsufficientRoleError on role mismatch.
    13. HumanGateManager.approve succeeds when actor holds required_role.
    14. HumanGateManager.approve succeeds when required_role is None.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.config import settings
from backend.app.main import app
from backend.app.models.actor import Actor
from backend.app.models.decision import DecisionOutcome, DecisionResult, DecisionStatus
from backend.app.models.human_gate import HumanGateStatus
from backend.app.models.runtime import RuntimeState
from backend.app.runtime.human_gate_manager import (
    HumanGateInsufficientRoleError,
    HumanGateManager,
)

_TEST_FLOWS_DIR = os.path.join(os.path.dirname(__file__), "flows")
BASE = "/api/runtime"

_API_KEY = "test-key-supervisor"
_API_KEY_MAP = {
    _API_KEY: {"actor_id": "supervisor_01", "roles": ["supervisor", "reviewer"]},
    "test-key-analyst": {"actor_id": "analyst_01", "roles": ["analyst"]},
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


def _pending_result() -> DecisionResult:
    now = datetime.now(timezone.utc)
    return DecisionResult(
        trace_id=uuid4(),
        flow_id=uuid4(),
        flow_version="1.0.0",
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


# ------------------------------------------------------------------ #
# 10. Actor model                                                      #
# ------------------------------------------------------------------ #


def test_actor_model_fields() -> None:
    actor = Actor(actor_id="user_01", roles=["admin", "reviewer"])
    assert actor.actor_id == "user_01"
    assert "admin" in actor.roles
    assert actor.actor_type == "human"


def test_actor_default_roles_is_empty_list() -> None:
    actor = Actor(actor_id="user_02")
    assert actor.roles == []


# ------------------------------------------------------------------ #
# 11–14. HumanGateManager role validation (unit)                      #
# ------------------------------------------------------------------ #


def test_manager_approve_raises_insufficient_role_on_mismatch() -> None:
    manager = HumanGateManager()
    result = _pending_result()
    gate = manager.create_request(result, required_role="supervisor")

    with pytest.raises(HumanGateInsufficientRoleError, match="supervisor"):
        manager.approve(str(gate.id), "user_01", actor_roles=["analyst"])


def test_manager_reject_raises_insufficient_role_on_mismatch() -> None:
    manager = HumanGateManager()
    result = _pending_result()
    gate = manager.create_request(result, required_role="supervisor")

    with pytest.raises(HumanGateInsufficientRoleError, match="supervisor"):
        manager.reject(str(gate.id), "user_01", actor_roles=["analyst"])


def test_manager_approve_succeeds_when_role_matches() -> None:
    manager = HumanGateManager()
    result = _pending_result()
    gate = manager.create_request(result, required_role="supervisor")

    updated = manager.approve(str(gate.id), "user_01", actor_roles=["supervisor"])
    assert updated.status == DecisionStatus.CONFIRMED


def test_manager_approve_succeeds_when_no_required_role() -> None:
    manager = HumanGateManager()
    result = _pending_result()
    gate = manager.create_request(result)  # no required_role

    updated = manager.approve(str(gate.id), "user_01", actor_roles=None)
    assert updated.status == DecisionStatus.CONFIRMED


def test_manager_approve_succeeds_when_no_required_role_and_no_roles() -> None:
    manager = HumanGateManager()
    result = _pending_result()
    gate = manager.create_request(result)

    updated = manager.approve(str(gate.id), "user_01")  # default actor_roles=None
    assert updated.status == DecisionStatus.CONFIRMED


# ------------------------------------------------------------------ #
# 1–2. auth_enabled=False (existing behaviour)                        #
# ------------------------------------------------------------------ #


def test_auth_disabled_approve_uses_body_actor_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "flow_dir", _TEST_FLOWS_DIR)
    monkeypatch.setattr(settings, "auth_enabled", False)

    with TestClient(app) as client:
        eval_resp = client.post(f"{BASE}/evaluate", json=_pending_human_body())
        assert eval_resp.status_code == 200
        gate_id = eval_resp.json()["human_gate"]["id"]

        resp = client.post(
            f"{BASE}/human-gates/{gate_id}/approve",
            json={"actor_id": "supervisor_01", "comment": "OK"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"


def test_auth_disabled_approve_no_actor_id_defaults_to_anonymous(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "flow_dir", _TEST_FLOWS_DIR)
    monkeypatch.setattr(settings, "auth_enabled", False)

    with TestClient(app) as client:
        eval_resp = client.post(f"{BASE}/evaluate", json=_pending_human_body())
        assert eval_resp.status_code == 200
        gate_id = eval_resp.json()["human_gate"]["id"]

        # actor_id omitted — should not raise, defaults to "anonymous"
        resp = client.post(
            f"{BASE}/human-gates/{gate_id}/approve",
            json={},
        )
        assert resp.status_code == 200


# ------------------------------------------------------------------ #
# 3–4. 401 cases                                                       #
# ------------------------------------------------------------------ #


def test_auth_enabled_approve_missing_key_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "flow_dir", _TEST_FLOWS_DIR)
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "api_key_role_map", _API_KEY_MAP)

    with TestClient(app) as client:
        eval_resp = client.post(f"{BASE}/evaluate", json=_pending_human_body())
        assert eval_resp.status_code == 200
        gate_id = eval_resp.json()["human_gate"]["id"]

        resp = client.post(
            f"{BASE}/human-gates/{gate_id}/approve",
            json={"actor_id": "supervisor_01"},
            # no X-Api-Key header
        )
        assert resp.status_code == 401


def test_auth_enabled_approve_invalid_key_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "flow_dir", _TEST_FLOWS_DIR)
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "api_key_role_map", _API_KEY_MAP)

    with TestClient(app) as client:
        eval_resp = client.post(f"{BASE}/evaluate", json=_pending_human_body())
        assert eval_resp.status_code == 200
        gate_id = eval_resp.json()["human_gate"]["id"]

        resp = client.post(
            f"{BASE}/human-gates/{gate_id}/approve",
            json={},
            headers={"X-Api-Key": "not-a-valid-key"},
        )
        assert resp.status_code == 401


# ------------------------------------------------------------------ #
# 5–8. Role match / mismatch                                          #
# ------------------------------------------------------------------ #


def _gate_with_required_role(client: TestClient, required_role: str) -> str:
    """Evaluate to PENDING_HUMAN, then forcibly set required_role on the gate."""
    eval_resp = client.post(f"{BASE}/evaluate", json=_pending_human_body())
    assert eval_resp.status_code == 200
    result = eval_resp.json()
    gate_id = result["human_gate"]["id"]

    # Patch the stored gate request to require a specific role.
    manager: HumanGateManager = client.app.state.human_gate_manager
    existing = manager._store[gate_id]
    manager._store[gate_id] = existing.model_copy(update={"required_role": required_role})
    return gate_id


def test_auth_enabled_approve_role_match_returns_200(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "flow_dir", _TEST_FLOWS_DIR)
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "api_key_role_map", _API_KEY_MAP)

    with TestClient(app) as client:
        gate_id = _gate_with_required_role(client, "supervisor")

        resp = client.post(
            f"{BASE}/human-gates/{gate_id}/approve",
            json={},
            headers={"X-Api-Key": _API_KEY},  # has role "supervisor"
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"


def test_auth_enabled_approve_role_mismatch_returns_403(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "flow_dir", _TEST_FLOWS_DIR)
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "api_key_role_map", _API_KEY_MAP)

    with TestClient(app) as client:
        gate_id = _gate_with_required_role(client, "supervisor")

        resp = client.post(
            f"{BASE}/human-gates/{gate_id}/approve",
            json={},
            headers={"X-Api-Key": "test-key-analyst"},  # only has role "analyst"
        )
        assert resp.status_code == 403


def test_auth_enabled_reject_role_match_returns_200(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "flow_dir", _TEST_FLOWS_DIR)
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "api_key_role_map", _API_KEY_MAP)

    with TestClient(app) as client:
        gate_id = _gate_with_required_role(client, "supervisor")

        resp = client.post(
            f"{BASE}/human-gates/{gate_id}/reject",
            json={"comment": "Not approved"},
            headers={"X-Api-Key": _API_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"


def test_auth_enabled_reject_role_mismatch_returns_403(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "flow_dir", _TEST_FLOWS_DIR)
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "api_key_role_map", _API_KEY_MAP)

    with TestClient(app) as client:
        gate_id = _gate_with_required_role(client, "supervisor")

        resp = client.post(
            f"{BASE}/human-gates/{gate_id}/reject",
            json={},
            headers={"X-Api-Key": "test-key-analyst"},
        )
        assert resp.status_code == 403


# ------------------------------------------------------------------ #
# 9. Gate with no required_role passes any actor                      #
# ------------------------------------------------------------------ #


def test_no_required_role_gate_passes_any_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "flow_dir", _TEST_FLOWS_DIR)
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "api_key_role_map", _API_KEY_MAP)

    with TestClient(app) as client:
        # escalate_flow creates a gate with required_role=None by default
        eval_resp = client.post(f"{BASE}/evaluate", json=_pending_human_body())
        assert eval_resp.status_code == 200
        gate_id = eval_resp.json()["human_gate"]["id"]

        # analyst key has no "supervisor" role — but gate has no required_role → should pass
        resp = client.post(
            f"{BASE}/human-gates/{gate_id}/approve",
            json={},
            headers={"X-Api-Key": "test-key-analyst"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"
