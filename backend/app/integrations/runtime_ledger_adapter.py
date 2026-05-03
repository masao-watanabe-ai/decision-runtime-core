"""
Runtime Ledger Adapter — bridges DecisionRuntimeEngine output to Ledger Core v2.

Converts a DecisionTrace / DecisionResult pair into a sequence of LedgerEvents
and appends them to the immutable ledger.

Step type mapping:
    trace.signal_id              → StepType.SIGNAL  (triggering signal)
    evaluated_nodes.decision     → StepType.DECISION
    evaluated_nodes.boundary     → StepType.BOUNDARY
    evaluated_nodes.fallback     → StepType.OUTCOME
    evaluated_nodes.action       → StepType.ACTION
    trace.human_gate_requests    → StepType.HUMAN
    final DecisionResult         → StepType.OUTCOME (resolution summary)

Event IDs are derived deterministically from (trace_id, step_key) via UUID5
so that committing the same trace twice produces the same event_ids — the
ledger's duplicate check then prevents double-writing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid5, NAMESPACE_URL

from backend.app.integrations.ledger_client import (
    LedgerAppendResult,
    LedgerAppendStatus,
    LedgerClient,
    LedgerEvent,
)
from backend.app.models.decision import DecisionResult
from backend.app.models.human_gate import HumanGateRequest
from backend.app.models.trace import DecisionTrace

# Project-scoped namespace for deterministic event UUIDs.
_LEDGER_NS: UUID = uuid5(NAMESPACE_URL, "decision-runtime-core:ledger:v1")


class StepType(str, Enum):
    """Ledger step categories that classify each event in a decision trace."""

    SIGNAL = "signal"
    DECISION = "decision"
    BOUNDARY = "boundary"
    HUMAN = "human"
    ACTION = "action"
    OUTCOME = "outcome"


_NODE_TYPE_TO_STEP: dict[str, StepType] = {
    "signal": StepType.SIGNAL,
    "decision": StepType.DECISION,
    "boundary": StepType.BOUNDARY,
    "human_gate": StepType.HUMAN,
    "action": StepType.ACTION,
    "fallback": StepType.OUTCOME,
}


class LedgerCommitStatus(str, Enum):
    """Aggregate result of committing all events from a single trace."""

    ACCEPTED = "accepted"    # every event accepted
    PARTIAL = "partial"      # mix of accepted and duplicate
    DUPLICATE = "duplicate"  # every event was already present
    FAILED = "failed"        # at least one INVALID response


@dataclass
class LedgerCommitResult:
    """Summary of a commit() call."""

    status: LedgerCommitStatus
    appended: int
    duplicates: int
    event_ids: list[UUID] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class RuntimeLedgerAdapter:
    """Converts DecisionTrace / DecisionResult to LedgerEvents and appends them.

    Inject an instance into DecisionRuntimeEngine to enable ledger integration.
    The adapter is stateless beyond its LedgerClient reference — safe to reuse
    across evaluations.
    """

    def __init__(
        self,
        ledger_client: LedgerClient | None = None,
        schema_version: str = "1.0",
    ) -> None:
        self._client: LedgerClient = ledger_client or LedgerClient()
        self._schema_version = schema_version

    def commit(
        self,
        trace: DecisionTrace,
        decision_result: DecisionResult,
    ) -> LedgerCommitResult:
        """Convert trace + decision_result to LedgerEvents and append each one.

        In DUPLICATE scenarios (same trace committed twice), the second call
        returns LedgerCommitStatus.DUPLICATE without writing anything new.
        """
        events = self._build_events(trace, decision_result)

        appended = 0
        duplicates = 0
        event_ids: list[UUID] = []
        errors: list[str] = []

        for event in events:
            result = self._client.append(event)
            event_ids.append(result.event_id)
            if result.status == LedgerAppendStatus.ACCEPTED:
                appended += 1
            elif result.status == LedgerAppendStatus.DUPLICATE:
                duplicates += 1
            else:
                errors.append(
                    result.error_message or f"invalid event: {event.event_id}"
                )

        if errors:
            commit_status = LedgerCommitStatus.FAILED
        elif appended == 0 and duplicates > 0:
            commit_status = LedgerCommitStatus.DUPLICATE
        elif duplicates > 0:
            commit_status = LedgerCommitStatus.PARTIAL
        else:
            commit_status = LedgerCommitStatus.ACCEPTED

        return LedgerCommitResult(
            status=commit_status,
            appended=appended,
            duplicates=duplicates,
            event_ids=event_ids,
            errors=errors,
        )

    def append_human_gate_event(
        self,
        gate_request: HumanGateRequest,
        decision_result: DecisionResult,
        actor_id: str,
        action: str,
        occurred_at: datetime,
        actor_roles: list[str] | None = None,
        comment: str | None = None,
    ) -> LedgerAppendResult:
        """Append a human gate resolution event (approve/reject) to the ledger.

        The event_id is deterministic: same gate + same action always maps to
        the same event_id, making repeated calls idempotent (DUPLICATE on retry).

        Args:
            gate_request:    The HumanGateRequest being resolved.
            decision_result: The DecisionResult returned by approve/reject.
            actor_id:        Identifier of the actor who took the action.
            action:          "approve" or "reject".
            occurred_at:     Wall-clock time of the action.
            actor_roles:     Roles held by the actor at action time; None if auth disabled.
            comment:         Optional reviewer note.

        Returns:
            LedgerAppendResult with ACCEPTED, DUPLICATE, or INVALID status.
        """
        trace_id: UUID | None = (
            gate_request.trace_id if gate_request.trace_id is not None else decision_result.trace_id
        )
        decision_id: UUID = (
            gate_request.decision_id if gate_request.decision_id is not None else decision_result.id
        )

        if trace_id is None:
            return LedgerAppendResult(
                status=LedgerAppendStatus.INVALID,
                event_id=gate_request.id,
                error_message="trace_id is required for human gate ledger event",
            )

        gate_id_str = str(gate_request.id)
        event = LedgerEvent(
            event_id=_event_id(trace_id, f"human_action:{gate_id_str}:{action}"),
            schema_version=self._schema_version,
            trace_id=trace_id,
            decision_id=decision_id,
            flow_id=gate_request.flow_id,
            flow_version=decision_result.flow_version,
            step_type=StepType.HUMAN,
            step_id=gate_id_str,
            payload={
                "event_type": f"runtime.human_gate.{action}d",
                "gate_id": gate_id_str,
                "decision_id": str(decision_id),
                "action": action,
                "actor_id": actor_id,
                "actor_roles": actor_roles,
                "required_role": gate_request.required_role,
                "comment": comment,
                f"{action}d_at": occurred_at.isoformat(),
            },
            occurred_at=occurred_at,
        )
        return self._client.append(event)

    def get_by_event_id(self, event_id: UUID) -> "LedgerEvent | None":
        """Return the ledger event with the given ID, or None if not found."""
        return self._client.get_by_event_id(event_id)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _build_events(
        self,
        trace: DecisionTrace,
        decision_result: DecisionResult,
    ) -> list[LedgerEvent]:
        """Produce the ordered sequence of LedgerEvents for one evaluation."""
        events: list[LedgerEvent] = []

        # 1. Signal event — the triggering input.
        if trace.signal_id is not None:
            events.append(
                LedgerEvent(
                    event_id=_event_id(trace.id, f"signal:{trace.signal_id}"),
                    schema_version=self._schema_version,
                    trace_id=trace.id,
                    decision_id=decision_result.id,
                    flow_id=trace.flow_id,
                    flow_version=trace.flow_version,
                    step_type=StepType.SIGNAL,
                    step_id=str(trace.signal_id),
                    payload={"signal_id": str(trace.signal_id)},
                    occurred_at=trace.started_at,
                )
            )

        # 2. Per-node events — one per entry in evaluated_nodes.
        for node_record in trace.evaluated_nodes:
            node_id: str = node_record.get("node_id", "")
            node_type: str = node_record.get("node_type", "decision")
            step_type = _NODE_TYPE_TO_STEP.get(node_type, StepType.DECISION)
            events.append(
                LedgerEvent(
                    event_id=_event_id(trace.id, f"node:{node_id}"),
                    schema_version=self._schema_version,
                    trace_id=trace.id,
                    decision_id=decision_result.id,
                    flow_id=trace.flow_id,
                    flow_version=trace.flow_version,
                    step_type=step_type,
                    step_id=node_id,
                    payload={
                        "node_id": node_id,
                        "node_type": node_type,
                        "matched": node_record.get("matched"),
                        "condition": node_record.get("condition"),
                        "reason": node_record.get("reason"),
                    },
                    occurred_at=trace.completed_at or trace.started_at,
                )
            )

        # 3. Human gate events — from human_gate_requests embedded in the trace.
        for gate_req in trace.human_gate_requests:
            events.append(
                LedgerEvent(
                    event_id=_event_id(trace.id, f"human:{gate_req.id}"),
                    schema_version=self._schema_version,
                    trace_id=trace.id,
                    decision_id=decision_result.id,
                    flow_id=trace.flow_id,
                    flow_version=trace.flow_version,
                    step_type=StepType.HUMAN,
                    step_id=str(gate_req.id),
                    payload={
                        "gate_id": str(gate_req.id),
                        "node_id": gate_req.node_id,
                        "status": gate_req.status.value,
                        "required_role": gate_req.required_role,
                    },
                    occurred_at=gate_req.created_at,
                )
            )

        # 4. Outcome event — resolution summary for the whole evaluation.
        events.append(
            LedgerEvent(
                event_id=_event_id(trace.id, f"outcome:{decision_result.id}"),
                schema_version=self._schema_version,
                trace_id=trace.id,
                decision_id=decision_result.id,
                flow_id=trace.flow_id,
                flow_version=trace.flow_version,
                step_type=StepType.OUTCOME,
                step_id=str(decision_result.id),
                payload={
                    "decision_id": str(decision_result.id),
                    "status": decision_result.status.value,
                    "outcome": decision_result.outcome.value,
                    "selected_node_id": decision_result.selected_node_id,
                    "confidence": decision_result.confidence,
                },
                occurred_at=decision_result.evaluated_at,
            )
        )

        return events


def _event_id(trace_id: UUID, step_key: str) -> UUID:
    """Derive a deterministic event UUID from trace_id and a step-specific key."""
    return uuid5(_LEDGER_NS, f"{trace_id}:{step_key}")
