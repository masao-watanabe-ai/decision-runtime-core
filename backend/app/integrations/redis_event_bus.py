"""
Redis Streams-backed EventBus — persistent, replayable event bus for RuntimeEvents.

Replaces the in-memory EventBus when event_bus_backend=redis.
Callers (DecisionRuntimeEngine, route handlers) are unaware of the backend.

Design:
    publish    → XADD  redis_event_stream * event_json <json>
    get_events → XRANGE redis_event_stream <start> + [COUNT n]

Pagination:
    since_id=None        → XRANGE stream - + COUNT limit  (from start)
    since_id="ms-seq"    → XRANGE stream (ms-seq + COUNT limit  (exclusive, Redis 6.2+)

Each stream entry carries a single field "event_json" holding the full
RuntimeEvent serialised as JSON (Pydantic model_dump_json).  The Redis
entry ID (auto-generated "milliseconds-sequence") is stored in
event.metadata["redis_entry_id"] so callers can use it as a pagination cursor.

redis-py is imported lazily so that the module can be imported without the
package being installed.  Constructing a RedisEventBus with no redis_factory
raises RuntimeError (not ImportError) to give the operator a clear message.

Interface parity with EventBus:
    publish(event)                           → same signature
    get_events(since_id=None, limit=None)    → same signature (additive params)
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from backend.app.models.event import RuntimeEvent

_MAX_LIMIT = 1000


class RedisEventBus:
    """Redis Streams-backed EventBus.

    Args:
        redis_url:      Redis connection URL, e.g. redis://localhost:6379.
                        Ignored when redis_factory is provided.
        stream_name:    Redis Streams key to XADD / XRANGE against.
        redis_factory:  Optional callable that returns a Redis client.
                        Inject a MagicMock in tests to avoid a real connection.
    """

    def __init__(
        self,
        redis_url: str,
        stream_name: str,
        redis_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._stream_name = stream_name
        if redis_factory is not None:
            self._client = redis_factory()
        else:
            try:
                import redis  # type: ignore[import]
                self._client = redis.from_url(redis_url, decode_responses=True)
            except ImportError as exc:
                raise RuntimeError(
                    "redis is required for the redis event bus backend. "
                    "Install it with: pip install redis"
                ) from exc

    # ------------------------------------------------------------------ #
    # Public interface (matches EventBus)                                  #
    # ------------------------------------------------------------------ #

    def publish(self, event: RuntimeEvent) -> None:
        """Append the event to the Redis stream via XADD."""
        self._client.xadd(
            self._stream_name,
            {"event_json": event.model_dump_json()},
        )

    def is_ready(self) -> bool:
        """Return True if the Redis connection is healthy (PING succeeds)."""
        try:
            self._client.ping()
            return True
        except Exception:
            return False

    def get_events(
        self,
        since_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[RuntimeEvent]:
        """Return events from the stream in publication order (oldest first).

        Args:
            since_id: Redis stream entry ID to use as an exclusive cursor.
                      When provided, returns events published AFTER that entry.
                      Stored in each returned event's metadata["redis_entry_id"].
            limit:    Maximum events to return.  Capped at 1000.
                      None passes count=None to XRANGE (no server-side limit).
        """
        if since_id is None:
            start = "-"
        else:
            start = f"({since_id}"  # exclusive range (Redis 6.2+)

        effective_limit = min(limit, _MAX_LIMIT) if limit is not None else None
        entries = self._client.xrange(self._stream_name, start, "+", count=effective_limit)

        result: list[RuntimeEvent] = []
        for entry_id, fields in entries:
            raw = fields.get("event_json") or fields.get(b"event_json", "")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if raw:
                event = RuntimeEvent.model_validate_json(raw)
                # Inject Redis entry_id into metadata so callers can use it as a cursor.
                event = event.model_copy(
                    update={"metadata": {**event.metadata, "redis_entry_id": entry_id}}
                )
                result.append(event)
        return result
