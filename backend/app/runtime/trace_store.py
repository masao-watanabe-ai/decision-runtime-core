"""
Trace Store — in-memory storage for DecisionTrace objects.

Supports lookup by trace_id (equals DecisionResult.trace_id) and by
decision_id (equals DecisionResult.id).

No external dependencies; suitable for unit tests and single-process
deployments.  Swap this class for a persistent implementation (Redis,
Postgres) without changing callers.
"""
from __future__ import annotations

from backend.app.models.trace import DecisionTrace


class TraceNotFoundError(Exception):
    """Raised when a trace cannot be found by trace_id or decision_id."""


class TraceStore:
    """In-memory store for DecisionTrace objects.

    Indexes:
        _by_trace_id    — keyed by str(trace.id)
        _by_decision_id — keyed by str(decision_result.id) for every result
                          stored in trace.decision_results; also keyed by
                          str(trace.decision_id) when set.
    """

    def __init__(self) -> None:
        self._by_trace_id: dict[str, DecisionTrace] = {}
        self._by_decision_id: dict[str, DecisionTrace] = {}

    def save(self, trace: DecisionTrace) -> None:
        """Store a trace and update both indexes."""
        key = str(trace.id)
        self._by_trace_id[key] = trace

        # Index by each embedded decision result's id.
        for result in trace.decision_results:
            self._by_decision_id[str(result.id)] = trace

        # Also index by the explicit decision_id shortcut when present.
        if trace.decision_id is not None:
            self._by_decision_id[str(trace.decision_id)] = trace

    def get(self, trace_id: str) -> DecisionTrace:
        """Return a trace by its trace_id.

        Raises:
            TraceNotFoundError: If trace_id is not in the store.
        """
        trace = self._by_trace_id.get(trace_id)
        if trace is None:
            raise TraceNotFoundError(f"Trace '{trace_id}' not found")
        return trace

    def get_by_decision_id(self, decision_id: str) -> DecisionTrace:
        """Return the trace linked to the given decision_id.

        Raises:
            TraceNotFoundError: If no trace is indexed under decision_id.
        """
        trace = self._by_decision_id.get(decision_id)
        if trace is None:
            raise TraceNotFoundError(
                f"No trace found for decision '{decision_id}'"
            )
        return trace
