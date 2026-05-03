-- Decision Trace Ledger — append-only event store
-- Ledger is the durable source of truth for all committed decisions.
-- Runtime evaluates; Ledger commits.
--
-- Rules:
--   - No UPDATE or DELETE. This table is append-only.
--   - event_id must be globally unique (enforced by PRIMARY KEY).
--   - sequence_no is per-trace monotonically increasing (enforced by unique constraint).
--   - event_hash chains events within a trace for tamper detection.
--   - prev_hash is NULL only for the first event in a trace.
--
-- Usage:
--   psql $DATABASE_URL -f docs/sql/ledger_events.sql

CREATE TABLE IF NOT EXISTS ledger_events (
    -- Identity
    event_id            UUID            NOT NULL,
    trace_id            UUID            NOT NULL,
    decision_id         UUID            NOT NULL,
    flow_id             UUID            NOT NULL,
    flow_version        TEXT            NOT NULL,

    -- Ordering
    sequence_no         BIGINT          NOT NULL,   -- monotonic per trace_id

    -- Classification
    step_type           TEXT            NOT NULL,   -- signal / decision / boundary / human / action / outcome
    event_type          TEXT            NOT NULL,   -- same as step_type; reserved for future subtyping
    step_id             TEXT            NOT NULL,   -- node_id, signal_id, gate_id, etc.
    actor_type          TEXT            NOT NULL    DEFAULT 'system',
    actor_id            TEXT            NOT NULL    DEFAULT 'runtime',

    -- Timestamps
    occurred_at         TIMESTAMPTZ     NOT NULL,   -- wall-clock time the step occurred
    created_at          TIMESTAMPTZ     NOT NULL    DEFAULT NOW(),

    -- Payload
    payload             JSONB           NOT NULL    DEFAULT '{}',
    metadata            JSONB           NOT NULL    DEFAULT '{}',

    -- Causality / correlation (nullable — populated when known)
    aggregate_id        UUID,           -- defaults to trace_id for decision events
    parent_event_id     UUID,           -- direct parent in causal chain
    causation_id        UUID,           -- root cause event (may equal parent_event_id)
    correlation_id      UUID,           -- groups related events across traces

    -- Policy / boundary references (nullable — populated by boundary events)
    policy_ref          TEXT,           -- identifier of the policy rule that fired
    boundary_ref        TEXT,           -- identifier of the boundary node
    source_ref          TEXT,           -- upstream signal or system that triggered this event

    -- Multi-tenancy
    tenant_id           TEXT            NOT NULL    DEFAULT 'default',

    -- Schema
    schema_version      TEXT            NOT NULL    DEFAULT '1.0',

    -- Integrity chain
    prev_hash           TEXT,           -- SHA-256 hex of the previous event in this trace; NULL for first
    event_hash          TEXT            NOT NULL,   -- SHA-256 hex of canonical event fields + prev_hash

    -- Constraints
    CONSTRAINT pk_ledger_events             PRIMARY KEY (event_id),
    CONSTRAINT uq_ledger_events_trace_seq   UNIQUE (trace_id, sequence_no)
);

-- Indexes

-- Primary access pattern: fetch all events for a trace in sequence order.
CREATE INDEX IF NOT EXISTS idx_ledger_events_trace_seq
    ON ledger_events (trace_id, sequence_no ASC);

-- Aggregate-level event queries (e.g. all events for a decision aggregate).
CREATE INDEX IF NOT EXISTS idx_ledger_events_aggregate_id
    ON ledger_events (aggregate_id)
    WHERE aggregate_id IS NOT NULL;

-- Cross-trace correlation queries.
CREATE INDEX IF NOT EXISTS idx_ledger_events_correlation_id
    ON ledger_events (correlation_id)
    WHERE correlation_id IS NOT NULL;

-- Multi-tenant trace lookup (tenant_id scoped access).
CREATE INDEX IF NOT EXISTS idx_ledger_events_tenant_trace
    ON ledger_events (tenant_id, trace_id);
