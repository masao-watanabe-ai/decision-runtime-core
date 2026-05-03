"""
Integration tests for RuntimeLedgerAdapter injection via FastAPI lifespan.

Covers:
  - ledger_enabled=false  → app.state.ledger_adapter is None, no events appended
  - ledger_enabled=true   → app.state.ledger_adapter is RuntimeLedgerAdapter
  - evaluate pipeline with ledger enabled → LedgerEvents written to client
  - parallel mode: broken ledger → 200 OK, correct DecisionResult returned
  - response format unchanged regardless of ledger state
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.app.config import settings
from backend.app.integrations.runtime_ledger_adapter import RuntimeLedgerAdapter, StepType
from backend.app.main import app

_TEST_FLOWS_DIR = os.path.join(os.path.dirname(__file__), "flows")

# ------------------------------------------------------------------ #
# Fixtures                                                             #
# ------------------------------------------------------------------ #


@pytest.fixture
def test_client_with_ledger(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with ledger_enabled=True and test flows loaded."""
    monkeypatch.setattr(settings, "flow_dir", _TEST_FLOWS_DIR)
    monkeypatch.setattr(settings, "ledger_enabled", True)
    monkeypatch.setattr(settings, "ledger_mode", "parallel")
    with TestClient(app) as client:
        yield client


@pytest.fixture
def test_client_ledger_disabled(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with ledger_enabled=False (default) and test flows loaded."""
    monkeypatch.setattr(settings, "flow_dir", _TEST_FLOWS_DIR)
    monkeypatch.setattr(settings, "ledger_enabled", False)
    with TestClient(app) as client:
        yield client


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

_CONFIRMED_BODY = {
    "flow_id": "always_confirmed",
    "signal": {
        "type": "test_signal",
        "confidence": 0.9,
        "payload": {"should_escalate": False},
        "source": "test-source",
    },
}

# ------------------------------------------------------------------ #
# Lifecycle state tests                                                #
# ------------------------------------------------------------------ #


def test_ledger_disabled_state_is_none(
    test_client_ledger_disabled: TestClient,
) -> None:
    """When ledger_enabled=False, both ledger_client and ledger_adapter are None."""
    state = test_client_ledger_disabled.app.state
    assert state.ledger_client is None
    assert state.ledger_adapter is None


def test_ledger_enabled_state_has_adapter(
    test_client_with_ledger: TestClient,
) -> None:
    """When ledger_enabled=True, app.state.ledger_adapter is a RuntimeLedgerAdapter."""
    state = test_client_with_ledger.app.state
    assert isinstance(state.ledger_adapter, RuntimeLedgerAdapter)
    assert state.ledger_client is not None


def test_ledger_adapter_uses_injected_client(
    test_client_with_ledger: TestClient,
) -> None:
    """The adapter's internal client is the same object stored in app.state.ledger_client."""
    state = test_client_with_ledger.app.state
    # The adapter's _client field must reference the same LedgerClient singleton.
    assert state.ledger_adapter._client is state.ledger_client


# ------------------------------------------------------------------ #
# Evaluate pipeline integration                                        #
# ------------------------------------------------------------------ #


def test_evaluate_with_ledger_enabled_appends_events(
    test_client_with_ledger: TestClient,
) -> None:
    """POST /evaluate with ledger_enabled=True writes LedgerEvents to the client."""
    resp = test_client_with_ledger.post("/api/runtime/evaluate", json=_CONFIRMED_BODY)
    assert resp.status_code == 200

    events = test_client_with_ledger.app.state.ledger_client.get_events()
    assert len(events) > 0


def test_evaluate_with_ledger_enabled_contains_decision_and_outcome_steps(
    test_client_with_ledger: TestClient,
) -> None:
    """evaluate() pipeline produces at least SIGNAL, DECISION, and OUTCOME LedgerEvents."""
    test_client_with_ledger.post("/api/runtime/evaluate", json=_CONFIRMED_BODY)

    step_types = {
        e.step_type for e in test_client_with_ledger.app.state.ledger_client.get_events()
    }
    assert StepType.SIGNAL in step_types
    assert StepType.DECISION in step_types
    assert StepType.OUTCOME in step_types


def test_evaluate_with_ledger_disabled_no_events(
    test_client_ledger_disabled: TestClient,
) -> None:
    """POST /evaluate with ledger_enabled=False appends nothing to the ledger."""
    resp = test_client_ledger_disabled.post("/api/runtime/evaluate", json=_CONFIRMED_BODY)
    assert resp.status_code == 200

    # ledger_client is None when disabled — no events to inspect.
    assert test_client_ledger_disabled.app.state.ledger_client is None


# ------------------------------------------------------------------ #
# Parallel mode resilience                                             #
# ------------------------------------------------------------------ #


def test_parallel_mode_broken_ledger_returns_200(
    test_client_with_ledger: TestClient,
) -> None:
    """In parallel mode, a ledger failure must not affect the API response."""
    # Break the ledger client after startup so the adapter's commit() raises.
    test_client_with_ledger.app.state.ledger_client.append = MagicMock(
        side_effect=RuntimeError("ledger offline")
    )

    resp = test_client_with_ledger.post("/api/runtime/evaluate", json=_CONFIRMED_BODY)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "confirmed"
    assert data["outcome"] == "pass"


def test_parallel_mode_broken_ledger_decision_result_complete(
    test_client_with_ledger: TestClient,
) -> None:
    """The DecisionResult fields are fully populated even when the ledger is offline."""
    test_client_with_ledger.app.state.ledger_client.append = MagicMock(
        side_effect=RuntimeError("ledger offline")
    )

    resp = test_client_with_ledger.post("/api/runtime/evaluate", json=_CONFIRMED_BODY)

    data = resp.json()
    assert data["selected_node_id"] == "match_node"
    assert data["confidence"] == pytest.approx(0.9)
    assert data["trace_id"] is not None
    assert data["id"] is not None


# ------------------------------------------------------------------ #
# Response format parity                                               #
# ------------------------------------------------------------------ #


def test_response_format_identical_with_and_without_ledger(
    test_client_with_ledger: TestClient,
    test_client_ledger_disabled: TestClient,
) -> None:
    """The API response schema is identical regardless of ledger state."""
    resp_ledger = test_client_with_ledger.post("/api/runtime/evaluate", json=_CONFIRMED_BODY)
    resp_no_ledger = test_client_ledger_disabled.post("/api/runtime/evaluate", json=_CONFIRMED_BODY)

    assert resp_ledger.status_code == resp_no_ledger.status_code == 200

    d_ledger = resp_ledger.json()
    d_no_ledger = resp_no_ledger.json()

    # Schema-level keys must be identical (UUIDs and timestamps will differ).
    assert set(d_ledger.keys()) == set(d_no_ledger.keys())

    # Routing decision must be identical.
    for field in ("status", "outcome", "selected_node_id", "flow_version"):
        assert d_ledger[field] == d_no_ledger[field], f"field '{field}' differs"


def test_ledger_events_trace_id_matches_response(
    test_client_with_ledger: TestClient,
) -> None:
    """Every LedgerEvent's trace_id matches the trace_id in the API response."""
    resp = test_client_with_ledger.post("/api/runtime/evaluate", json=_CONFIRMED_BODY)
    assert resp.status_code == 200
    expected_trace_id = resp.json()["trace_id"]

    events = test_client_with_ledger.app.state.ledger_client.get_events()
    for event in events:
        assert str(event.trace_id) == expected_trace_id, (
            f"Event {event.event_id} has trace_id={event.trace_id}, "
            f"expected {expected_trace_id}"
        )


def test_two_evaluations_produce_distinct_trace_ids(
    test_client_with_ledger: TestClient,
) -> None:
    """Each evaluate call writes events under a distinct trace_id."""
    resp1 = test_client_with_ledger.post("/api/runtime/evaluate", json=_CONFIRMED_BODY)
    resp2 = test_client_with_ledger.post("/api/runtime/evaluate", json=_CONFIRMED_BODY)

    tid1 = resp1.json()["trace_id"]
    tid2 = resp2.json()["trace_id"]
    assert tid1 != tid2

    events = test_client_with_ledger.app.state.ledger_client.get_events()
    trace_ids = {str(e.trace_id) for e in events}
    assert tid1 in trace_ids
    assert tid2 in trace_ids
