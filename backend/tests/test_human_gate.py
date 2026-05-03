from __future__ import annotations

from uuid import uuid4

import pytest

from backend.app.models.decision import DecisionOutcome, DecisionResult, DecisionStatus
from backend.app.models.human_gate import HumanGateStatus
from backend.app.models.runtime import RuntimeState
from backend.app.runtime.human_gate_manager import (
    HumanGateInvalidStateError,
    HumanGateManager,
    HumanGateNotFoundError,
)


# ------------------------------------------------------------------ #
# Test-object factories                                                #
# ------------------------------------------------------------------ #


def _pending_result(action: dict | None = None) -> DecisionResult:
    return DecisionResult(
        trace_id=uuid4(),
        flow_id=uuid4(),
        flow_version="1.0.0",
        selected_node_id="boundary_node",
        source_signal_id=uuid4(),
        state=RuntimeState.CONFIRMED,
        status=DecisionStatus.PENDING_HUMAN,
        outcome=DecisionOutcome.PASS,
        action=action or {"type": "route", "target": "queue"},
    )


# ------------------------------------------------------------------ #
# Tests                                                                #
# ------------------------------------------------------------------ #


def test_create_human_gate_request() -> None:
    """create_request() returns a HumanGateRequest linked to the DecisionResult."""
    manager = HumanGateManager()
    result = _pending_result()

    gate = manager.create_request(result, required_role="supervisor", reason="High-value escalation")

    assert gate.decision_id == result.id
    assert gate.trace_id == result.trace_id
    assert gate.flow_id == result.flow_id
    assert gate.node_id == result.selected_node_id
    assert gate.required_role == "supervisor"
    assert gate.status == HumanGateStatus.PENDING
    assert len(gate.options) >= 2
    option_values = {o.value for o in gate.options}
    assert "approve" in option_values
    assert "reject" in option_values


def test_approve_transitions_to_confirmed() -> None:
    """approve() transitions the DecisionResult from pending_human to confirmed."""
    manager = HumanGateManager()
    result = _pending_result()
    gate = manager.create_request(result)

    updated = manager.approve(str(gate.id), actor_id="reviewer_1", comment="Looks good")

    assert updated.status == DecisionStatus.CONFIRMED
    assert updated.state == RuntimeState.CONFIRMED
    assert updated.action == result.action  # action preserved on approve

    resolved_gate = manager.get_request(str(gate.id))
    assert resolved_gate.status == HumanGateStatus.APPROVED
    assert resolved_gate.response_value == "approve"
    assert resolved_gate.response_note == "Looks good"
    assert resolved_gate.responded_at is not None


def test_reject_transitions_to_rejected() -> None:
    """reject() transitions the DecisionResult to rejected and clears action."""
    manager = HumanGateManager()
    result = _pending_result()
    gate = manager.create_request(result)

    updated = manager.reject(str(gate.id), actor_id="reviewer_2", comment="Cannot approve")

    assert updated.status == DecisionStatus.REJECTED
    assert updated.state == RuntimeState.REJECTED
    assert updated.action is None  # action cleared on reject

    resolved_gate = manager.get_request(str(gate.id))
    assert resolved_gate.status == HumanGateStatus.REJECTED
    assert resolved_gate.response_value == "reject"
    assert resolved_gate.response_note == "Cannot approve"
    assert resolved_gate.responded_at is not None


def test_unknown_request_id_raises() -> None:
    """approve() and reject() raise HumanGateNotFoundError for unknown IDs."""
    manager = HumanGateManager()
    unknown_id = str(uuid4())

    with pytest.raises(HumanGateNotFoundError):
        manager.approve(unknown_id, actor_id="any")

    with pytest.raises(HumanGateNotFoundError):
        manager.reject(unknown_id, actor_id="any")


def test_double_approve_not_allowed() -> None:
    """Calling approve() twice on the same request raises HumanGateInvalidStateError."""
    manager = HumanGateManager()
    gate = manager.create_request(_pending_result())
    request_id = str(gate.id)

    manager.approve(request_id, actor_id="reviewer_1")

    with pytest.raises(HumanGateInvalidStateError):
        manager.approve(request_id, actor_id="reviewer_1")


def test_pending_state_required() -> None:
    """Attempting to reject an already-approved request raises HumanGateInvalidStateError."""
    manager = HumanGateManager()
    gate = manager.create_request(_pending_result())
    request_id = str(gate.id)

    manager.approve(request_id, actor_id="reviewer_1")

    with pytest.raises(HumanGateInvalidStateError, match="only PENDING"):
        manager.reject(request_id, actor_id="reviewer_2")
