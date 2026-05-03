"""
PostgreSQL-backed LedgerClient — append-only event store for Decision Trace Ledger Core v2.

Implements the same interface as the in-memory LedgerClient so that callers
(RuntimeLedgerAdapter, LedgerProjector) are unaware of the storage backend.

Design constraints:
    - Append-only: append() inserts; no UPDATE or DELETE is ever issued.
    - event_id globally unique: duplicate appends return DUPLICATE without writing.
    - sequence_no is per-trace monotonically increasing (computed inside a transaction).
    - prev_hash chains events within a trace; event_hash seals the record.
    - psycopg2 is imported lazily so that the module can be imported without the
      package being installed (only the postgres backend path requires it).

Connection management:
    Each public method opens one connection, performs its work, commits (for writes),
    then closes. For high-throughput production use, replace _connect() with a
    connection pool (e.g. psycopg2.pool.ThreadedConnectionPool).

Swap-in compatibility:
    PostgresLedgerClient has the same four public methods as LedgerClient:
        append / get_by_event_id / get_events_by_trace_id / get_events
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from uuid import UUID

from backend.app.integrations.ledger_client import (
    LedgerAppendResult,
    LedgerAppendStatus,
    LedgerEvent,
)

# Columns returned by every SELECT query — must match _row_to_event() parameter order.
_SELECT_COLS = (
    "event_id, trace_id, decision_id, flow_id, flow_version, "
    "step_type, step_id, payload, occurred_at, schema_version"
)


class PostgresLedgerClient:
    """Append-only PostgreSQL-backed ledger client.

    Args:
        database_url:       psycopg2-compatible DSN (ignored when connection_factory given).
        connection_factory: Optional callable that returns a DB-API 2.0 connection.
                            Inject a mock factory in tests to avoid a real Postgres instance.
    """

    def __init__(
        self,
        database_url: str,
        connection_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._database_url = database_url
        self._connection_factory = connection_factory

    # ------------------------------------------------------------------ #
    # Public interface (matches LedgerClient)                              #
    # ------------------------------------------------------------------ #

    def append(self, event: LedgerEvent) -> LedgerAppendResult:
        """Append one event to the ledger.

        Duplicate event_ids return DUPLICATE without writing.
        All reads (duplicate check, sequence_no, prev_hash) and the INSERT
        run inside a single transaction to ensure monotonic ordering.
        """
        conn = self._connect()
        cur = conn.cursor()
        try:
            # 1. Duplicate check — returns early without holding locks long.
            cur.execute(
                "SELECT 1 FROM ledger_events WHERE event_id = %s",
                (str(event.event_id),),
            )
            if cur.fetchone() is not None:
                conn.rollback()
                return LedgerAppendResult(
                    status=LedgerAppendStatus.DUPLICATE,
                    event_id=event.event_id,
                )

            # 2. Next sequence_no for this trace (0-based; first event → 1).
            cur.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) + 1 "
                "FROM ledger_events WHERE trace_id = %s",
                (str(event.trace_id),),
            )
            sequence_no: int = cur.fetchone()[0]

            # 3. Hash of the previous event in this trace (for chain integrity).
            cur.execute(
                "SELECT event_hash FROM ledger_events "
                "WHERE trace_id = %s ORDER BY sequence_no DESC LIMIT 1",
                (str(event.trace_id),),
            )
            row = cur.fetchone()
            prev_hash: Optional[str] = row[0] if row is not None else None

            # 4. Compute this event's hash.
            event_hash = compute_event_hash(event, sequence_no, prev_hash)

            # 5. Insert (append-only: no ON CONFLICT clause).
            cur.execute(
                """
                INSERT INTO ledger_events (
                    event_id, trace_id, decision_id, flow_id, flow_version,
                    sequence_no, step_type, event_type, step_id,
                    actor_type, actor_id, occurred_at,
                    payload, metadata,
                    aggregate_id, tenant_id, schema_version,
                    prev_hash, event_hash
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s, %s,
                    %s, %s
                )
                """,
                (
                    str(event.event_id),
                    str(event.trace_id),
                    str(event.decision_id),
                    str(event.flow_id),
                    event.flow_version,
                    sequence_no,
                    str(event.step_type),
                    str(event.step_type),   # event_type mirrors step_type
                    event.step_id,
                    "system",
                    "runtime",
                    event.occurred_at,
                    json.dumps(event.payload),
                    "{}",
                    str(event.trace_id),    # aggregate_id defaults to trace_id
                    "default",
                    event.schema_version,
                    prev_hash,
                    event_hash,
                ),
            )
            conn.commit()
            return LedgerAppendResult(
                status=LedgerAppendStatus.ACCEPTED,
                event_id=event.event_id,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()

    def get_by_event_id(self, event_id: UUID) -> Optional[LedgerEvent]:
        """Return the event with the given event_id, or None if not found."""
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute(
                f"SELECT {_SELECT_COLS} FROM ledger_events WHERE event_id = %s",
                (str(event_id),),
            )
            row = cur.fetchone()
            return _row_to_event(row) if row is not None else None
        finally:
            cur.close()
            conn.close()

    def get_events_by_trace_id(self, trace_id: str) -> list[LedgerEvent]:
        """Return all events for trace_id in ascending sequence_no order."""
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute(
                f"SELECT {_SELECT_COLS} FROM ledger_events "
                "WHERE trace_id = %s ORDER BY sequence_no ASC",
                (trace_id,),
            )
            return [_row_to_event(row) for row in cur.fetchall()]
        finally:
            cur.close()
            conn.close()

    def get_events(self) -> list[LedgerEvent]:
        """Return all stored events in created_at order (oldest first)."""
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute(
                f"SELECT {_SELECT_COLS} FROM ledger_events ORDER BY created_at ASC"
            )
            return [_row_to_event(row) for row in cur.fetchall()]
        finally:
            cur.close()
            conn.close()

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def is_ready(self) -> bool:
        """Return True if a DB connection can be opened and SELECT 1 succeeds."""
        try:
            conn = self._connect()
            cur = conn.cursor()
            try:
                cur.execute("SELECT 1")
                return True
            finally:
                cur.close()
                conn.close()
        except Exception:
            return False

    def _connect(self) -> Any:
        """Return a new DB-API 2.0 connection.

        Uses the injected connection_factory when present; otherwise opens a
        psycopg2 connection from database_url.  psycopg2 is imported lazily
        so that the module loads cleanly when psycopg2 is not installed.
        """
        if self._connection_factory is not None:
            return self._connection_factory()
        try:
            import psycopg2  # type: ignore[import]
            return psycopg2.connect(self._database_url)
        except ImportError as exc:
            raise ImportError(
                "psycopg2-binary is required for the postgres ledger backend. "
                "Install it with: pip install psycopg2-binary"
            ) from exc


# ------------------------------------------------------------------ #
# Module-level helpers (exported for testing)                          #
# ------------------------------------------------------------------ #


def compute_event_hash(
    event: LedgerEvent,
    sequence_no: int,
    prev_hash: Optional[str],
) -> str:
    """Return a SHA-256 hex digest of the canonical event representation.

    The canonical form is a deterministically serialised JSON object covering
    the identity and ordering fields.  Payload is intentionally excluded: it
    may contain mutable runtime context that differs between environments.
    """
    canonical: dict[str, Any] = {
        "event_id": str(event.event_id),
        "trace_id": str(event.trace_id),
        "decision_id": str(event.decision_id),
        "step_type": str(event.step_type),
        "step_id": event.step_id,
        "sequence_no": sequence_no,
        "prev_hash": prev_hash,
    }
    serialised = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialised.encode("utf-8")).hexdigest()


def _row_to_event(row: tuple) -> LedgerEvent:
    """Convert a DB row (in _SELECT_COLS order) to a LedgerEvent.

    Handles both string and UUID values for UUID columns (psycopg2 may return
    either depending on configuration; mocks typically return strings or UUIDs).
    Handles both dict and JSON-string values for JSONB columns.
    """
    (
        event_id, trace_id, decision_id, flow_id, flow_version,
        step_type, step_id, payload, occurred_at, schema_version,
    ) = row

    if isinstance(payload, str):
        payload = json.loads(payload)
    elif payload is None:
        payload = {}

    def _uuid(v: Any) -> UUID:
        return v if isinstance(v, UUID) else UUID(str(v))

    if not isinstance(occurred_at, datetime):
        occurred_at = datetime.fromisoformat(str(occurred_at)).replace(tzinfo=timezone.utc)

    return LedgerEvent(
        event_id=_uuid(event_id),
        schema_version=str(schema_version),
        trace_id=_uuid(trace_id),
        decision_id=_uuid(decision_id),
        flow_id=_uuid(flow_id),
        flow_version=str(flow_version),
        step_type=str(step_type),
        step_id=str(step_id),
        payload=payload,
        occurred_at=occurred_at,
    )
