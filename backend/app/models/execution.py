from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field


class ExecutionResult(BaseModel):
    """Record of a completed execution triggered by a confirmed decision.

    Produced after the orchestrator finishes executing the action selected by
    the Decision Runtime Engine.  Links back to the originating DecisionResult
    via ``decision_id`` and ``trace_id`` for audit and ledger correlation.
    """

    execution_id: str = Field(
        ...,
        description="UUID string uniquely identifying this execution",
    )
    decision_id: str = Field(
        ...,
        description="Canonical decision_id from the originating DecisionResult",
    )
    trace_id: str = Field(
        ...,
        description="Trace ID of the originating evaluation run",
    )
    status: str = Field(
        ...,
        description="Execution outcome: succeeded/failed/pending",
    )
    output: dict[str, Any] = Field(
        default_factory=dict,
        description="Execution output payload from the worker",
    )
    timestamp: Optional[str] = Field(
        None,
        description="ISO-8601 timestamp when execution completed",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp when this result was created",
    )


class ExecutionRequest(BaseModel):
    """A request to execute the action selected by the decision engine.

    Created when a DecisionResult reaches status=confirmed.  The execution_id
    uniquely identifies this specific execution attempt and is propagated
    through the runtime.execution.requested event.
    """

    execution_id: str = Field(
        ...,
        description="UUID string uniquely identifying this execution attempt",
    )
    decision_id: str = Field(
        ...,
        description="ID of the DecisionResult that triggered this request",
    )
    trace_id: str = Field(
        ...,
        description="Trace ID of the originating evaluation run",
    )
    action: Optional[dict[str, Any]] = Field(
        None,
        description="Action payload from the selected decision node",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp when this execution request was created",
    )
