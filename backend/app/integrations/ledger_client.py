"""
Ledger Client — interface for appending events to Decision Trace Ledger Core v2.

This stub implements an in-memory ledger suitable for unit tests and local
development.  Swap LedgerClient.append() for a real Postgres/append-only-log
backend without changing any callers.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class LedgerAppendStatus(str, Enum):
    """Result of a single ledger append operation."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    INVALID = "invalid"


class LedgerEvent(BaseModel):
    """A single immutable event record written to the ledger."""

    event_id: UUID = Field(default_factory=uuid4, description="Unique event identifier; deterministic for dedup")
    schema_version: str = Field("1.0")
    trace_id: UUID = Field(..., description="Execution trace this event belongs to")
    decision_id: UUID = Field(..., description="DecisionResult this event is part of")
    flow_id: UUID
    flow_version: str
    step_type: str = Field(..., description="StepType value: signal/decision/boundary/human/action/outcome")
    step_id: str = Field(..., description="Identifier of the step within the trace (node_id, signal_id, etc.)")
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LedgerAppendResult(BaseModel):
    """Result returned by LedgerClient.append()."""

    status: LedgerAppendStatus
    event_id: UUID
    error_message: Optional[str] = None


class LedgerClient:
    """In-memory stub for Ledger Core v2.

    Tracks events by event_id and rejects duplicates.
    Thread-safety: not guaranteed; suitable for single-threaded use and tests.
    """

    def __init__(self) -> None:
        self._events: list[LedgerEvent] = []
        self._seen_ids: set[UUID] = set()
        self._by_event_id: dict[UUID, LedgerEvent] = {}
        self._by_trace_id: dict[str, list[LedgerEvent]] = {}

    def append(self, event: LedgerEvent) -> LedgerAppendResult:
        """Append an event to the ledger.

        Returns DUPLICATE when event_id was already appended.
        Returns ACCEPTED on success.
        """
        if event.event_id in self._seen_ids:
            return LedgerAppendResult(
                status=LedgerAppendStatus.DUPLICATE,
                event_id=event.event_id,
            )
        self._events.append(event)
        self._seen_ids.add(event.event_id)
        self._by_event_id[event.event_id] = event
        key = str(event.trace_id)
        self._by_trace_id.setdefault(key, []).append(event)
        return LedgerAppendResult(
            status=LedgerAppendStatus.ACCEPTED,
            event_id=event.event_id,
        )

    def get_by_event_id(self, event_id: UUID) -> Optional[LedgerEvent]:
        """Return the event with the given event_id, or None if not found."""
        return self._by_event_id.get(event_id)

    def get_events_by_trace_id(self, trace_id: str) -> list[LedgerEvent]:
        """Return all events belonging to trace_id in append order."""
        return list(self._by_trace_id.get(trace_id, []))

    def get_events(self) -> list[LedgerEvent]:
        """Return all stored events in append order."""
        return list(self._events)
