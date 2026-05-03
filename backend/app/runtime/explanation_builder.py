"""
Explanation Builder — converts a DecisionTrace into a human-readable
explanation dict.

The explanation is suitable for returning directly from the
GET /api/runtime/decision/{decision_id}/explain endpoint.
"""
from __future__ import annotations

from typing import Any

from backend.app.models.trace import DecisionTrace


class ExplanationBuilder:
    """Builds a structured explanation dict from a DecisionTrace."""

    def build(self, trace: DecisionTrace) -> dict[str, Any]:
        """Return an explanation of the decision captured in ``trace``.

        Returns:
            {
                "decision_id":         str | None,
                "trace_id":            str,
                "selected_node":       str | None,
                "matched_conditions":  list[dict],
                "unmatched_conditions": list[dict],
                "boundary_effects":    list[dict],
                "human_gate":          dict | None,
                "final_action":        dict | None,
                "final_status":        str | None,
            }
        """
        primary = trace.decision_results[0] if trace.decision_results else None

        matched_conditions = [
            self._node_summary(n)
            for n in trace.evaluated_nodes
            if n.get("matched")
        ]
        unmatched_conditions = [
            self._node_summary(n)
            for n in trace.evaluated_nodes
            if not n.get("matched")
        ]
        boundary_effects = [
            {
                "boundary_id": br.boundary_id,
                "triggered": br.triggered,
                "severity": br.severity,
                "effect": br.effect,
                "reason": br.reason,
            }
            for br in trace.boundary_results
        ]

        human_gate: dict[str, Any] | None = None
        if trace.human_gate_requests:
            gate = trace.human_gate_requests[0]
            human_gate = {
                "required": True,
                "status": gate.status.value,
                "request_id": str(gate.id),
            }

        return {
            "decision_id": str(trace.decision_id) if trace.decision_id else None,
            "trace_id": str(trace.id),
            "selected_node": primary.selected_node_id if primary else None,
            "matched_conditions": matched_conditions,
            "unmatched_conditions": unmatched_conditions,
            "boundary_effects": boundary_effects,
            "human_gate": human_gate,
            "final_action": primary.action if primary else None,
            "final_status": primary.status.value if primary else None,
        }

    @staticmethod
    def _node_summary(node: dict[str, Any]) -> dict[str, Any]:
        return {
            "node_id": node.get("node_id"),
            "node_type": node.get("node_type"),
            "condition": node.get("condition"),
            "reason": node.get("reason"),
        }
