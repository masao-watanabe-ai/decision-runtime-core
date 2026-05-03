from enum import Enum


class RuntimeState(str, Enum):
    """Lifecycle states of the decision runtime engine."""

    RECEIVED = "received"
    EVALUATING = "evaluating"
    PENDING_HUMAN = "pending_human"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
