"""
Boundary Engine — DecisionResult + boundary nodes → adjusted DecisionResult.

Determinism contract:
    apply(signal, flow, decision_result) is a pure function.
    Given identical inputs it always produces a result with identical
    status, selected_node_id, action, and boundary_results regardless of
    when or how many times it is called.

Scope:
    Only BOUNDARY nodes are processed.  HUMAN_GATE, DECISION, FALLBACK,
    and all other node types are ignored.  Human gate requests are NOT
    created here; the escalate effect only flips status to pending_human.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from backend.app.models.boundary import BoundaryResult
from backend.app.models.decision import DecisionResult, DecisionStatus
from backend.app.models.flow import DecisionFlow, NodeType
from backend.app.models.signal import Signal
from backend.app.runtime.condition_evaluator import ConditionEvaluator

SEVERITY_ORDER: dict[str, int] = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}


class BoundaryEngine:
    """Evaluates all BOUNDARY nodes in a flow and adjusts the DecisionResult.

    Evaluation steps:
        1. Evaluate ALL active boundary nodes using their condition expression.
        2. Collect triggered boundaries.
        3. Sort triggered boundaries by severity (critical > high > medium > low).
        4. Apply the effect of the highest-severity triggered boundary.
    """

    def __init__(self) -> None:
        self._evaluator = ConditionEvaluator()

    def apply(
        self,
        signal: Signal,
        flow: DecisionFlow,
        decision_result: DecisionResult,
    ) -> tuple[DecisionResult, list[BoundaryResult]]:
        """Evaluate boundary nodes and return an adjusted DecisionResult.

        Args:
            signal:          The source signal (provides evaluation context).
            flow:            The DecisionFlow containing boundary node definitions.
            decision_result: The result produced by DecisionRuntimeEngine.evaluate().

        Returns:
            A tuple of (updated DecisionResult, all BoundaryResult records).
            The updated result's ``boundary_results`` field contains the same list.
        """
        context = self._build_context(signal)

        boundary_nodes = [
            n for n in flow.nodes
            if n.node_type == NodeType.BOUNDARY and n.is_active
        ]

        # Step 1: Evaluate ALL boundary nodes regardless of outcome.
        all_results: list[BoundaryResult] = []
        for node in boundary_nodes:
            triggered = self._evaluator.evaluate(node.condition, context)
            severity = node.severity or "low"
            effect = node.effect or "allow"
            reason = (
                f"Boundary '{node.id}' triggered with effect '{effect}'"
                if triggered
                else f"Boundary '{node.id}' condition not met"
            )
            all_results.append(
                BoundaryResult(
                    boundary_id=node.id,
                    triggered=triggered,
                    severity=severity,
                    effect=effect,
                    action=node.action,
                    reason=reason,
                    evaluated_at=datetime.now(timezone.utc),
                )
            )

        # Step 2: Collect triggered boundaries.
        triggered = [r for r in all_results if r.triggered]

        if not triggered:
            updated = decision_result.model_copy(update={"boundary_results": all_results})
            return updated, all_results

        # Step 3: Sort by severity descending; stable sort preserves node declaration order for ties.
        triggered.sort(key=lambda r: SEVERITY_ORDER.get(r.severity, 0), reverse=True)

        # Step 4: Apply the highest-severity effect.
        dominant = triggered[0]
        updated = self._apply_effect(decision_result, dominant)
        updated = updated.model_copy(update={"boundary_results": all_results})
        return updated, all_results

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _build_context(self, signal: Signal) -> dict[str, Any]:
        """Map Signal fields to the condition evaluation context."""
        return {
            "type": signal.type,
            "confidence": signal.confidence,
            "payload": signal.payload,
            "source": signal.source,
            "created_at": signal.timestamp,
        }

    def _apply_effect(
        self, result: DecisionResult, boundary: BoundaryResult
    ) -> DecisionResult:
        """Return a new DecisionResult with the boundary effect applied.

        Effects:
            allow     — no change.
            block     — status → blocked, action → None.
            override  — action and selected_node_id replaced by boundary values.
            escalate  — status → pending_human, selected_node_id → boundary.
            redirect  — action and selected_node_id replaced by boundary values.
        """
        effect = boundary.effect

        if effect == "allow":
            return result

        if effect == "block":
            return result.model_copy(
                update={"status": DecisionStatus.BLOCKED, "action": None}
            )

        if effect == "override":
            return result.model_copy(
                update={
                    "action": boundary.action,
                    "selected_node_id": boundary.boundary_id,
                }
            )

        if effect == "escalate":
            return result.model_copy(
                update={
                    "status": DecisionStatus.PENDING_HUMAN,
                    "selected_node_id": boundary.boundary_id,
                }
            )

        if effect == "redirect":
            return result.model_copy(
                update={
                    "action": boundary.action,
                    "selected_node_id": boundary.boundary_id,
                }
            )

        return result
