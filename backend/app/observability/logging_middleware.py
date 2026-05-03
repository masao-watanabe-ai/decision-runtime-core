"""ASGI middleware for structured JSON access logging.

Logs each HTTP request as a single JSON record containing:
    request_id    auto-generated UUID (scoped to this request)
    method        HTTP verb
    path          URL path (no query string)
    status_code   HTTP response status
    duration_ms   Wall-clock time for the request in milliseconds

Auth headers (X-Api-Key) and request/response bodies are never logged.
When structured_logging_enabled=False in settings, the middleware is a no-op.
"""
from __future__ import annotations

import json
import logging
import time
from uuid import uuid4

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("decision_runtime.access")


class StructuredLoggingMiddleware:
    """Log every HTTP request as a structured JSON access record."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        from backend.app.config import settings  # lazy to allow monkeypatch in tests

        if scope["type"] != "http" or not settings.structured_logging_enabled:
            await self.app(scope, receive, send)
            return

        request_id = str(uuid4())
        start = time.monotonic()
        status_code: int = 500

        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.monotonic() - start) * 1000, 2)
            log_entry = {
                "request_id": request_id,
                "method": scope.get("method", ""),
                "path": scope.get("path", ""),
                "status_code": status_code,
                "duration_ms": duration_ms,
            }
            level = logging.WARNING if status_code >= 400 else logging.INFO
            logger.log(level, json.dumps(log_entry))
