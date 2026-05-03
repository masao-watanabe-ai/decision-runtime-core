from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from backend.app.config import settings
from backend.app.registry.flow_registry import FlowRegistry
from backend.app.registry.flow_validator import FlowValidationError
from backend.app.integrations.event_bus import EventBus
from backend.app.routes.runtime import router as runtime_router
from backend.app.runtime.human_gate_manager import HumanGateManager
from backend.app.runtime.idempotency_store import IdempotencyStore
from backend.app.runtime.trace_store import TraceStore


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize shared singletons at startup."""
    registry = FlowRegistry(settings.flow_dir)
    try:
        registry.load_all()
    except FlowValidationError as exc:
        raise RuntimeError(f"Startup aborted — flow validation failed: {exc}") from exc
    app.state.flow_registry = registry
    app.state.human_gate_manager = HumanGateManager()
    app.state.trace_store = TraceStore()
    app.state.event_bus = EventBus()
    app.state.idempotency_store = IdempotencyStore()
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Decision Runtime Core — production-grade decision execution engine",
    lifespan=lifespan,
)

app.include_router(runtime_router)


@app.get("/health", tags=["system"])
async def health_check() -> dict[str, str]:
    """Returns service liveness status."""
    return {"status": "ok"}
