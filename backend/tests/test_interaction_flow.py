"""Tests for interaction_default_flow.yaml.

Validates that Interaction Core signals are correctly evaluated by the Runtime:
  - High-risk keywords + confidence >= 0.7  → escalate / pending_human (Human Gate)
  - Suggested actions + confidence >= 0.8   → create_decision_candidate (Studio)
  - Insights/keywords + medium confidence   → route_to_studio (Studio)
  - confidence < 0.5                        → notify_only (channel)
  - No actionable content                   → log_only (fallback / ledger)
  - Same idempotency_key                    → same result ID (deduplication)
  - source = "interaction-core"             → accepted by evaluate endpoint

Signal ≠ Decision principle:
  Suggested actions from Interaction Core are treated as Decision Candidates only.
  High-risk signals always route to Human Gate before any execution.
  Interaction Core does not make decisions; Runtime Core is the sole Decision point.
"""
from __future__ import annotations

import os
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.app.config import settings
from backend.app.main import app
from backend.app.models.decision import DecisionStatus
from backend.app.models.flow import DecisionFlow
from backend.app.models.signal import Signal, SignalValueType
from backend.app.registry.flow_registry import FlowRegistry
from backend.app.runtime.engine import DecisionRuntimeEngine

_PROD_FLOWS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "flows")
)
_FLOW_ID = "interaction_default_flow"


# ------------------------------------------------------------------ #
# Fixtures                                                             #
# ------------------------------------------------------------------ #


@pytest.fixture(scope="module")
def interaction_flow() -> DecisionFlow:
    """Load interaction_default_flow from the production flows directory."""
    registry = FlowRegistry(_PROD_FLOWS_DIR)
    registry.load_all()
    return registry.get(_FLOW_ID)


@pytest.fixture
def interaction_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient pointed at the production flows directory."""
    monkeypatch.setattr(settings, "flow_dir", _PROD_FLOWS_DIR)
    with TestClient(app) as client:
        yield client


# ------------------------------------------------------------------ #
# Signal factory                                                       #
# ------------------------------------------------------------------ #


def _signal(
    confidence: float,
    keywords: list[str] | None = None,
    insights: list[str] | None = None,
    suggested_actions: list[str] | None = None,
    summary: str = "",
    source: str = "interaction-core",
    idempotency_key: str | None = None,
) -> Signal:
    payload: dict[str, Any] = {
        "channel_id": "ch_test",
        "message_id": "msg_test",
        "summary": summary,
        "keywords": keywords if keywords is not None else [],
        "insights": insights if insights is not None else [],
        "suggested_actions": suggested_actions if suggested_actions is not None else [],
    }
    return Signal(
        name="interaction_analysis",
        value_type=SignalValueType.JSON,
        type="interaction_analysis",
        confidence=confidence,
        payload=payload,
        source=source,
        idempotency_key=idempotency_key,
    )


def _evaluate_body(
    confidence: float,
    keywords: list[str] | None = None,
    insights: list[str] | None = None,
    suggested_actions: list[str] | None = None,
    summary: str = "",
    source: str = "interaction-core",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "channel_id": "ch_test",
        "message_id": "msg_test",
        "summary": summary,
        "keywords": keywords if keywords is not None else [],
        "insights": insights if insights is not None else [],
        "suggested_actions": suggested_actions if suggested_actions is not None else [],
    }
    sig: dict[str, Any] = {
        "type": "interaction_analysis",
        "confidence": confidence,
        "payload": payload,
        "source": source,
    }
    if idempotency_key is not None:
        sig["idempotency_key"] = idempotency_key
    return {"flow_id": _FLOW_ID, "signal": sig}


# ------------------------------------------------------------------ #
# Case A — High-risk keyword + confidence >= 0.7 → escalate / human  #
# ------------------------------------------------------------------ #


def test_high_risk_keyword_escalates_to_human_gate(
    interaction_flow: DecisionFlow,
) -> None:
    """High-risk keyword ('legal') + confidence 0.87 → PENDING_HUMAN via escalate boundary."""
    signal = _signal(
        confidence=0.87,
        keywords=["refund", "legal"],
        insights=["Possible escalation case"],
        suggested_actions=["Escalate to support manager"],
        summary="Customer requests refund and mentions legal risk.",
    )
    result = DecisionRuntimeEngine().evaluate(signal, interaction_flow)

    assert result.status == DecisionStatus.PENDING_HUMAN
    assert result.action is not None
    assert result.action["type"] == "escalate"
    assert result.action["target"] == "human_review"
    assert result.action["parameters"]["reason"] == "high_risk_interaction_signal"


def test_high_risk_at_boundary_confidence_escalates(
    interaction_flow: DecisionFlow,
) -> None:
    """Exactly at the 0.7 confidence threshold with risk keywords → still PENDING_HUMAN."""
    signal = _signal(
        confidence=0.70,
        keywords=["compliance"],
    )
    result = DecisionRuntimeEngine().evaluate(signal, interaction_flow)

    assert result.status == DecisionStatus.PENDING_HUMAN
    assert result.action["type"] == "escalate"


def test_all_high_risk_keywords_trigger_escalation(
    interaction_flow: DecisionFlow,
) -> None:
    """Each individual high-risk keyword triggers escalation at confidence >= 0.7."""
    risk_keywords = ["legal", "contract", "security", "incident", "refund", "compliance"]
    for keyword in risk_keywords:
        signal = _signal(confidence=0.75, keywords=[keyword])
        result = DecisionRuntimeEngine().evaluate(signal, interaction_flow)
        assert result.status == DecisionStatus.PENDING_HUMAN, (
            f"Expected PENDING_HUMAN for keyword='{keyword}', got {result.status}"
        )


# ------------------------------------------------------------------ #
# Case B — Suggested actions + confidence >= 0.8 → candidate          #
# ------------------------------------------------------------------ #


def test_high_confidence_action_creates_decision_candidate(
    interaction_flow: DecisionFlow,
) -> None:
    """suggested_actions present + confidence 0.85 + no risk keywords → create_decision_candidate."""
    signal = _signal(
        confidence=0.85,
        keywords=["onboarding"],
        insights=["User interested in premium plan"],
        suggested_actions=["Send premium upgrade offer"],
    )
    result = DecisionRuntimeEngine().evaluate(signal, interaction_flow)

    assert result.status == DecisionStatus.CONFIRMED
    assert result.action is not None
    assert result.action["type"] == "create_decision_candidate"
    assert result.action["target"] == "decision_trace_studio"
    assert result.action["parameters"]["reason"] == "high_confidence_suggested_action"
    assert result.selected_node_id == "high_confidence_action"


def test_high_confidence_candidate_is_not_immediate_execution(
    interaction_flow: DecisionFlow,
) -> None:
    """Candidate decision must not be an execution-type action (Signal ≠ Decision principle)."""
    signal = _signal(
        confidence=0.90,
        suggested_actions=["Schedule follow-up call"],
    )
    result = DecisionRuntimeEngine().evaluate(signal, interaction_flow)

    # action.type must not be any direct execution variant
    assert result.action is not None
    assert result.action["type"] not in {"execute", "run", "trigger", "dispatch"}
    assert result.action["type"] == "create_decision_candidate"


# ------------------------------------------------------------------ #
# Case C — Insights/keywords + medium confidence → route to Studio    #
# ------------------------------------------------------------------ #


def test_needs_structuring_routes_to_decision_trace_studio(
    interaction_flow: DecisionFlow,
) -> None:
    """Insights + keywords + confidence 0.6 → route_to_studio at decision_trace_studio."""
    signal = _signal(
        confidence=0.60,
        keywords=["pricing"],
        insights=["Customer comparing plans"],
    )
    result = DecisionRuntimeEngine().evaluate(signal, interaction_flow)

    assert result.status == DecisionStatus.CONFIRMED
    assert result.action is not None
    assert result.action["type"] == "route_to_studio"
    assert result.action["target"] == "decision_trace_studio"
    assert result.selected_node_id == "needs_structuring"


def test_needs_structuring_with_only_insights(
    interaction_flow: DecisionFlow,
) -> None:
    """Insights only (no keywords, no suggested_actions) + confidence 0.65 → route_to_studio."""
    signal = _signal(
        confidence=0.65,
        insights=["User sentiment negative"],
    )
    result = DecisionRuntimeEngine().evaluate(signal, interaction_flow)

    assert result.status == DecisionStatus.CONFIRMED
    assert result.action["type"] == "route_to_studio"


# ------------------------------------------------------------------ #
# Case D — Low confidence → notify_only                               #
# ------------------------------------------------------------------ #


def test_low_confidence_notify_only(interaction_flow: DecisionFlow) -> None:
    """confidence < 0.5 → notify_only to channel, no decision made."""
    signal = _signal(
        confidence=0.30,
        keywords=["weather"],
        insights=["General inquiry"],
    )
    result = DecisionRuntimeEngine().evaluate(signal, interaction_flow)

    assert result.status == DecisionStatus.CONFIRMED
    assert result.action is not None
    assert result.action["type"] == "notify_only"
    assert result.action["target"] == "channel"
    assert result.selected_node_id == "low_confidence"


def test_low_confidence_does_not_escalate_even_with_risk_keywords(
    interaction_flow: DecisionFlow,
) -> None:
    """Risk keywords + confidence 0.4 (< 0.7 boundary threshold) → notify_only, not escalate."""
    signal = _signal(
        confidence=0.40,
        keywords=["legal", "refund"],
    )
    result = DecisionRuntimeEngine().evaluate(signal, interaction_flow)

    assert result.status == DecisionStatus.CONFIRMED
    assert result.action["type"] == "notify_only"


# ------------------------------------------------------------------ #
# Case E — Fallback → log_only                                        #
# ------------------------------------------------------------------ #


def test_fallback_logs_unactionable_signal(interaction_flow: DecisionFlow) -> None:
    """No keywords, no insights, no suggested_actions + confidence 0.7 → fallback log_only."""
    signal = _signal(
        confidence=0.70,
        keywords=[],
        insights=[],
        suggested_actions=[],
    )
    result = DecisionRuntimeEngine().evaluate(signal, interaction_flow)

    assert result.status == DecisionStatus.FALLBACK
    assert result.action is not None
    assert result.action["type"] == "log_only"
    assert result.action["target"] == "ledger"


def test_fallback_when_no_nodes_match_medium_confidence(
    interaction_flow: DecisionFlow,
) -> None:
    """Empty payload + confidence 0.6 (not low enough for notify_only, no content for studio) → fallback."""
    signal = _signal(confidence=0.60)
    result = DecisionRuntimeEngine().evaluate(signal, interaction_flow)

    assert result.status == DecisionStatus.FALLBACK
    assert result.action["type"] == "log_only"


# ------------------------------------------------------------------ #
# Case 6 — Idempotency                                                 #
# ------------------------------------------------------------------ #


def test_idempotency_deduplicates_same_key(
    interaction_client: TestClient,
) -> None:
    """Two evaluate calls with the same idempotency_key return the same decision ID."""
    body = _evaluate_body(
        confidence=0.30,
        keywords=[],
        insights=[],
        suggested_actions=[],
        idempotency_key="interaction:ch_001:msg_idempotent_01:v1",
    )
    r1 = interaction_client.post("/api/runtime/evaluate", json=body)
    r2 = interaction_client.post("/api/runtime/evaluate", json=body)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"], (
        "Second call with same idempotency_key must return the cached decision ID"
    )


# ------------------------------------------------------------------ #
# Case 7 — source = "interaction-core" is accepted                    #
# ------------------------------------------------------------------ #


def test_interaction_core_source_accepted_by_evaluate(
    interaction_client: TestClient,
) -> None:
    """source='interaction-core' is accepted and high-risk payload routes to PENDING_HUMAN."""
    body = _evaluate_body(
        confidence=0.87,
        keywords=["refund", "legal"],
        insights=["Possible escalation case"],
        suggested_actions=["Escalate to support manager"],
        summary="Customer requests refund and mentions legal risk.",
        source="interaction-core",
        idempotency_key="interaction:ch_001:msg_001:v_test7",
    )
    resp = interaction_client.post("/api/runtime/evaluate", json=body)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending_human"
    assert data["action"]["type"] == "escalate"
    assert data["action"]["target"] == "human_review"


# ------------------------------------------------------------------ #
# Flow registry — GET endpoints                                        #
# ------------------------------------------------------------------ #


def test_flow_listed_in_registry(interaction_client: TestClient) -> None:
    """GET /api/runtime/flows lists interaction_default_flow."""
    resp = interaction_client.get("/api/runtime/flows")
    assert resp.status_code == 200
    flow_ids = [f["flow_id"] for f in resp.json()]
    assert _FLOW_ID in flow_ids


def test_get_flow_by_id(interaction_client: TestClient) -> None:
    """GET /api/runtime/flows/interaction_default_flow returns the correct flow."""
    resp = interaction_client.get(f"/api/runtime/flows/{_FLOW_ID}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["flow_id"] == _FLOW_ID
    assert data["version"] == "1.0.0"
