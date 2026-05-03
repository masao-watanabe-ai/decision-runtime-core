from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Union
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class SignalValueType(str, Enum):
    """Declared data type categories for signal values."""

    NUMERIC = "numeric"
    BOOLEAN = "boolean"
    STRING = "string"
    JSON = "json"


class Signal(BaseModel):
    """A typed input signal carrying data into the decision runtime.

    Signals are the primary data carriers that contracts evaluate against.
    Each signal has a declared value type and is sourced from a specific origin.

    The ``type``, ``confidence``, and ``payload`` fields are the primary
    inputs to condition evaluation.  The raw ``value`` field retains the
    original scalar or composite value for audit purposes.
    """

    id: UUID = Field(default_factory=uuid4, description="Unique signal identifier")
    name: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Signal name used to reference this signal in contract conditions",
    )
    type: str = Field(
        "",
        max_length=256,
        description="Domain type classification of the signal (e.g. 'customer_complaint', 'refund_request')",
    )
    value_type: SignalValueType = Field(..., description="Declared data type of the signal value")
    value: Union[float, bool, str, dict[str, Any], None] = Field(
        None,
        description="Raw signal payload; must be consistent with value_type",
    )
    confidence: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description="Confidence score for this signal in the range [0.0, 1.0]",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured key-value payload used in condition expressions (e.g. payload.customer_tier)",
    )
    source: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description="Origin of the signal (service name, sensor ID, user identifier, etc.)",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Wall-clock time when the signal was emitted at its source",
    )
    idempotency_key: Optional[str] = Field(
        None,
        max_length=512,
        description="Caller-supplied key for idempotent evaluation; used by the engine to recover from Ledger DUPLICATE responses",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary key-value pairs for tagging and routing",
    )

    model_config = {"json_encoders": {datetime: lambda v: v.isoformat()}}
