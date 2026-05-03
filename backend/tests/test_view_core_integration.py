"""View Core integration tests.

Verifies that Decision Runtime Core exposes the three View-support APIs
(GET /traces, POST /compare, POST /simulate) while keeping existing
evaluate behaviour unchanged and never triggering real side effects during
simulation (no Ledger commit, no EventBus publish, no persistent HumanGate).

DTM View Core does not make decisions.
It calls Decision Runtime Core to evaluate, inspect, compare, and simulate
decision traces.  Decision ownership remains inside Runtime:

    Interaction → Signal → Runtime → Boundary → Human → Ledger → View
"""
from __future__ import annotations

from fastapi.testclient import TestClient

BASE = "/api/runtime"


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _eval_confirmed(client: TestClient, confidence: float = 0.9) -> dict:
    resp = client.post(f"{BASE}/evaluate", json={
        "flow_id": "always_confirmed",
        "signal": {
            "type": "test_signal",
            "confidence": confidence,
            "payload": {},
            "source": "test-source",
        },
    })
    assert resp.status_code == 200
    return resp.json()


def _eval_escalate(client: TestClient, should_escalate: bool = False) -> dict:
    resp = client.post(f"{BASE}/evaluate", json={
        "flow_id": "escalate_flow",
        "signal": {
            "type": "test_signal",
            "confidence": 0.9,
            "payload": {"should_escalate": should_escalate},
            "source": "test-source",
        },
    })
    assert resp.status_code == 200
    return resp.json()


# ------------------------------------------------------------------ #
# GET /api/runtime/traces — trace summaries                            #
# ------------------------------------------------------------------ #


def test_list_traces_returns_trace_summaries(test_client: TestClient) -> None:
    """GET /traces returns TraceSummary objects after an evaluate call."""
    result = _eval_confirmed(test_client)

    resp = test_client.get(f"{BASE}/traces")
    assert resp.status_code == 200
    summaries = resp.json()
    assert len(summaries) >= 1

    summary = summaries[0]
    assert "trace_id" in summary
    assert "decision_id" in summary
    assert "flow_id" in summary
    assert "status" in summary
    assert "confidence" in summary
    assert "created_at" in summary

    assert summary["trace_id"] == result["trace_id"]
    assert summary["status"] == "confirmed"
    assert summary["confidence"] == result["confidence"]


def test_list_traces_includes_action_type(test_client: TestClient) -> None:
    """TraceSummary includes the action.type from the selected node."""
    _eval_confirmed(test_client)
    summaries = test_client.get(f"{BASE}/traces").json()
    assert summaries[0]["action_type"] == "route"


def test_list_traces_empty_when_no_evaluations(test_client: TestClient) -> None:
    """GET /traces returns an empty list when no traces exist."""
    resp = test_client.get(f"{BASE}/traces")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_traces_pagination_limit(test_client: TestClient) -> None:
    """limit parameter restricts the number of returned summaries."""
    for _ in range(3):
        _eval_confirmed(test_client)

    resp = test_client.get(f"{BASE}/traces?limit=2")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_list_traces_pagination_offset(test_client: TestClient) -> None:
    """offset parameter skips summaries for pagination."""
    for _ in range(3):
        _eval_confirmed(test_client)

    all_resp = test_client.get(f"{BASE}/traces?limit=3").json()
    page2_resp = test_client.get(f"{BASE}/traces?limit=2&offset=1").json()

    assert len(page2_resp) == 2
    # Second page starts one position later than the full list.
    assert page2_resp[0]["trace_id"] == all_resp[1]["trace_id"]


# ------------------------------------------------------------------ #
# POST /api/runtime/compare — field-level diffs                        #
# ------------------------------------------------------------------ #


def test_compare_returns_field_level_diffs(test_client: TestClient) -> None:
    """POST /compare returns diffs when two traces have different confidence values."""
    r1 = _eval_confirmed(test_client, confidence=0.9)
    r2 = _eval_confirmed(test_client, confidence=0.5)

    resp = test_client.post(f"{BASE}/compare", json={
        "base_trace_id": r1["trace_id"],
        "target_trace_id": r2["trace_id"],
    })
    assert resp.status_code == 200
    data = resp.json()

    assert data["base_trace_id"] == r1["trace_id"]
    assert data["target_trace_id"] == r2["trace_id"]
    assert "diffs" in data

    confidence_diffs = [d for d in data["diffs"] if d["path"] == "decision.confidence"]
    assert len(confidence_diffs) == 1
    assert confidence_diffs[0]["base"] == 0.9
    assert confidence_diffs[0]["target"] == 0.5


def test_compare_no_diffs_for_same_status(test_client: TestClient) -> None:
    """Two traces from the same flow with the same confidence produce no status diff."""
    r1 = _eval_confirmed(test_client, confidence=0.8)
    r2 = _eval_confirmed(test_client, confidence=0.8)

    resp = test_client.post(f"{BASE}/compare", json={
        "base_trace_id": r1["trace_id"],
        "target_trace_id": r2["trace_id"],
    })
    assert resp.status_code == 200
    diffs = resp.json()["diffs"]
    status_diffs = [d for d in diffs if d["path"] == "decision.status"]
    assert len(status_diffs) == 0


def test_compare_detects_status_diff(test_client: TestClient) -> None:
    """POST /compare detects status diff between confirmed and pending_human traces."""
    confirmed = _eval_confirmed(test_client)
    pending = _eval_escalate(test_client, should_escalate=True)

    resp = test_client.post(f"{BASE}/compare", json={
        "base_trace_id": confirmed["trace_id"],
        "target_trace_id": pending["trace_id"],
    })
    assert resp.status_code == 200
    diffs = resp.json()["diffs"]
    status_diffs = [d for d in diffs if d["path"] == "decision.status"]
    assert len(status_diffs) == 1
    assert status_diffs[0]["base"] == "confirmed"
    assert status_diffs[0]["target"] == "pending_human"


def test_compare_returns_404_for_missing_base_trace(test_client: TestClient) -> None:
    """POST /compare returns 404 when the base trace does not exist."""
    resp = test_client.post(f"{BASE}/compare", json={
        "base_trace_id": "00000000-0000-0000-0000-000000000000",
        "target_trace_id": "00000000-0000-0000-0000-000000000001",
    })
    assert resp.status_code == 404
    assert "base" in resp.json()["detail"].lower() or "00000000" in resp.json()["detail"]


def test_compare_returns_404_for_missing_target_trace(test_client: TestClient) -> None:
    """POST /compare returns 404 when the target trace does not exist."""
    r1 = _eval_confirmed(test_client)

    resp = test_client.post(f"{BASE}/compare", json={
        "base_trace_id": r1["trace_id"],
        "target_trace_id": "00000000-0000-0000-0000-000000000099",
    })
    assert resp.status_code == 404
    assert "00000000-0000-0000-0000-000000000099" in resp.json()["detail"]


# ------------------------------------------------------------------ #
# POST /api/runtime/simulate — simulation without side effects         #
# ------------------------------------------------------------------ #


def test_simulate_returns_decision_result(test_client: TestClient) -> None:
    """POST /simulate returns a SimulateResponse with mode=simulation."""
    result = _eval_confirmed(test_client)
    trace_id = result["trace_id"]

    resp = test_client.post(f"{BASE}/simulate", json={"trace_id": trace_id})
    assert resp.status_code == 200
    data = resp.json()

    assert data["mode"] == "simulation"
    assert data["source_trace_id"] == trace_id
    assert data["committed"] is False
    assert data["events_published"] is False
    assert "result" in data
    assert "trace" in data
    assert data["result"]["status"] == "confirmed"


def test_simulate_with_confidence_override(test_client: TestClient) -> None:
    """signal_overrides.confidence is applied to the simulation signal."""
    result = _eval_confirmed(test_client, confidence=0.9)

    resp = test_client.post(f"{BASE}/simulate", json={
        "trace_id": result["trace_id"],
        "signal_overrides": {"confidence": 0.3},
    })
    assert resp.status_code == 200
    assert resp.json()["result"]["confidence"] == 0.3


def test_simulate_with_payload_override(test_client: TestClient) -> None:
    """signal_overrides.payload is merged into the original signal's payload."""
    # Evaluate non-escalating to get a confirmed trace.
    result = _eval_escalate(test_client, should_escalate=False)
    assert result["status"] == "confirmed"

    # Simulate with escalating override.
    resp = test_client.post(f"{BASE}/simulate", json={
        "trace_id": result["trace_id"],
        "signal_overrides": {"payload": {"should_escalate": True}},
    })
    assert resp.status_code == 200
    sim = resp.json()
    # With should_escalate=True the boundary escalates to pending_human.
    assert sim["result"]["status"] == "pending_human"


def test_simulate_does_not_commit_to_ledger(test_client: TestClient) -> None:
    """Simulation does not save the simulated trace to the TraceStore.

    The trace count must remain unchanged after the simulate call.
    """
    result = _eval_confirmed(test_client)

    traces_before = test_client.get(f"{BASE}/traces").json()
    count_before = len(traces_before)

    test_client.post(f"{BASE}/simulate", json={"trace_id": result["trace_id"]})

    traces_after = test_client.get(f"{BASE}/traces").json()
    assert len(traces_after) == count_before


def test_simulate_does_not_publish_event_bus_event(test_client: TestClient) -> None:
    """Simulation does not publish any events to the EventBus."""
    result = _eval_confirmed(test_client)

    events_before = test_client.get(f"{BASE}/events").json()
    count_before = len(events_before)

    test_client.post(f"{BASE}/simulate", json={"trace_id": result["trace_id"]})

    events_after = test_client.get(f"{BASE}/events").json()
    assert len(events_after) == count_before


def test_simulate_does_not_create_persistent_human_gate(test_client: TestClient) -> None:
    """Simulation with an escalating boundary does not persist a HumanGateRequest.

    The result shows pending_human but human_gate is None (not persisted),
    and the HumanGateManager store remains empty.
    """
    # First: confirmed evaluation on escalate_flow (no gate created).
    result = _eval_escalate(test_client, should_escalate=False)
    assert result["status"] == "confirmed"

    # Simulate with escalating payload — would normally create a gate.
    resp = test_client.post(f"{BASE}/simulate", json={
        "trace_id": result["trace_id"],
        "signal_overrides": {"payload": {"should_escalate": True}},
    })
    assert resp.status_code == 200
    sim = resp.json()

    # Simulation correctly shows the pending_human outcome.
    assert sim["result"]["status"] == "pending_human"

    # human_gate must be None — simulation never persists gate requests.
    assert sim["result"]["human_gate"] is None

    # Nothing in the gate manager store.
    assert len(test_client.app.state.human_gate_manager._store) == 0


def test_simulate_reuses_existing_flow_and_original_signal(test_client: TestClient) -> None:
    """Simulation without overrides uses the original flow and signal confidence."""
    result = _eval_confirmed(test_client, confidence=0.7)
    original_flow_id = result["flow_id"]

    resp = test_client.post(f"{BASE}/simulate", json={"trace_id": result["trace_id"]})
    assert resp.status_code == 200
    sim = resp.json()

    # Flow identity must match the source trace.
    assert sim["result"]["flow_id"] == original_flow_id
    # Signal confidence is preserved when no override is given.
    assert sim["result"]["confidence"] == 0.7


def test_simulate_returns_404_for_missing_trace(test_client: TestClient) -> None:
    """POST /simulate returns 404 when the source trace does not exist."""
    resp = test_client.post(f"{BASE}/simulate", json={
        "trace_id": "00000000-0000-0000-0000-000000000000",
    })
    assert resp.status_code == 404


# ------------------------------------------------------------------ #
# Existing evaluate behaviour is unchanged                             #
# ------------------------------------------------------------------ #


def test_existing_evaluate_behavior_unchanged(test_client: TestClient) -> None:
    """POST /evaluate still returns a confirmed DecisionResult with execution_id."""
    resp = test_client.post(f"{BASE}/evaluate", json={
        "flow_id": "always_confirmed",
        "signal": {
            "type": "test_signal",
            "confidence": 0.9,
            "payload": {},
            "source": "test-source",
        },
    })
    assert resp.status_code == 200
    result = resp.json()
    assert result["status"] == "confirmed"
    assert result["execution_id"] is not None
    assert result["human_gate"] is None


def test_existing_evaluate_trace_saved(test_client: TestClient) -> None:
    """POST /evaluate still saves the trace so GET /traces/{id} works."""
    result = _eval_confirmed(test_client)
    trace_resp = test_client.get(f"{BASE}/traces/{result['trace_id']}")
    assert trace_resp.status_code == 200
    assert trace_resp.json()["id"] == result["trace_id"]


def test_existing_evaluate_event_published(test_client: TestClient) -> None:
    """POST /evaluate still publishes an execution.requested event."""
    _eval_confirmed(test_client)
    events = test_client.get(f"{BASE}/events").json()
    exec_events = [e for e in events if e["event_type"] == "runtime.execution.requested"]
    assert len(exec_events) >= 1
