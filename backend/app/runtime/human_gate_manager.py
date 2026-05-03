"""
Human Gate Manager — pending_human → approve / reject → confirmed / rejected.

Storage is in-memory (MVP).  No Redis, no DB, no async workers.

Lifecycle:
    create_request()  — called by the engine when status == pending_human
    approve()         — actor confirms; DecisionResult.status → confirmed
    reject()          — actor rejects;  DecisionResult.status → rejected, action → None
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from backend.app.models.decision import DecisionResult, DecisionStatus
from backend.app.models.human_gate import HumanGateOption, HumanGateRequest, HumanGateStatus
from backend.app.models.runtime import RuntimeState


class HumanGateNotFoundError(Exception):
    """Raised when a gate request ID cannot be found in the store."""


class HumanGateInvalidStateError(Exception):
    """Raised when an action is attempted on a gate request in the wrong state."""


class HumanGateInsufficientRoleError(Exception):
    """Raised when the actor does not hold the required_role for a gate."""


_APPROVE_OPTION = HumanGateOption(
    value="approve",
    label="Approve",
    description="Confirm the escalated decision and allow it to proceed",
    is_default=True,
)
_REJECT_OPTION = HumanGateOption(
    value="reject",
    label="Reject",
    description="Deny the escalated decision and mark it as rejected",
    is_default=False,
)


class HumanGateManager:
    """Manages the lifecycle of HumanGateRequests using in-memory storage.

    One instance should be shared across the application (stored in app.state).
    """

    def __init__(self) -> None:
        self._store: dict[str, HumanGateRequest] = {}
        self._results: dict[str, DecisionResult] = {}

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def create_request(
        self,
        decision_result: DecisionResult,
        required_role: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> HumanGateRequest:
        """Create and store a HumanGateRequest linked to a pending DecisionResult.

        Args:
            decision_result: The DecisionResult whose status is pending_human.
            required_role:   Role required to resolve the gate; None = any actor.
            reason:          Optional human-readable reason for the escalation.

        Returns:
            A newly created HumanGateRequest in PENDING status.
        """
        title = reason or "Human review required for escalated decision"
        request = HumanGateRequest(
            flow_id=decision_result.flow_id,
            decision_id=decision_result.id,
            trace_id=decision_result.trace_id,
            node_id=decision_result.selected_node_id,
            required_role=required_role,
            title=title,
            question="Review the escalated decision and choose to approve or reject it",
            options=[_APPROVE_OPTION, _REJECT_OPTION],
        )
        key = str(request.id)
        self._store[key] = request
        self._results[key] = decision_result
        return request

    def approve(
        self,
        request_id: str,
        actor_id: str,
        comment: Optional[str] = None,
        actor_roles: list[str] | None = None,
    ) -> DecisionResult:
        """Approve a pending gate request; transitions the DecisionResult to confirmed.

        Args:
            request_id: String form of the HumanGateRequest UUID.
            actor_id:   Identifier of the reviewer approving the request.
            comment:    Optional note attached to the response.

        Returns:
            Updated DecisionResult with status=confirmed and state=confirmed.

        Raises:
            HumanGateNotFoundError:       If request_id is not in the store.
            HumanGateInvalidStateError:   If the request is not in PENDING state.
            HumanGateInsufficientRoleError: If actor_roles does not include required_role.
        """
        request = self._get_request(request_id)
        self._require_pending(request)
        self._require_role(request, actor_roles)

        now = datetime.now(timezone.utc)
        self._store[request_id] = request.model_copy(
            update={
                "status": HumanGateStatus.APPROVED,
                "response_value": "approve",
                "response_note": comment,
                "responded_at": now,
            }
        )

        updated = self._results[request_id].model_copy(
            update={
                "state": RuntimeState.CONFIRMED,
                "status": DecisionStatus.CONFIRMED,
                "updated_at": now,
            }
        )
        self._results[request_id] = updated
        return updated

    def reject(
        self,
        request_id: str,
        actor_id: str,
        comment: Optional[str] = None,
        actor_roles: list[str] | None = None,
    ) -> DecisionResult:
        """Reject a pending gate request; transitions the DecisionResult to rejected.

        Args:
            request_id: String form of the HumanGateRequest UUID.
            actor_id:   Identifier of the reviewer rejecting the request.
            comment:    Optional note attached to the response.

        Returns:
            Updated DecisionResult with status=rejected, state=rejected, action=None.

        Raises:
            HumanGateNotFoundError:       If request_id is not in the store.
            HumanGateInvalidStateError:   If the request is not in PENDING state.
            HumanGateInsufficientRoleError: If actor_roles does not include required_role.
        """
        request = self._get_request(request_id)
        self._require_pending(request)
        self._require_role(request, actor_roles)

        now = datetime.now(timezone.utc)
        self._store[request_id] = request.model_copy(
            update={
                "status": HumanGateStatus.REJECTED,
                "response_value": "reject",
                "response_note": comment,
                "responded_at": now,
            }
        )

        updated = self._results[request_id].model_copy(
            update={
                "state": RuntimeState.REJECTED,
                "status": DecisionStatus.REJECTED,
                "action": None,
                "updated_at": now,
            }
        )
        self._results[request_id] = updated
        return updated

    def get_request(self, request_id: str) -> HumanGateRequest:
        """Return a stored gate request by ID.

        Raises:
            HumanGateNotFoundError: If request_id is not in the store.
        """
        return self._get_request(request_id)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _get_request(self, request_id: str) -> HumanGateRequest:
        request = self._store.get(request_id)
        if request is None:
            raise HumanGateNotFoundError(
                f"HumanGateRequest '{request_id}' not found"
            )
        return request

    def _require_pending(self, request: HumanGateRequest) -> None:
        if request.status != HumanGateStatus.PENDING:
            raise HumanGateInvalidStateError(
                f"HumanGateRequest '{request.id}' is in state "
                f"'{request.status.value}'; only PENDING requests can be resolved"
            )

    def _require_role(self, request: HumanGateRequest, actor_roles: list[str] | None) -> None:
        if request.required_role is not None:
            roles = actor_roles or []
            if request.required_role not in roles:
                raise HumanGateInsufficientRoleError(
                    f"Role '{request.required_role}' required to resolve "
                    f"gate '{request.id}'"
                )
