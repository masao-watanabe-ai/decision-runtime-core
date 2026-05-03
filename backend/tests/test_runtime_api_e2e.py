"""End-to-end tests for the Decision Runtime Core API.

Each test receives a fresh TestClient (see conftest.py) backed by:
  - always_confirmed flow  — decision node matches all signals; no boundary
  - escalate_flow          — decision always matches; boundary escalates when
                             payload.should_escalate is true

All app.state objects (EventBus, TraceStore, HumanGateManager,
IdempotencyStore) are reset per test via the lifespan.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

BASE = "/api/runtime"


# ------------------------------------------------------------------ #
# Signal / request factories                                           #
# ------------------------------------------------------------------ #


def _confirmed_body() -> dict:
    return {
        "flow_id": "always_confirmed",
        "signal": {
            "type": "test_signal",
            "confidence": 0.9,
            "payload": {"should_escalate": False},
            "source": "test-source",
        },
    }


def _pending_human_body() -> dict:
    return {
        "flow_id": "escalate_flow",
        "signal": {
            "type": "test_signal",
            "confidence": 0.9,
            "payload": {"should_escalate": True},
            "source": "test-source",
        },
    }


# ------------------------------------------------------------------ #
# System / flow discovery                                              #
# ------------------------------------------------------------------ #


def test_health_endpoint(test_client: TestClient) -> None:
    """GET /health returns 200 and service-ok indicator."""
    resp = test_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_list_flows_endpoint(test_client: TestClient) -> None:
    """GET /api/runtime/flows lists the test flows loaded from the test directory."""
    resp = test_client.get(f"{BASE}/flows")
    assert resp.status_code == 200
    flow_ids = [f["flow_id"] for f in resp.json()]
    assert "always_confirmed" in flow_ids
    assert "escalate_flow" in flow_ids


def test_get_flow_endpoint(test_client: TestClient) -> None:
    """GET /api/runtime/flows/{flow_id} returns the named flow."""
    resp = test_client.get(f"{BASE}/flows/always_confirmed")
    assert resp.status_code == 200
    data = resp.json()
    assert data["flow_id"] == "always_confirmed"
    assert data["version"] == "1.0.0"


# ------------------------------------------------------------------ #
# Evaluate — confirmed path                                            #
# ------------------------------------------------------------------ #


def test_evaluate_confirmed_decision(test_client: TestClient) -> None:
    """POST /evaluate with a matching signal returns status=confirmed and sets execution_id."""
    resp = test_client.post(f"{BASE}/evaluate", json=_confirmed_body())
    assert resp.status_code == 200

    result = resp.json()
    assert result["status"] == "confirmed"
    assert result["execution_id"] is not None
    assert result["human_gate"] is None


def test_evaluate_pending_human_decision(test_client: TestClient) -> None:
    """POST /evaluate against a flow with an escalating boundary returns status=pending_human."""
    resp = test_client.post(f"{BASE}/evaluate", json=_pending_human_body())
    assert resp.status_code == 200

    result = resp.json()
    assert result["status"] == "pending_human"
    assert result["execution_id"] is None
    assert result["human_gate"] is not None


# ------------------------------------------------------------------ #
# Trace & explain                                                      #
# ------------------------------------------------------------------ #


def test_trace_endpoint_after_evaluate(test_client: TestClient) -> None:
    """GET /traces/{trace_id} returns the trace produced by the preceding evaluation."""
    eval_resp = test_client.post(f"{BASE}/evaluate", json=_confirmed_body())
    assert eval_resp.status_code == 200
    trace_id = eval_resp.json()["trace_id"]

    trace_resp = test_client.get(f"{BASE}/traces/{trace_id}")
    assert trace_resp.status_code == 200
    assert trace_resp.json()["id"] == trace_id


def test_explain_endpoint_after_evaluate(test_client: TestClient) -> None:
    """GET /decision/{decision_id}/explain returns a structured explanation."""
    eval_resp = test_client.post(f"{BASE}/evaluate", json=_confirmed_body())
    assert eval_resp.status_code == 200
    decision_id = eval_resp.json()["id"]

    explain_resp = test_client.get(f"{BASE}/decision/{decision_id}/explain")
    assert explain_resp.status_code == 200
    explanation = explain_resp.json()
    assert explanation["decision_id"] == decision_id
    assert explanation["final_status"] == "confirmed"
    assert "matched_conditions" in explanation
    assert "boundary_effects" in explanation


# ------------------------------------------------------------------ #
# Events                                                               #
# ------------------------------------------------------------------ #


def test_events_endpoint_after_confirmed_decision(test_client: TestClient) -> None:
    """GET /events returns a runtime.execution.requested event for each confirmed decision."""
    eval_resp = test_client.post(f"{BASE}/evaluate", json=_confirmed_body())
    assert eval_resp.status_code == 200
    execution_id = eval_resp.json()["execution_id"]

    events_resp = test_client.get(f"{BASE}/events")
    assert events_resp.status_code == 200
    events = events_resp.json()

    exec_events = [
        e for e in events
        if e["event_type"] == "runtime.execution.requested"
    ]
    assert len(exec_events) == 1
    assert exec_events[0]["payload"]["execution_id"] == execution_id


def test_no_execution_event_for_pending_human(test_client: TestClient) -> None:
    """Evaluating a pending_human decision does NOT emit an execution.requested event."""
    resp = test_client.post(f"{BASE}/evaluate", json=_pending_human_body())
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending_human"

    events = test_client.get(f"{BASE}/events").json()
    exec_events = [
        e for e in events
        if e["event_type"] == "runtime.execution.requested"
    ]
    assert len(exec_events) == 0


# ------------------------------------------------------------------ #
# Human gate approve / reject                                          #
# ------------------------------------------------------------------ #


def test_human_gate_approve_endpoint(test_client: TestClient) -> None:
    """Approving a pending gate transitions the decision to confirmed."""
    eval_resp = test_client.post(f"{BASE}/evaluate", json=_pending_human_body())
    assert eval_resp.status_code == 200
    result = eval_resp.json()
    assert result["status"] == "pending_human"
    gate_id = result["human_gate"]["id"]

    approve_resp = test_client.post(
        f"{BASE}/human-gates/{gate_id}/approve",
        json={"actor_id": "supervisor_01", "comment": "Looks good"},
    )
    assert approve_resp.status_code == 200
    approved = approve_resp.json()
    assert approved["status"] == "confirmed"


def test_human_gate_reject_endpoint(test_client: TestClient) -> None:
    """Rejecting a pending gate transitions the decision to rejected and clears action."""
    eval_resp = test_client.post(f"{BASE}/evaluate", json=_pending_human_body())
    assert eval_resp.status_code == 200
    gate_id = eval_resp.json()["human_gate"]["id"]

    reject_resp = test_client.post(
        f"{BASE}/human-gates/{gate_id}/reject",
        json={"actor_id": "supervisor_02", "comment": "Not approved"},
    )
    assert reject_resp.status_code == 200
    rejected = reject_resp.json()
    assert rejected["status"] == "rejected"
    assert rejected["action"] is None


# ------------------------------------------------------------------ #
# Idempotency                                                          #
# ------------------------------------------------------------------ #


def test_idempotency_returns_same_result(test_client: TestClient) -> None:
    """Repeated calls with the same idempotency_key return the same DecisionResult."""
    body = {
        "flow_id": "always_confirmed",
        "signal": {
            "type": "test_signal",
            "confidence": 0.9,
            "payload": {},
            "source": "test-source",
            "idempotency_key": "idem-key-e2e-001",
        },
    }

    r1 = test_client.post(f"{BASE}/evaluate", json=body)
    r2 = test_client.post(f"{BASE}/evaluate", json=body)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]
    assert r1.json()["execution_id"] == r2.json()["execution_id"]


def test_idempotency_does_not_duplicate_events(test_client: TestClient) -> None:
    """A cached (idempotent) response does not emit a second execution.requested event."""
    body = {
        "flow_id": "always_confirmed",
        "signal": {
            "type": "test_signal",
            "confidence": 0.9,
            "payload": {},
            "source": "test-source",
            "idempotency_key": "idem-key-e2e-002",
        },
    }

    test_client.post(f"{BASE}/evaluate", json=body)
    events_after_first = test_client.get(f"{BASE}/events").json()

    test_client.post(f"{BASE}/evaluate", json=body)
    events_after_second = test_client.get(f"{BASE}/events").json()

    assert len(events_after_first) == len(events_after_second)


# ------------------------------------------------------------------ #
# Error handling                                                        #
# ------------------------------------------------------------------ #


def test_flow_not_found_returns_404(test_client: TestClient) -> None:
    """Evaluating against an unknown flow_id returns HTTP 404."""
    resp = test_client.post(
        f"{BASE}/evaluate",
        json={
            "flow_id": "non_existent_flow_xyz",
            "signal": {
                "type": "test_signal",
                "confidence": 0.5,
                "payload": {},
                "source": "test-source",
            },
        },
    )
    assert resp.status_code == 404
    assert "non_existent_flow_xyz" in resp.json()["detail"]
