from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class BoundaryType(str, Enum):
    """Categories of boundary constraints that can be enforced at a node."""

    RATE_LIMIT = "rate_limit"
    VALUE_RANGE = "value_range"
    TIME_WINDOW = "time_window"
    RESOURCE_QUOTA = "resource_quota"
    CONFIDENCE_FLOOR = "confidence_floor"
    SIGNAL_FRESHNESS = "signal_freshness"
    CUSTOM = "custom"


class BoundaryEffect(str, Enum):
    """Effect applied to the DecisionResult when a boundary node triggers."""

    ALLOW = "allow"
    BLOCK = "block"
    OVERRIDE = "override"
    ESCALATE = "escalate"
    REDIRECT = "redirect"


class BoundarySeverity(str, Enum):
    """Severity level of a triggered boundary."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class BoundaryViolation(BaseModel):
    """Details of a single boundary constraint violation detected during evaluation."""

    boundary_type: BoundaryType = Field(...)
    constraint_name: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Human-readable name of the violated constraint",
    )
    message: str = Field(
        ...,
        min_length=1,
        description="Diagnostic message explaining the violation",
    )
    actual_value: Optional[Any] = Field(
        None,
        description="The observed value that triggered the violation",
    )
    limit_value: Optional[Any] = Field(
        None,
        description="The boundary limit or threshold that was breached",
    )
    severity: str = Field(
        "error",
        pattern=r"^(warning|error|critical)$",
        description="Violation severity: warning, error, or critical",
    )


class BoundaryResult(BaseModel):
    """Result of evaluating a single boundary node against a signal.

    Produced by ``BoundaryEngine.apply()`` for every active BOUNDARY node
    in a flow regardless of whether the boundary triggered.
    """

    boundary_id: str = Field(..., description="ID of the boundary node that was evaluated")
    triggered: bool = Field(..., description="True if the boundary condition fired")
    severity: str = Field(..., description="Severity level: critical/high/medium/low")
    effect: str = Field(..., description="Effect applied: allow/block/override/escalate/redirect")
    action: Optional[dict[str, Any]] = Field(
        None,
        description="Action payload to apply when effect is override or redirect",
    )
    reason: str = Field(..., description="Human-readable explanation of the evaluation outcome")
    evaluated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Wall-clock timestamp when boundary evaluation completed",
    )


class RuntimeBoundaryResult(BaseModel):
    """Canonical cross-system boundary result for Decision Runtime OS v2.

    Provides a stable contract for orchestrators, ledgers, and audit systems.
    Convertible from ``BoundaryResult`` via ``from_boundary_result()``.

    Field mapping from BoundaryResult:
        boundary_id → boundary_id
        not triggered → passed   (passed=True means boundary was NOT violated)
        effect → action          (the enforcement action: allow/block/escalate/…)
        action → payload         (the action's data payload, if any)
        severity → severity
        reason → reason
    """

    boundary_id: str = Field(..., description="ID of the boundary node that was evaluated")
    passed: bool = Field(
        ...,
        description="True when the boundary was satisfied (condition did NOT trigger)",
    )
    action: str = Field(
        ...,
        description="Enforcement action applied: allow/block/override/escalate/redirect",
    )
    reason: Optional[str] = Field(
        None,
        description="Human-readable explanation of the evaluation outcome",
    )
    severity: Optional[str] = Field(
        None,
        description="Severity level: critical/high/medium/low",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Action data payload (populated when effect is override or redirect)",
    )

    @classmethod
    def from_boundary_result(cls, br: BoundaryResult) -> "RuntimeBoundaryResult":
        """Convert a BoundaryResult to a RuntimeBoundaryResult."""
        return cls(
            boundary_id=br.boundary_id,
            passed=not br.triggered,
            action=br.effect,
            reason=br.reason,
            severity=br.severity,
            payload=br.action or {},
        )
