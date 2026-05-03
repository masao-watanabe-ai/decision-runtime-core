"""
Ledger Projector — reconstructs a DecisionTrace from LedgerEvents.

The TraceStore is an in-memory runtime cache. After a service restart it is
empty, but the Ledger is the durable source of truth. This projector reads
events from the Ledger and projects them back into a DecisionTrace so that
GET /traces/{trace_id} can still serve data when the cache is cold.

Projection is read-only: it never writes to the Ledger or TraceStore.

Reconstructed fields:
    id, flow_id, flow_version   — from the first event's trace/flow fields
    signal_id                   — from the SIGNAL step's step_id
    decision_id                 — from the OUTCOME event's decision_id field
    started_at                  — occurred_at of the SIGNAL event (or first event)
    completed_at                — occurred_at of the OUTCOME event
    state                       — RuntimeState.COMPLETED when an OUTCOME event is
                                  present; RuntimeState.EVALUATING otherwise
    evaluated_nodes             — from DECISION / BOUNDARY / FALLBACK / ACTION events
    decision_results            — one partial DecisionResult from the OUTCOME event
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from backend.app.integrations.ledger_client import LedgerClient, LedgerEvent
from backend.app.models.decision import DecisionOutcome, DecisionResult, DecisionStatus
from backend.app.models.runtime import RuntimeState
from backend.app.models.trace import DecisionTrace

_OUTCOME_STEP = "outcome"
_SIGNAL_STEP = "signal"
_NODE_STEPS = {"decision", "boundary", "fallback", "action"}


class LedgerProjector:
    """Projects LedgerEvents for a trace_id into a DecisionTrace.

    Inject a LedgerClient; call project(trace_id) to get the reconstructed trace.
    Returns None when no events are found for the given trace_id.
    """

    def __init__(self, ledger_client: LedgerClient) -> None:
        self._client = ledger_client

    def project(self, trace_id: str) -> DecisionTrace | None:
        """Reconstruct a DecisionTrace from all ledger events for trace_id.

        Returns None when the ledger has no events for the given trace_id.
        """
        events = self._client.get_events_by_trace_id(trace_id)
        if not events:
            return None

        first = events[0]
        flow_id: UUID = first.flow_id
        flow_version: str = first.flow_version

        signal_id: UUID | None = None
        started_at: datetime = first.occurred_at
        completed_at: datetime | None = None
        decision_id: UUID | None = None
        evaluated_nodes: list[dict[str, Any]] = []
        outcome_event: LedgerEvent | None = None

        for event in events:
            step_type = str(event.step_type).split(".")[-1].lower()

            if step_type == _SIGNAL_STEP:
                try:
                    signal_id = UUID(event.step_id)
                except (ValueError, AttributeError):
                    pass
                started_at = event.occurred_at

            elif step_type in _NODE_STEPS:
                p = event.payload
                evaluated_nodes.append({
                    "node_id": p.get("node_id", event.step_id),
                    "node_type": p.get("node_type", step_type),
                    "matched": p.get("matched"),
                    "condition": p.get("condition"),
                    "reason": p.get("reason"),
                })

            elif step_type == _OUTCOME_STEP:
                outcome_event = event
                decision_id = event.decision_id
                completed_at = event.occurred_at

        state = RuntimeState.COMPLETED if outcome_event is not None else RuntimeState.EVALUATING

        decision_results: list[DecisionResult] = []
        if outcome_event is not None:
            decision_results = [_build_result(outcome_event, signal_id, started_at)]

        try:
            trace_uuid = UUID(trace_id)
        except ValueError:
            return None

        return DecisionTrace(
            id=trace_uuid,
            flow_id=flow_id,
            flow_version=flow_version,
            state=state,
            decision_id=decision_id,
            signal_id=signal_id,
            started_at=started_at,
            completed_at=completed_at,
            evaluated_nodes=evaluated_nodes,
            decision_results=decision_results,
            metadata={"projected_from_ledger": True},
        )


def _build_result(
    outcome_event: LedgerEvent,
    signal_id: UUID | None,
    created_at: datetime,
) -> DecisionResult:
    """Construct a partial DecisionResult from an OUTCOME LedgerEvent."""
    p = outcome_event.payload

    try:
        status = DecisionStatus(p.get("status", DecisionStatus.CONFIRMED))
    except ValueError:
        status = DecisionStatus.CONFIRMED

    try:
        outcome = DecisionOutcome(p.get("outcome", DecisionOutcome.PASS))
    except ValueError:
        outcome = DecisionOutcome.PASS

    source_signal_id: UUID = signal_id or outcome_event.trace_id

    return DecisionResult(
        id=outcome_event.decision_id,
        trace_id=outcome_event.trace_id,
        flow_id=outcome_event.flow_id,
        flow_version=outcome_event.flow_version,
        selected_node_id=p.get("selected_node_id", ""),
        source_signal_id=source_signal_id,
        state=RuntimeState.CONFIRMED,
        status=status,
        outcome=outcome,
        confidence=float(p.get("confidence", 1.0)),
        evaluated_at=outcome_event.occurred_at,
        created_at=created_at,
        updated_at=outcome_event.occurred_at,
        metadata={"projected_from_ledger": True},
    )
