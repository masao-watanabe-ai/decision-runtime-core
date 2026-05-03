"""In-memory idempotency store for decision evaluation requests."""
from __future__ import annotations

from backend.app.models.decision import DecisionResult


class IdempotencyStore:
    """Maps idempotency keys to previously computed DecisionResults.

    Thread-safety: not guaranteed; suitable for single-threaded use and tests.
    """

    def __init__(self) -> None:
        self._store: dict[str, DecisionResult] = {}

    def get(self, key: str) -> DecisionResult | None:
        """Return the cached DecisionResult for key, or None if not cached."""
        return self._store.get(key)

    def set(self, key: str, result: DecisionResult) -> None:
        """Store result under key; overwrites any existing entry."""
        self._store[key] = result
