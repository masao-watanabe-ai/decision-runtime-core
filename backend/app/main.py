from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from backend.app.config import settings
from backend.app.registry.flow_registry import FlowRegistry
from backend.app.registry.flow_validator import FlowValidationError
from backend.app.integrations.event_bus import EventBus
from backend.app.integrations.execution_publisher import NoopExecutionPublisher
from backend.app.integrations.kafka_execution_publisher import KafkaExecutionPublisher
from backend.app.integrations.ledger_client import LedgerClient
from backend.app.integrations.redis_event_bus import RedisEventBus
from backend.app.integrations.ledger_projector import LedgerProjector
from backend.app.integrations.postgres_ledger_client import PostgresLedgerClient
from backend.app.integrations.runtime_ledger_adapter import RuntimeLedgerAdapter
from backend.app.observability import metrics as _metrics
from backend.app.observability.logging_middleware import StructuredLoggingMiddleware
from backend.app.routes.runtime import router as runtime_router
from backend.app.security_headers import SecurityHeadersMiddleware
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
    app.state.idempotency_store = IdempotencyStore()

    if settings.event_bus_backend == "redis":
        if not settings.redis_url:
            raise RuntimeError(
                "redis_url must be set when event_bus_backend=redis"
            )
        app.state.event_bus = RedisEventBus(settings.redis_url, settings.redis_event_stream)
    else:
        app.state.event_bus = EventBus()

    if settings.execution_publisher_backend == "kafka":
        if not settings.kafka_bootstrap_servers:
            raise RuntimeError(
                "kafka_bootstrap_servers must be set when execution_publisher_backend=kafka"
            )
        app.state.execution_publisher = KafkaExecutionPublisher(
            settings.kafka_bootstrap_servers,
            settings.kafka_execution_topic,
        )
    else:
        app.state.execution_publisher = NoopExecutionPublisher()

    if settings.ledger_enabled:
        if settings.ledger_backend == "postgres":
            if not settings.ledger_database_url:
                raise RuntimeError(
                    "ledger_database_url must be set when ledger_backend=postgres"
                )
            ledger_client: LedgerClient | PostgresLedgerClient = PostgresLedgerClient(
                settings.ledger_database_url
            )
        else:
            ledger_client = LedgerClient()
        app.state.ledger_client = ledger_client
        app.state.ledger_adapter = RuntimeLedgerAdapter(
            ledger_client=ledger_client,
            schema_version=settings.ledger_schema_version,
        )
        app.state.ledger_projector = LedgerProjector(ledger_client)
    else:
        app.state.ledger_client = None
        app.state.ledger_adapter = None
        app.state.ledger_projector = None

    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Decision Runtime Core — production-grade decision execution engine",
    lifespan=lifespan,
)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(StructuredLoggingMiddleware)
app.include_router(runtime_router)


@app.get("/health", tags=["system"])
async def health_check() -> dict[str, str]:
    """Returns service liveness status (legacy; prefer /health/live)."""
    return {"status": "ok"}


@app.get("/health/live", tags=["system"])
async def health_live() -> dict[str, str]:
    """Liveness probe — always returns 200 when the process is running."""
    return {"status": "ok", "type": "live"}


@app.get("/health/ready", tags=["system"])
async def health_ready(request: Request) -> dict[str, Any]:
    """Readiness probe — returns 200 when all dependencies are healthy, 503 otherwise.

    Checks:
        event_bus       — Redis PING (only when backend=redis)
        ledger_client   — SELECT 1 via PostgresLedgerClient (only when ledger_backend=postgres)
        execution_publisher — Kafka producer initialised (only when backend=kafka)
    """
    checks: dict[str, str] = {}
    all_ok = True

    event_bus = request.app.state.event_bus
    if hasattr(event_bus, "is_ready"):
        ok = event_bus.is_ready()
        checks["event_bus"] = "ok" if ok else "error"
        if not ok:
            all_ok = False

    ledger_client = getattr(request.app.state, "ledger_client", None)
    if ledger_client is not None and hasattr(ledger_client, "is_ready"):
        ok = ledger_client.is_ready()
        checks["ledger"] = "ok" if ok else "error"
        if not ok:
            all_ok = False

    execution_publisher = getattr(request.app.state, "execution_publisher", None)
    if execution_publisher is not None and hasattr(execution_publisher, "is_ready"):
        ok = execution_publisher.is_ready()
        checks["execution_publisher"] = "ok" if ok else "error"
        if not ok:
            all_ok = False

    status = "ok" if all_ok else "degraded"
    if not all_ok:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=503,
            content={"status": status, "type": "ready", "checks": checks},
        )
    return {"status": status, "type": "ready", "checks": checks}


@app.get("/metrics", tags=["system"])
async def get_metrics() -> Response:
    """Return runtime metrics in Prometheus text format (version 0.0.4).

    Disabled when metrics_enabled=False or observability_enabled=False.
    """
    if not settings.observability_enabled or not settings.metrics_enabled:
        raise HTTPException(status_code=404, detail="Metrics disabled")
    return Response(
        content=_metrics.prometheus_text(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
