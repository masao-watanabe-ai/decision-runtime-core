"""
Trace Builder — assembles a DecisionTrace from a completed evaluation run.

The trace is a complete audit record: every evaluated node, the final
DecisionResult, all boundary results, and any human gate request.

The trace id is taken from ``decision_result.trace_id`` so that
``DecisionTrace.id == DecisionResult.trace_id`` — the pair is linked by
this shared UUID.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from backend.app.models.decision import DecisionResult
from backend.app.models.flow import DecisionFlow
from backend.app.models.human_gate import HumanGateRequest
from backend.app.models.runtime import RuntimeState
from backend.app.models.signal import Signal
from backend.app.models.trace import DecisionTrace


class TraceBuilder:
    """Builds a DecisionTrace from the outputs of a single evaluation run."""

    def create_trace(
        self,
        signal: Signal,
        flow: DecisionFlow,
        decision_result: DecisionResult,
        evaluated_nodes: list[dict[str, Any]],
    ) -> DecisionTrace:
        """Assemble and return a DecisionTrace.

        Args:
            signal:           The source signal that triggered the evaluation.
            flow:             The DecisionFlow that was evaluated.
            decision_result:  The final DecisionResult produced by the engine.
            evaluated_nodes:  Per-node evaluation records collected by the engine.

        Returns:
            A fully populated DecisionTrace whose ``id`` equals
            ``decision_result.trace_id``.
        """
        gate_requests: list[HumanGateRequest] = (
            [decision_result.human_gate] if decision_result.human_gate is not None else []
        )

        return DecisionTrace(
            id=decision_result.trace_id,
            flow_id=flow.id,
            flow_version=flow.version,
            state=RuntimeState.COMPLETED,
            decision_id=decision_result.id,
            signal_id=signal.id,
            evaluated_nodes=evaluated_nodes,
            signals=[signal],
            decision_results=[decision_result],
            boundary_results=list(decision_result.boundary_results),
            human_gate_requests=gate_requests,
            started_at=decision_result.created_at,
            completed_at=datetime.now(timezone.utc),
        )
