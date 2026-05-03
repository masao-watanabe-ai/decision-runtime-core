"""
Decision Runtime Engine — Signal → Decision nodes → Resolution → DecisionResult.

Routing determinism contract:
    Given identical inputs, evaluate() always produces a result with identical
    selected_node_id, status, outcome, and action.  Only the auto-generated
    UUID fields (id, trace_id) legitimately differ between calls.

Side-effect pipeline (all optional, injected via constructor):
    parallel mode  — EventBus publish → TraceStore save → Ledger commit (fire-and-forget)
    strict mode    — Ledger commit → if ACCEPTED: EventBus publish → TraceStore save
                                     if non-ACCEPTED: return DecisionStatus.ERROR immediately

In strict mode the Ledger is the official commit gate: execution is never
requested for a decision the Ledger has not accepted.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from backend.app.integrations.event_bus import EventBus
from backend.app.observability import metrics as _metrics
from backend.app.integrations.execution_publisher import ExecutionPublisher
from backend.app.integrations.runtime_ledger_adapter import LedgerCommitStatus, RuntimeLedgerAdapter
from backend.app.models.decision import DecisionOutcome, DecisionResult, DecisionStatus
from backend.app.models.event import EventType, RuntimeEvent
from backend.app.models.execution import ExecutionRequest
from backend.app.models.flow import DecisionFlow, DecisionNode, NodeType
from backend.app.models.runtime import RuntimeState
from backend.app.models.signal import Signal
from backend.app.runtime.boundary_engine import BoundaryEngine
from backend.app.runtime.condition_evaluator import ConditionEvaluationError, ConditionEvaluator
from backend.app.runtime.human_gate_manager import HumanGateManager
from backend.app.runtime.idempotency_store import IdempotencyStore
from backend.app.runtime.trace_builder import TraceBuilder
from backend.app.runtime.trace_store import TraceNotFoundError, TraceStore


class DecisionRuntimeEngine:
    """Evaluates a Signal against the decision nodes of a DecisionFlow and returns a DecisionResult.

    Only DECISION and FALLBACK nodes are processed.  BOUNDARY, HUMAN_GATE,
    and all other node types are ignored at this stage.
    """

    def __init__(
        self,
        human_gate_manager: HumanGateManager | None = None,
        trace_store: TraceStore | None = None,
        event_bus: EventBus | None = None,
        ledger_adapter: RuntimeLedgerAdapter | None = None,
        ledger_mode: str = "parallel",
        idempotency_store: IdempotencyStore | None = None,
        execution_publisher: ExecutionPublisher | None = None,
    ) -> None:
        self._evaluator: ConditionEvaluator = ConditionEvaluator()
        self._boundary_engine: BoundaryEngine = BoundaryEngine()
        self._human_gate_manager: HumanGateManager | None = human_gate_manager
        self._trace_builder: TraceBuilder = TraceBuilder()
        self._trace_store: TraceStore | None = trace_store
        self._event_bus: EventBus | None = event_bus
        self._ledger_adapter: RuntimeLedgerAdapter | None = ledger_adapter
        self._ledger_mode: str = ledger_mode
        self._idempotency_store: IdempotencyStore | None = idempotency_store
        self._execution_publisher: ExecutionPublisher | None = execution_publisher

    def evaluate(self, signal: Signal, flow: DecisionFlow) -> DecisionResult:
        """Run the decision evaluation pipeline for one signal against one flow.

        Steps:
            1. Build evaluation context from the signal.
            2. Evaluate all active DECISION nodes; collect matches.
            3. If no match, select the FALLBACK node.
            4. Apply the flow's resolution policy to pick the winner from matches.
            5. Return a fully populated DecisionResult.

        Args:
            signal: The input signal to evaluate.
            flow:   The decision flow defining which nodes and conditions to apply.

        Returns:
            A DecisionResult describing the selected node and evaluation metadata.

        Raises:
            ConditionEvaluationError: When a node's condition expression is invalid.
            RuntimeError: When no active fallback node is present and no node matched.
        """
        _eval_start = time.monotonic()

        def _record(result: "DecisionResult") -> "DecisionResult":
            _metrics.increment("decision_evaluate_total", {"status": result.status.value})
            _metrics.observe("decision_evaluate_duration_seconds", time.monotonic() - _eval_start)
            return result

        trace_id = uuid4()
        context = self._build_context(signal)
        evaluated_nodes: list[dict[str, Any]] = []

        # Collect active decision nodes in flow.nodes declaration order.
        decision_nodes: list[DecisionNode] = [
            n for n in flow.nodes
            if n.node_type == NodeType.DECISION and n.is_active
        ]

        matched: list[DecisionNode] = []
        conditions_evaluated = 0
        conditions_passed = 0

        for node in decision_nodes:
            condition: str | None = node.condition
            conditions_evaluated += 1
            # Propagate ConditionEvaluationError; the engine does not silently
            # absorb bad condition expressions.
            node_matched = self._evaluator.evaluate(condition, context)
            if node_matched:
                matched.append(node)
                conditions_passed += 1
            evaluated_nodes.append({
                "node_id": node.id,
                "node_type": "decision",
                "matched": node_matched,
                "condition": condition or "",
                "reason": "condition matched" if node_matched else "condition not matched",
            })

        if matched:
            selected = self._resolve(matched, flow)
            status = DecisionStatus.CONFIRMED
            outcome = DecisionOutcome.PASS
        else:
            selected = self._get_fallback(flow)
            status = DecisionStatus.FALLBACK
            outcome = DecisionOutcome.FAIL
            evaluated_nodes.append({
                "node_id": selected.id,
                "node_type": "fallback",
                "matched": True,
                "condition": "",
                "reason": "no decision node matched; fallback selected",
            })

        now = datetime.now(timezone.utc)

        initial = DecisionResult(
            trace_id=trace_id,
            flow_id=flow.id,
            flow_version=flow.version,
            selected_node_id=selected.id,
            source_signal_id=signal.id,
            state=RuntimeState.CONFIRMED,
            status=status,
            outcome=outcome,
            confidence=signal.confidence,
            action=selected.action,
            contract_id=selected.contract_id,
            contract_version=selected.config.get("contract_version"),
            signals_used=[signal.name],
            conditions_evaluated=conditions_evaluated,
            conditions_passed=conditions_passed,
            evaluated_at=now,
            created_at=now,
            updated_at=now,
        )

        final, all_boundary_results = self._boundary_engine.apply(signal, flow, initial)

        # Collect boundary evaluation records using condition strings from the flow.
        boundary_node_map: dict[str, DecisionNode] = {
            n.id: n for n in flow.nodes if n.node_type == NodeType.BOUNDARY
        }
        for br in all_boundary_results:
            bnode = boundary_node_map.get(br.boundary_id)
            evaluated_nodes.append({
                "node_id": br.boundary_id,
                "node_type": "boundary",
                "matched": br.triggered,
                "condition": bnode.condition if bnode else None,
                "reason": br.reason,
            })

        if (
            final.status == DecisionStatus.PENDING_HUMAN
            and self._human_gate_manager is not None
        ):
            gate_request = self._human_gate_manager.create_request(final)
            final = final.model_copy(update={"human_gate": gate_request})

        # Strict mode: Ledger is the commit gate.  Must run BEFORE EventBus so
        # execution is never requested for a decision the Ledger rejected.
        if self._ledger_adapter is not None and self._ledger_mode == "strict":
            commit_trace = self._trace_builder.create_trace(signal, flow, final, evaluated_nodes)
            ledger_result = self._ledger_adapter.commit(commit_trace, final)
            _metrics.increment("ledger_append_total", {"result": ledger_result.status.value})
            if ledger_result.status != LedgerCommitStatus.ACCEPTED:
                if ledger_result.status == LedgerCommitStatus.DUPLICATE:
                    prior = self._recover_prior_result(ledger_result.event_ids, signal)
                    if prior is not None:
                        return _record(prior)
                    error_msg = "Ledger commit failed: duplicate — prior result not recoverable"
                else:
                    error_msg = f"Ledger commit failed: {ledger_result.status.value}"
                now = datetime.now(timezone.utc)
                return _record(final.model_copy(update={
                    "status": DecisionStatus.ERROR,
                    "state": RuntimeState.FAILED,
                    "error_message": error_msg,
                    "updated_at": now,
                }))

        # Create ExecutionRequest and dispatch to publishers for confirmed decisions.
        # Skipped in strict mode when ledger rejected (we already returned above).
        if final.status == DecisionStatus.CONFIRMED:
            exec_req = ExecutionRequest(
                execution_id=str(uuid4()),
                decision_id=str(final.id),
                trace_id=str(final.trace_id),
                action=final.action,
            )
            final = final.model_copy(update={"execution_id": exec_req.execution_id})
            runtime_event = RuntimeEvent(
                event_type=EventType.EXECUTION_REQUESTED,
                flow_id=final.flow_id,
                trace_id=final.trace_id,
                decision_id=final.id,
                payload={
                    "execution_id": exec_req.execution_id,
                    "decision_id": str(final.id),
                    "action": final.action,
                },
            )
            # EventBus: internal event record for observability and audit.
            if self._event_bus is not None:
                self._event_bus.publish(runtime_event)
            # ExecutionPublisher: external hand-off to orchestrator.
            if self._execution_publisher is not None:
                self._execution_publisher.publish(runtime_event)

        # In strict mode, persist the finalised result for future DUPLICATE recovery.
        # Only done when the signal carries an idempotency_key and the store is wired.
        if (
            self._ledger_adapter is not None
            and self._ledger_mode == "strict"
            and signal.idempotency_key
            and self._idempotency_store is not None
        ):
            self._idempotency_store.set(signal.idempotency_key, final)

        # Build trace (after EventBus so execution_id is captured) and persist.
        # Parallel mode ledger commit is fire-and-forget: failures never block result.
        if self._trace_store is not None or (
            self._ledger_adapter is not None and self._ledger_mode != "strict"
        ):
            trace = self._trace_builder.create_trace(signal, flow, final, evaluated_nodes)
            if self._trace_store is not None:
                self._trace_store.save(trace)
            if self._ledger_adapter is not None and self._ledger_mode != "strict":
                try:
                    commit_result = self._ledger_adapter.commit(trace, final)
                    _metrics.increment("ledger_append_total", {"result": commit_result.status.value})
                except Exception:
                    pass

        return _record(final)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _recover_prior_result(
        self,
        event_ids: list,
        signal: Signal,
    ) -> DecisionResult | None:
        """Attempt to recover the previously committed DecisionResult for a DUPLICATE.

        Recovery priority:
            1. IdempotencyStore  — fastest; keyed by signal.idempotency_key.
            2. Ledger event → TraceStore — for cases where the in-memory
               idempotency cache was lost (e.g. service restart) but the
               persistent ledger and trace store still hold the record.

        Returns None when recovery is not possible.
        """
        # Priority 1: IdempotencyStore lookup.
        if signal.idempotency_key and self._idempotency_store is not None:
            cached = self._idempotency_store.get(signal.idempotency_key)
            if cached is not None:
                return cached

        # Priority 2: Ledger event → TraceStore lookup.
        if self._ledger_adapter is not None and self._trace_store is not None:
            for event_id in event_ids:
                event = self._ledger_adapter.get_by_event_id(event_id)
                if event is None:
                    continue
                try:
                    trace = self._trace_store.get_by_decision_id(str(event.decision_id))
                    if trace.decision_results:
                        return trace.decision_results[0]
                except (TraceNotFoundError, Exception):
                    continue

        return None

    def _build_context(self, signal: Signal) -> dict[str, Any]:
        """Map Signal fields to the condition evaluation context."""
        return {
            "type": signal.type,
            "confidence": signal.confidence,
            "payload": signal.payload,
            "source": signal.source,
            "created_at": signal.timestamp,
        }

    def _resolve(self, candidates: list[DecisionNode], flow: DecisionFlow) -> DecisionNode:
        """Apply the flow's resolution policy to select one node from the matched candidates.

        Supported strategies:
            priority    — node with the highest ``config.priority`` value wins;
                          ties broken by earlier position in ``flow.nodes``.
            first_match — first node in ``flow.nodes`` declaration order wins.
                          (candidates are already in this order)

        Unknown strategies fall through to ``first_match`` semantics.
        """
        strategy: str = (
            flow.metadata
            .get("resolution_policy", {})
            .get("strategy", "first_match")
        )

        if strategy == "priority":
            node_index: dict[str, int] = {n.id: idx for idx, n in enumerate(flow.nodes)}
            # Primary sort key: higher priority wins.
            # Tie-breaker: smaller index (earlier in flow.nodes) wins.
            return max(
                candidates,
                key=lambda n: (
                    n.priority,
                    -node_index.get(n.id, len(flow.nodes)),
                ),
            )

        # first_match — candidates list already preserves flow.nodes order.
        return candidates[0]

    def _get_fallback(self, flow: DecisionFlow) -> DecisionNode:
        """Return the first active FALLBACK node in the flow.

        Raises:
            RuntimeError: When no active fallback node exists.
        """
        for node in flow.nodes:
            if node.node_type == NodeType.FALLBACK and node.is_active:
                return node
        raise RuntimeError(
            f"Flow '{flow.flow_id}' has no active fallback node; "
            "every flow must contain exactly one fallback node"
        )
