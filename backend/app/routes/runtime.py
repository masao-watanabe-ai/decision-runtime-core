from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.app.auth import get_actor
from backend.app.config import settings
from backend.app.integrations.event_bus import EventBus
from backend.app.integrations.ledger_client import LedgerAppendStatus
from backend.app.observability import metrics as _metrics
from backend.app.models.decision import DecisionResult
from backend.app.models.event import RuntimeEvent
from backend.app.models.flow import DecisionFlow
from backend.app.models.signal import Signal, SignalValueType
from backend.app.models.trace import DecisionTrace
from backend.app.registry.flow_validator import FlowNotFoundError
from backend.app.runtime.condition_evaluator import ConditionEvaluationError
from backend.app.runtime.engine import DecisionRuntimeEngine
from backend.app.runtime.explanation_builder import ExplanationBuilder
from backend.app.runtime.human_gate_manager import (
    HumanGateInsufficientRoleError,
    HumanGateInvalidStateError,
    HumanGateNotFoundError,
)
from backend.app.runtime.trace_store import TraceNotFoundError

router = APIRouter(prefix="/api/runtime", tags=["runtime"])


# ------------------------------------------------------------------ #
# Request / response models                                            #
# ------------------------------------------------------------------ #


class EvaluateSignalInput(BaseModel):
    """Signal data embedded in an evaluate request.

    Fields are a superset of the Signal model to accommodate API-level
    identifiers (signal_id, event_id, idempotency_key) without requiring
    callers to supply internal fields like value_type.
    """

    signal_id: Optional[str] = Field(None, description="Optional caller-supplied signal UUID")
    event_id: Optional[str] = Field(None, description="Optional originating event identifier")
    name: Optional[str] = Field(None, description="Signal name; defaults to type when omitted")
    type: str = Field("", description="Domain type classification of the signal")
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = Field("api", min_length=1, description="Origin of the signal")
    created_at: Optional[datetime] = Field(None, description="Signal emission timestamp; defaults to now")
    idempotency_key: Optional[str] = Field(
        None,
        description="When present, repeated calls with this key return the cached result",
    )


class EvaluateRequest(BaseModel):
    """Request body for POST /api/runtime/evaluate."""

    flow_id: str = Field(..., min_length=1, description="ID of the flow to evaluate")
    flow_version: Optional[str] = Field(
        None,
        description="Semantic version; omit to use the latest loaded version",
    )
    signal: EvaluateSignalInput


class GateActionBody(BaseModel):
    """Request body for human gate approve and reject actions."""

    actor_id: Optional[str] = Field(None, description="Reviewer identifier; required when auth_enabled=False")
    comment: Optional[str] = Field(None, max_length=4096, description="Optional reviewer note")


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _build_signal(sig: EvaluateSignalInput) -> Signal:
    """Convert an EvaluateSignalInput into a Signal for engine evaluation."""
    signal_id: Optional[UUID] = None
    if sig.signal_id:
        try:
            signal_id = UUID(sig.signal_id)
        except ValueError:
            pass

    kwargs: dict[str, Any] = {
        "name": sig.name or sig.type or "api_signal",
        "type": sig.type,
        "value_type": SignalValueType.JSON,
        "confidence": sig.confidence,
        "payload": sig.payload,
        "source": sig.source,
        "timestamp": sig.created_at or datetime.now(timezone.utc),
        "idempotency_key": sig.idempotency_key,
    }
    if signal_id is not None:
        kwargs["id"] = signal_id
    return Signal(**kwargs)


# ------------------------------------------------------------------ #
# Routes                                                               #
# ------------------------------------------------------------------ #


@router.post(
    "/evaluate",
    response_model=DecisionResult,
    summary="Evaluate a signal against a decision flow",
)
async def evaluate(body: EvaluateRequest, request: Request) -> DecisionResult:
    """Run the decision runtime pipeline for a single signal against the named flow.

    Steps:
        1. Return cached result if idempotency_key already seen.
        2. Load flow from registry (404 if not found).
        3. Build a Signal from the request body.
        4. Evaluate via DecisionRuntimeEngine (wires trace, human gate, event bus).
        5. Cache result under idempotency_key when provided.
        6. Return DecisionResult.

    Returns 404 when the flow is not found.
    Returns 400 when a condition expression is invalid.
    Returns 500 on unexpected runtime errors.
    """
    idempotency_key = body.signal.idempotency_key
    if idempotency_key:
        cached = request.app.state.idempotency_store.get(idempotency_key)
        if cached is not None:
            return cached

    try:
        flow = request.app.state.flow_registry.get(body.flow_id, body.flow_version)
    except FlowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    signal = _build_signal(body.signal)

    engine = DecisionRuntimeEngine(
        human_gate_manager=request.app.state.human_gate_manager,
        trace_store=request.app.state.trace_store,
        event_bus=request.app.state.event_bus,
        ledger_adapter=getattr(request.app.state, "ledger_adapter", None),
        ledger_mode=settings.ledger_mode,
        idempotency_store=getattr(request.app.state, "idempotency_store", None),
        execution_publisher=getattr(request.app.state, "execution_publisher", None),
    )
    try:
        result = engine.evaluate(signal, flow)
    except ConditionEvaluationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected runtime error: {exc}") from exc

    if idempotency_key:
        request.app.state.idempotency_store.set(idempotency_key, result)

    return result


@router.get(
    "/flows",
    response_model=list[DecisionFlow],
    summary="List all loaded flows",
)
async def list_flows(request: Request) -> list[DecisionFlow]:
    """Return every flow currently loaded in the registry."""
    return request.app.state.flow_registry.list_flows()


@router.get(
    "/flows/{flow_id}",
    response_model=DecisionFlow,
    summary="Get a flow by ID",
)
async def get_flow(
    flow_id: str,
    request: Request,
    version: Optional[str] = Query(
        None,
        description="Semantic version (MAJOR.MINOR.PATCH); omit to receive the latest version",
    ),
) -> DecisionFlow:
    """Return the flow matching the given ID and optional version.

    Returns 404 if no matching flow is found.
    """
    try:
        return request.app.state.flow_registry.get(flow_id, version)
    except FlowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/traces/{trace_id}",
    response_model=DecisionTrace,
    summary="Get a decision trace by ID",
)
async def get_trace(trace_id: str, request: Request) -> DecisionTrace:
    """Return the DecisionTrace for the given trace_id.

    Falls back to the LedgerProjector when the in-memory TraceStore is cold
    (e.g. after a service restart).  Returns 404 if neither source has a trace.
    """
    try:
        return request.app.state.trace_store.get(trace_id)
    except TraceNotFoundError:
        pass

    ledger_projector = getattr(request.app.state, "ledger_projector", None)
    if ledger_projector is not None:
        projected = ledger_projector.project(trace_id)
        if projected is not None:
            return projected

    raise HTTPException(status_code=404, detail=f"Trace '{trace_id}' not found")


@router.get(
    "/decision/{decision_id}/explain",
    summary="Get a structured explanation for a decision",
)
async def explain_decision(
    decision_id: str, request: Request
) -> dict[str, Any]:
    """Return a human-readable explanation of the decision identified by decision_id.

    Returns 404 if no trace is found for the given decision_id.
    """
    try:
        trace = request.app.state.trace_store.get_by_decision_id(decision_id)
    except TraceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ExplanationBuilder().build(trace)


class RuntimeStats(BaseModel):
    """Aggregated decision outcome counts from the in-memory TraceStore."""

    total: int
    confirmed: int
    fallback: int
    error: int
    pending_human: int
    rejected: int
    blocked: int
    confirmed_rate: float


@router.get(
    "/events",
    response_model=list[RuntimeEvent],
    summary="List runtime events with optional cursor pagination",
)
async def list_events(
    request: Request,
    since_id: Optional[str] = Query(
        None,
        description="Cursor — return events published after this event ID (exclusive)",
    ),
    limit: int = Query(
        100,
        ge=1,
        description="Maximum events to return (1–1000)",
    ),
) -> list[RuntimeEvent]:
    """Return RuntimeEvents in publication order (oldest first).

    Pagination:
        Pass ``since_id`` from the last received event's ``id`` (InMemory backend)
        or ``metadata.redis_entry_id`` (Redis backend) to fetch the next page.
        Unknown ``since_id`` values fall back silently to the start of the stream.
        ``limit`` is capped server-side at 1 000.
    """
    effective_limit = min(limit, 1000)
    return request.app.state.event_bus.get_events(since_id=since_id, limit=effective_limit)


@router.get(
    "/stats",
    response_model=RuntimeStats,
    summary="Aggregated decision outcome statistics",
)
async def get_stats(
    request: Request,
    limit: int = Query(
        1000,
        ge=1,
        description="Number of most-recent traces to include in the aggregation (1–10 000)",
    ),
) -> RuntimeStats:
    """Return decision outcome counts aggregated from the in-memory TraceStore.

    Stats are computed on-demand from the TraceStore (read model).
    They are independent of the Prometheus metrics registry.
    Returns zeros when the TraceStore is empty.
    ``limit`` is capped server-side at 10 000.
    """
    effective_limit = min(limit, 10_000)
    traces = request.app.state.trace_store.list_recent(limit=effective_limit)

    confirmed = fallback = error = pending_human = rejected = blocked = 0

    for trace in traces:
        for result in trace.decision_results:
            s = result.status.value
            if s == "confirmed":
                confirmed += 1
            elif s == "fallback":
                fallback += 1
            elif s == "error":
                error += 1
            elif s == "pending_human":
                pending_human += 1
            elif s == "rejected":
                rejected += 1
            elif s == "blocked":
                blocked += 1

    total = confirmed + fallback + error + pending_human + rejected + blocked
    confirmed_rate = confirmed / total if total > 0 else 0.0

    return RuntimeStats(
        total=total,
        confirmed=confirmed,
        fallback=fallback,
        error=error,
        pending_human=pending_human,
        rejected=rejected,
        blocked=blocked,
        confirmed_rate=round(confirmed_rate, 4),
    )


def _append_human_gate_to_ledger(
    request: Request,
    result: DecisionResult,
    request_id: str,
    actor_id: str,
    actor_roles: list[str] | None,
    action: str,
    comment: str | None,
) -> None:
    """Append a human gate resolution event to the ledger.

    In parallel mode (default), ledger failures are silently swallowed so the
    approve/reject response is never blocked.  In strict mode, a ledger failure
    raises HTTPException(500) — the action already committed to the manager, so
    callers should be aware this is a best-effort consistency guarantee.
    """
    ledger_adapter = getattr(request.app.state, "ledger_adapter", None)
    if ledger_adapter is None:
        return

    gate_req = request.app.state.human_gate_manager.get_request(request_id)
    occurred_at = datetime.now(timezone.utc)

    try:
        append_result = ledger_adapter.append_human_gate_event(
            gate_request=gate_req,
            decision_result=result,
            actor_id=actor_id,
            action=action,
            occurred_at=occurred_at,
            actor_roles=actor_roles,
            comment=comment,
        )
        _metrics.increment("ledger_append_total", {"result": append_result.status.value})
        if (
            settings.ledger_mode == "strict"
            and append_result.status == LedgerAppendStatus.INVALID
        ):
            raise HTTPException(
                status_code=500,
                detail=f"Ledger append failed for human gate {action} event",
            )
    except HTTPException:
        raise
    except Exception:
        if settings.ledger_mode == "strict":
            raise HTTPException(
                status_code=500,
                detail=f"Ledger append failed for human gate {action} event",
            )


@router.post(
    "/human-gates/{request_id}/approve",
    response_model=DecisionResult,
    summary="Approve a pending human gate request",
)
async def approve_gate(
    request_id: str,
    body: GateActionBody,
    request: Request,
) -> DecisionResult:
    """Approve a pending HumanGateRequest.

    Transitions the linked DecisionResult from pending_human → confirmed.
    When auth_enabled=True, the X-Api-Key header is used for actor identity and role lookup.
    When ledger is enabled, records the approval as an immutable human gate event.
    Returns 401 if auth_enabled=True and the key is missing or invalid.
    Returns 403 if the actor does not hold the gate's required_role.
    Returns 404 if the request_id is not found.
    Returns 409 if the request is not in PENDING state.
    Returns 500 if ledger_mode=strict and the ledger append fails.
    """
    actor = get_actor(request)
    if actor is not None:
        actor_id = actor.actor_id
        actor_roles: list[str] | None = actor.roles
    else:
        actor_id = body.actor_id or "anonymous"
        actor_roles = None

    try:
        result = request.app.state.human_gate_manager.approve(
            request_id, actor_id, body.comment, actor_roles=actor_roles
        )
    except HumanGateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HumanGateInvalidStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HumanGateInsufficientRoleError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    _metrics.increment("human_gate_action_total", {"action": "approve"})
    _append_human_gate_to_ledger(request, result, request_id, actor_id, actor_roles, "approve", body.comment)
    return result


@router.post(
    "/human-gates/{request_id}/reject",
    response_model=DecisionResult,
    summary="Reject a pending human gate request",
)
async def reject_gate(
    request_id: str,
    body: GateActionBody,
    request: Request,
) -> DecisionResult:
    """Reject a pending HumanGateRequest.

    Transitions the linked DecisionResult from pending_human → rejected
    and clears the action payload.
    When auth_enabled=True, the X-Api-Key header is used for actor identity and role lookup.
    When ledger is enabled, records the rejection as an immutable human gate event.
    Returns 401 if auth_enabled=True and the key is missing or invalid.
    Returns 403 if the actor does not hold the gate's required_role.
    Returns 404 if the request_id is not found.
    Returns 409 if the request is not in PENDING state.
    Returns 500 if ledger_mode=strict and the ledger append fails.
    """
    actor = get_actor(request)
    if actor is not None:
        actor_id = actor.actor_id
        actor_roles_: list[str] | None = actor.roles
    else:
        actor_id = body.actor_id or "anonymous"
        actor_roles_ = None

    try:
        result = request.app.state.human_gate_manager.reject(
            request_id, actor_id, body.comment, actor_roles=actor_roles_
        )
    except HumanGateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HumanGateInvalidStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HumanGateInsufficientRoleError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    _metrics.increment("human_gate_action_total", {"action": "reject"})
    _append_human_gate_to_ledger(request, result, request_id, actor_id, actor_roles_, "reject", body.comment)
    return result
