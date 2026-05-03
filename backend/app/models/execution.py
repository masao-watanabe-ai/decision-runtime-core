from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field


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
