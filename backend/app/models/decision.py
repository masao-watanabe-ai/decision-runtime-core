from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator

from .boundary import BoundaryResult
from .human_gate import HumanGateRequest
from .runtime import RuntimeState


class DecisionOutcome(str, Enum):
    """Fine-grained outcome of evaluating a single condition or contract."""

    PASS = "pass"
    FAIL = "fail"
    ABSTAIN = "abstain"
    ERROR = "error"


class DecisionStatus(str, Enum):
    """High-level lifecycle status of a completed decision."""

    CONFIRMED = "confirmed"           # at least one decision node matched
    FALLBACK = "fallback"             # no decision node matched; fallback node used
    BLOCKED = "blocked"               # a boundary node blocked execution
    PENDING_HUMAN = "pending_human"   # a boundary escalated to human review
    REJECTED = "rejected"             # a human reviewer rejected the escalation
    ERROR = "error"                   # evaluation failed with an unrecoverable error


class DecisionResult(BaseModel):
    """Complete record of a single decision produced by the runtime engine.

    Populated by ``DecisionRuntimeEngine.evaluate()`` and stored in the
    execution trace for audit, replay, and downstream routing.
    """

    id: UUID = Field(default_factory=uuid4, description="Unique decision result identifier")
    trace_id: UUID = Field(..., description="ID of the runtime trace this result belongs to")
    flow_id: UUID = Field(..., description="ID of the DecisionFlow that was evaluated")
    flow_version: str = Field(..., description="Semantic version of the flow at evaluation time")
    selected_node_id: str = Field(
        ...,
        description="ID of the DecisionNode or fallback node selected by the resolution policy",
    )
    source_signal_id: UUID = Field(
        ...,
        description="ID of the Signal that triggered this evaluation",
    )
    state: RuntimeState = Field(
        ...,
        description="Runtime lifecycle state at the moment this result was produced",
    )
    status: DecisionStatus = Field(
        ...,
        description="High-level outcome: CONFIRMED when a decision node matched, FALLBACK otherwise",
    )
    outcome: DecisionOutcome = Field(
        ...,
        description="Fine-grained contract evaluation outcome",
    )
    confidence: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description="Confidence score inherited from the source signal",
    )
    action: Optional[dict[str, Any]] = Field(
        None,
        description="Action payload declared in the selected node's config; None if the node has no action",
    )
    contract_id: Optional[UUID] = Field(
        None,
        description="ID of the DecisionContract linked to the selected node; None for fallback nodes",
    )
    contract_version: Optional[str] = Field(
        None,
        description="Semantic version of the linked contract; None for fallback nodes",
    )
    signals_used: list[str] = Field(
        default_factory=list,
        description="Names of the signals that contributed to this decision",
    )
    conditions_evaluated: int = Field(
        0,
        ge=0,
        description="Number of decision nodes whose conditions were evaluated",
    )
    conditions_passed: int = Field(
        0,
        ge=0,
        description="Number of decision nodes whose conditions evaluated to true",
    )
    execution_id: Optional[str] = Field(
        None,
        description="Execution request ID attached when status=confirmed and an execution was requested",
    )
    boundary_results: list[BoundaryResult] = Field(
        default_factory=list,
        description="Results from boundary evaluation; populated after BoundaryEngine.apply()",
    )
    human_gate: Optional[HumanGateRequest] = Field(
        None,
        description="The HumanGateRequest created when status is pending_human; None otherwise",
    )
    error_message: Optional[str] = Field(
        None,
        description="Diagnostic message present when status is ERROR",
    )
    evaluated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp when the engine completed evaluation",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp when this record was created",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp of the most recent update to this record",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    # ------------------------------------------------------------------
    # Canonical cross-system contract fields (Decision Runtime OS v2)
    # ------------------------------------------------------------------
    # These fields provide a stable interface for downstream systems
    # (orchestrator, ledger, audit) regardless of internal UUID types.
    # Populated automatically by model_validator from existing fields.
    # ------------------------------------------------------------------

    decision_id: str = Field(
        default="",
        description="Canonical string decision identifier (derived from id)",
    )
    signal_id: Optional[str] = Field(
        None,
        description="Canonical string signal identifier (derived from source_signal_id)",
    )
    decision_type: str = Field(
        default="",
        description="Decision category (derived from status when not explicitly set)",
    )
    selected_flow_id: Optional[str] = Field(
        None,
        description="String flow identifier for orchestrator routing (derived from flow_id)",
    )
    reason: Optional[str] = Field(
        None,
        description="Human-readable reason for the decision outcome",
    )
    human_gate_required: bool = Field(
        False,
        description="True when human approval is required (derived from human_gate)",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Decision payload for downstream execution (derived from action)",
    )
    timestamp: Optional[str] = Field(
        None,
        description="ISO-8601 evaluation timestamp (derived from evaluated_at)",
    )
    schema_version: str = Field(
        "decision-result/v1",
        description="Schema version for cross-system compatibility",
    )

    @model_validator(mode="after")
    def _populate_canonical_fields(self) -> "DecisionResult":
        """Auto-populate canonical fields from existing UUID-based fields."""
        if not self.decision_id:
            self.decision_id = str(self.id)
        if self.signal_id is None:
            self.signal_id = str(self.source_signal_id)
        if not self.decision_type:
            self.decision_type = self.status.value
        if self.selected_flow_id is None:
            self.selected_flow_id = str(self.flow_id)
        if self.reason is None and self.error_message:
            self.reason = self.error_message
        # human_gate_required is always derived — consistent with human_gate presence
        self.human_gate_required = self.human_gate is not None
        if not self.payload and self.action:
            self.payload = self.action
        if self.timestamp is None:
            self.timestamp = self.evaluated_at.isoformat()
        return self
