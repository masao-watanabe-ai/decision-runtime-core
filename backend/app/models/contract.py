from __future__ import annotations

from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class ConditionOperator(str, Enum):
    """Comparison operators available in contract conditions."""

    EQUALS = "eq"
    NOT_EQUALS = "neq"
    GREATER_THAN = "gt"
    GREATER_THAN_OR_EQUALS = "gte"
    LESS_THAN = "lt"
    LESS_THAN_OR_EQUALS = "lte"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    IN = "in"
    NOT_IN = "not_in"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


class LogicalOperator(str, Enum):
    """Logical composition operators for combining condition groups."""

    AND = "and"
    OR = "or"
    NOT = "not"


class Condition(BaseModel):
    """A single evaluable predicate referencing a named signal."""

    signal_name: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Name of the signal whose value will be evaluated",
    )
    operator: ConditionOperator = Field(..., description="Comparison operator to apply")
    threshold: Optional[Any] = Field(
        None,
        description="Value to compare against; must be None for IS_NULL / IS_NOT_NULL operators",
    )
    description: Optional[str] = Field(
        None,
        max_length=1024,
        description="Human-readable explanation of this condition's intent",
    )


class ConditionGroup(BaseModel):
    """A logical grouping of conditions and nested groups combined by a single operator.

    Groups can be nested arbitrarily deep to express complex boolean logic.
    """

    logical_operator: LogicalOperator = Field(
        LogicalOperator.AND,
        description="Operator used to combine all conditions and nested_groups in this group",
    )
    conditions: list[Condition] = Field(
        default_factory=list,
        description="Leaf conditions evaluated within this group",
    )
    nested_groups: list[ConditionGroup] = Field(
        default_factory=list,
        description="Sub-groups whose results are combined by this group's logical operator",
    )


ConditionGroup.model_rebuild()


class ContractAction(BaseModel):
    """An action to execute when a contract's condition group resolves to a specific outcome."""

    id: UUID = Field(default_factory=uuid4)
    action_type: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Action type identifier (e.g. 'emit_event', 'set_output', 'notify')",
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Action-type-specific configuration parameters",
    )
    order: int = Field(
        0,
        ge=0,
        description="Execution sequence index; lower values execute first",
    )


class DecisionContract(BaseModel):
    """Defines the evaluation rules and resulting actions for a decision node.

    A contract specifies which signals are required, how they are evaluated,
    and what actions are triggered based on the pass/fail outcome.
    """

    id: UUID = Field(default_factory=uuid4)
    type: str = Field(
        ...,
        description="Contract type identifier used by the ContractRegistry for routing and validation",
    )
    name: str = Field(..., min_length=1, max_length=256)
    description: Optional[str] = Field(None, max_length=2048)
    version: str = Field(
        ...,
        pattern=r"^\d+\.\d+\.\d+$",
        description="Semantic version (MAJOR.MINOR.PATCH)",
    )
    required_signals: list[str] = Field(
        default_factory=list,
        description="Signal names that must be present in the runtime context for this contract to evaluate",
    )
    condition_group: Optional[ConditionGroup] = Field(
        None,
        description="Root condition group whose evaluation determines the contract outcome; "
        "may be None for inline contract references embedded in node config",
    )
    actions_on_pass: list[ContractAction] = Field(
        default_factory=list,
        description="Ordered actions executed when the condition group evaluates to true",
    )
    actions_on_fail: list[ContractAction] = Field(
        default_factory=list,
        description="Ordered actions executed when the condition group evaluates to false",
    )
    confidence_threshold: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description="Minimum confidence score required to act on a PASS outcome",
    )
    is_active: bool = Field(True, description="Inactive contracts are skipped during evaluation")
    metadata: dict[str, Any] = Field(default_factory=dict)
