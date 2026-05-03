from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class EventType(str, Enum):
    """Typed events emitted by the decision runtime at significant lifecycle moments."""

    FLOW_STARTED = "flow.started"
    FLOW_COMPLETED = "flow.completed"
    FLOW_FAILED = "flow.failed"
    FLOW_PAUSED = "flow.paused"
    FLOW_RESUMED = "flow.resumed"
    FLOW_CANCELLED = "flow.cancelled"
    NODE_ENTERED = "node.entered"
    NODE_EXITED = "node.exited"
    NODE_FAILED = "node.failed"
    DECISION_MADE = "decision.made"
    BOUNDARY_CHECKED = "boundary.checked"
    BOUNDARY_VIOLATED = "boundary.violated"
    HUMAN_GATE_OPENED = "human_gate.opened"
    HUMAN_GATE_RESPONDED = "human_gate.responded"
    HUMAN_GATE_TIMED_OUT = "human_gate.timed_out"
    SIGNAL_RECEIVED = "signal.received"
    EXECUTION_REQUESTED = "runtime.execution.requested"
    EXECUTION_COMPLETED = "runtime.execution.completed"


class RuntimeEvent(BaseModel):
    """An immutable event record emitted by the runtime at a significant lifecycle moment.

    Events are the primary integration point for observability, audit logging,
    and reactive downstream systems.
    """

    id: UUID = Field(default_factory=uuid4)
    event_type: EventType = Field(..., description="Structured type identifier of the event")
    flow_id: UUID = Field(..., description="ID of the flow execution that produced this event")
    trace_id: UUID = Field(..., description="ID of the DecisionTrace associated with this event")
    decision_id: Optional[UUID] = Field(
        None,
        description="ID of the DecisionResult that caused this event; None for flow-level events",
    )
    node_id: Optional[str] = Field(
        None,
        description="ID of the node that produced this event; None for flow-level events",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Wall-clock time when the event was emitted",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Event-type-specific data (e.g. decision outcome, violation details)",
    )
    correlation_id: Optional[UUID] = Field(
        None,
        description="Optional ID for linking causally related events across a request chain",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
