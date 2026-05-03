from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class HumanGateStatus(str, Enum):
    """Lifecycle states of a human gate request."""

    PENDING = "pending"
    ASSIGNED = "assigned"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class HumanGateOption(BaseModel):
    """A selectable response option presented to a human reviewer."""

    value: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Machine-readable option identifier used in response_value",
    )
    label: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description="Human-readable label displayed in the review interface",
    )
    description: Optional[str] = Field(
        None,
        max_length=2048,
        description="Extended explanation of the option and its consequences",
    )
    is_default: bool = Field(
        False,
        description="Whether this option is pre-selected in the review interface",
    )


class HumanGateRequest(BaseModel):
    """A request for human review and decision at a gate node.

    The gate suspends flow execution until a reviewer submits a response
    or the deadline is exceeded.
    """

    id: UUID = Field(default_factory=uuid4)
    flow_id: UUID = Field(
        ...,
        description="ID of the flow execution that is blocked on this gate",
    )
    decision_id: Optional[UUID] = Field(
        None,
        description="ID of the DecisionResult that triggered this gate request",
    )
    trace_id: Optional[UUID] = Field(
        None,
        description="Trace ID of the originating evaluation",
    )
    node_id: str = Field(
        ...,
        description="ID of the node (boundary or human_gate) that triggered this request",
    )
    required_role: Optional[str] = Field(
        None,
        description="Role required to resolve this gate; None means any authenticated actor",
    )
    title: str = Field(..., min_length=1, max_length=512)
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Contextual data presented to the reviewer to support decision-making",
    )
    question: str = Field(
        ...,
        min_length=1,
        max_length=4096,
        description="The question or decision prompt presented to the human reviewer",
    )
    options: list[HumanGateOption] = Field(
        ...,
        min_length=1,
        description="Available response options the reviewer may select",
    )
    assignee_id: Optional[str] = Field(
        None,
        description="Identifier of the reviewer assigned to handle this request",
    )
    deadline: Optional[datetime] = Field(
        None,
        description="Timestamp after which the request transitions to TIMED_OUT",
    )
    status: HumanGateStatus = Field(HumanGateStatus.PENDING)
    response_value: Optional[str] = Field(
        None,
        description="The value of the selected HumanGateOption once the reviewer responds",
    )
    response_note: Optional[str] = Field(
        None,
        max_length=4096,
        description="Optional free-text note submitted by the reviewer alongside their response",
    )
    responded_at: Optional[datetime] = Field(
        None,
        description="Timestamp when the reviewer submitted their response",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
