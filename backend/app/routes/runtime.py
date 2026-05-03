from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.app.integrations.event_bus import EventBus
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

    actor_id: str = Field(..., min_length=1, description="Identifier of the reviewer taking action")
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

    Returns 404 if the trace is not found.
    """
    try:
        return request.app.state.trace_store.get(trace_id)
    except TraceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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


@router.get(
    "/events",
    response_model=list[RuntimeEvent],
    summary="List all runtime events (development / debugging)",
)
async def list_events(request: Request) -> list[RuntimeEvent]:
    """Return all RuntimeEvents stored in the in-memory EventBus.

    Events are returned in publication order (oldest first).
    This endpoint is intended for development and debugging; it exposes
    the full in-memory event log without pagination.
    """
    return request.app.state.event_bus.get_events()


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
    Returns 404 if the request_id is not found.
    Returns 409 if the request is not in PENDING state.
    """
    try:
        return request.app.state.human_gate_manager.approve(
            request_id, body.actor_id, body.comment
        )
    except HumanGateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HumanGateInvalidStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
    Returns 404 if the request_id is not found.
    Returns 409 if the request is not in PENDING state.
    """
    try:
        return request.app.state.human_gate_manager.reject(
            request_id, body.actor_id, body.comment
        )
    except HumanGateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HumanGateInvalidStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
