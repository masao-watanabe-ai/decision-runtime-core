# Features — v1.0

## Core capabilities

### Deterministic decision evaluation

The engine evaluates signals against YAML-defined flows. Identical inputs always produce identical decision paths (excluding auto-generated UUIDs). Condition expressions use a strict AST whitelist — no `eval()`, no `exec()`.

Supported condition operators: `==`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `not in`, `and`, `or`, `not`.

Context variables available in conditions: `type`, `confidence`, `payload.*`, `source`, `created_at`.

### Declarative flow definitions

Decision logic lives in YAML files, not code. Flows define:
- `DECISION` nodes — conditions that select the winning action
- `BOUNDARY` nodes — rules that block, override, escalate, or redirect
- `HUMAN_GATE` nodes — escalation to human review

Flows support `first_match` and `priority` selection strategies. See [docs/flow-format.md](flow-format.md).

### Boundary enforcement

Boundary nodes enforce business rules at evaluation time, not execution time. Effects are applied in severity order (`critical > high > medium > low`):

| Effect | Result |
|---|---|
| `allow` | No change — decision proceeds |
| `block` | `status = blocked`, `action = null` |
| `escalate` | `status = pending_human` — suspended for review |
| `override` | Replace action and selected node |
| `redirect` | Replace action and selected node (semantic alias) |

### Human-in-the-loop approval

When a boundary escalates a decision, a `HumanGateRequest` is created with the full context. Reviewers call `POST /approve` or `POST /reject`. The gate supports:

- `required_role` — only actors with the specified role may resolve the gate
- `comment` — optional reviewer note (recorded in Ledger)
- Idempotent Ledger recording of the approval/rejection event

### Idempotent execution

Signals carrying an `idempotency_key` return the cached `DecisionResult` on repeated calls. No duplicate events, no duplicate traces, no duplicate ledger appends.

Recovery priority:
1. `IdempotencyStore` (fast in-memory lookup by key)
2. Ledger → `LedgerProjector` (for cross-restart recovery in strict mode)

### Full decision trace

Every evaluation produces a `DecisionTrace` capturing:
- Signal metadata
- All evaluated nodes (matched and unmatched)
- All triggered boundaries
- Human gate resolution (if applicable)
- Final `DecisionResult`

Traces are queryable by `trace_id` or `decision_id`. A structured explanation is available at `GET /decision/{id}/explain`.

### Ledger-backed decision commit

The `RuntimeLedgerAdapter` converts each trace into a sequence of `LedgerEvent` objects appended to the ledger in step order: signal → decision → boundary → human → action → outcome.

Each event carries:
- Deterministic `event_id` = `uuid5(namespace, "{trace_id}:{step_type}:{step_id}")`
- `prev_hash` — SHA-256 of the previous event in the trace
- `event_hash` — SHA-256 of the canonical JSON of this event

This produces a per-trace hash chain that detects any tampering.

Two commit modes:
- `parallel` (default): ledger write failures are swallowed; the `DecisionResult` is always returned
- `strict`: `ACCEPTED` is required before `EventBus.publish()`; failures return `status = error`

---

## Integrations

### PostgreSQL Ledger (`LEDGER_BACKEND=postgres`)

Append-only `ledger_events` table. Full DDL in `docs/sql/ledger_events.sql`.

- Duplicate `event_id` → `DUPLICATE` (idempotent re-append)
- `sequence_no` per trace computed inside a transaction
- `get_events_by_trace_id()` for replay and projection

### Redis Streams EventBus (`EVENT_BUS_BACKEND=redis`)

- `XADD` on publish; `XRANGE` on read
- Cursor pagination via `since_id` (exclusive range, Redis 6.2+)
- Entry ID stored in `event.metadata["redis_entry_id"]` for use as pagination cursor
- `is_ready()` via `PING`

### Kafka ExecutionPublisher (`EXECUTION_PUBLISHER_BACKEND=kafka`)

- Only `EXECUTION_REQUESTED` events are forwarded (all others silently dropped)
- Key = `flow_id` (partitions by flow for ordering)
- Value = full `RuntimeEvent` JSON
- `send() + flush()` per event (at-least-once delivery)
- `is_ready()` checks producer initialisation

---

## Observability

### Prometheus metrics (`GET /metrics`)

Text format 0.0.4. Scraped by Prometheus or compatible systems.

| Metric | Type | Labels |
|---|---|---|
| `decision_evaluate_total` | counter | `status` (confirmed, fallback, error, pending_human, blocked, rejected) |
| `decision_evaluate_duration_seconds` | summary | — (count + sum) |
| `human_gate_action_total` | counter | `action` (approve, reject) |
| `ledger_append_total` | counter | `result` (accepted, duplicate, failed, partial, invalid) |

Only incremented on success — 403/404 gate actions do not count.

### Structured JSON logging

Every request produces one log line:

```json
{
  "request_id": "uuid",
  "method": "POST",
  "path": "/api/runtime/evaluate",
  "status_code": 200,
  "duration_ms": 12.4
}
```

`4xx`/`5xx` at `WARNING`; `2xx`/`3xx` at `INFO`. Never logs request/response bodies or `X-Api-Key`.

### Health probes

| Endpoint | What it checks |
|---|---|
| `GET /health/live` | Process is alive (always 200) |
| `GET /health/ready` | Redis PING / Postgres `SELECT 1` / Kafka producer — 503 if any fail |

---

## Security

| Feature | Behaviour |
|---|---|
| AST-safe conditions | Whitelist-only; no arbitrary code execution |
| Security headers | `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Content-Security-Policy`, `Cache-Control: no-store` on all responses |
| X-Api-Key auth | Configurable key→actor→roles map; required on human gate actions when `AUTH_ENABLED=true` |
| RBAC | `required_role` on flow nodes; HTTP 403 if actor lacks role |

---

## Limitations (v1.0)

| Limitation | Detail |
|---|---|
| **Single-node only** | `TraceStore`, `HumanGateManager`, and `IdempotencyStore` are in-process. No HA, no distributed consensus. |
| **No multi-region** | All state is local to one instance. |
| **No flow hot-reload** | Flows are loaded at startup. Restart required to pick up changes. |
| **No rate limiting** | Add a reverse proxy (nginx, Envoy, API Gateway) for RPS caps. |
| **PostgreSQL only** | No MySQL, MongoDB, or other ledger backends in v1.0. |
| **Redis 6.2+** | Exclusive-range `XRANGE` requires Redis 6.2 or later. |
| **No gRPC / GraphQL** | HTTP/JSON only. |
| **No multi-tenancy** | Single-tenant per instance. |

---

## Test coverage

303 tests as of v1.0.0-rc1, covering:

- Decision evaluation (unit + E2E)
- Boundary engine effects
- Human gate lifecycle (create / approve / reject / role enforcement)
- Ledger adapter (all StepTypes, strict/parallel modes, duplicate handling)
- Ledger projection (replay → DecisionTrace reconstruction)
- PostgreSQL ledger client (append, duplicate, sequence, hash chain)
- Redis EventBus (publish, get_events, pagination, entry_id injection)
- Kafka ExecutionPublisher (publish, filter, producer injection)
- Idempotency store
- Auth / RBAC (401, 403, 200)
- Observability (metrics counters, logging middleware, endpoint)
- EventBus pagination (InMemory + Redis mock)
- Runtime stats endpoint
- Production hardening (health probes, security headers, is_ready())
