from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from .runtime import RuntimeState
from .signal import Signal
from .decision import DecisionResult
from .boundary import BoundaryResult
from .human_gate import HumanGateRequest


class NodeTraceState(str, Enum):
    """Execution state of a single node within a flow trace."""

    PENDING = "pending"
    ENTERED = "entered"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


class NodeTrace(BaseModel):
    """Execution record for a single node in a flow run.

    Captures entry/exit timestamps, linked result IDs, and any error
    that occurred, enabling per-node audit and performance analysis.
    """

    node_id: str = Field(..., description="ID of the node that was executed")
    node_name: str = Field(...)
    state: NodeTraceState = Field(NodeTraceState.PENDING)
    entered_at: Optional[datetime] = Field(None, description="Timestamp when execution entered this node")
    exited_at: Optional[datetime] = Field(None, description="Timestamp when execution left this node")
    duration_ms: Optional[float] = Field(
        None,
        ge=0.0,
        description="Wall-clock execution time in milliseconds",
    )
    decision_result_id: Optional[UUID] = Field(
        None,
        description="ID of the DecisionResult produced at this node",
    )
    boundary_result_id: Optional[UUID] = Field(
        None,
        description="ID of the BoundaryResult produced at this node",
    )
    human_gate_request_id: Optional[UUID] = Field(
        None,
        description="ID of the HumanGateRequest opened at this node",
    )
    error_message: Optional[str] = Field(None)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DecisionTrace(BaseModel):
    """Complete execution trace for a single flow run.

    Aggregates all node-level records, signals, decisions, boundary checks,
    and human gate interactions that occurred during one execution of a flow.
    """

    id: UUID = Field(default_factory=uuid4, description="Unique trace identifier; equals DecisionResult.trace_id")
    flow_id: UUID = Field(..., description="ID of the DecisionFlow being executed")
    flow_version: str = Field(..., description="Semantic version of the flow at execution time")
    state: RuntimeState = Field(RuntimeState.RECEIVED, description="Current lifecycle state of this execution")
    decision_id: Optional[UUID] = Field(
        None,
        description="ID of the primary DecisionResult produced during this execution",
    )
    signal_id: Optional[UUID] = Field(
        None,
        description="ID of the Signal that triggered this execution",
    )
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = Field(None)
    duration_ms: Optional[float] = Field(None, ge=0.0, description="Total execution time in milliseconds")
    signals: list[Signal] = Field(
        default_factory=list,
        description="All signals present in the runtime context during this execution",
    )
    evaluated_nodes: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Per-node evaluation records; each dict has node_id, node_type, matched, condition, reason",
    )
    node_traces: list[NodeTrace] = Field(
        default_factory=list,
        description="Ordered execution records for each node visited",
    )
    decision_results: list[DecisionResult] = Field(default_factory=list)
    boundary_results: list[BoundaryResult] = Field(default_factory=list)
    human_gate_requests: list[HumanGateRequest] = Field(default_factory=list)
    error_message: Optional[str] = Field(
        None,
        description="Top-level error message present when state is FAILED",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
