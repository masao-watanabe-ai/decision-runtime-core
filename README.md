# Decision Runtime Core

**AI is not prediction. It is decision.**

---

## What this is

The Decision Runtime Core is a **Decision OS Kernel** — a production-grade runtime that sits between AI signal producers and execution systems.

AI models output signals: probabilities, classifications, scores. Those are not decisions. A decision is an accountable, traceable, governed act — one that routes a customer, approves a transaction, or holds a shipment. This runtime turns signals into decisions.

```
[ Signal Source ]           e.g. AI model, user event, sensor
        ↓
[ Decision Runtime Engine ] evaluate → boundary → human gate
        ↓
[ Ledger (Postgres) ]       append-only commit of every decision fact
        ↓
[ EventBus (Redis Streams) ] publish confirmed decisions
        ↓
[ ExecutionPublisher (Kafka) ] hand off to external orchestrator
        ↓
[ External Orchestrator ]   act on confirmed decisions
```

Full architecture: [docs/architecture-runtime.md](docs/architecture-runtime.md)

---

## Use cases

- Call center routing and escalation
- Fraud detection with human approval
- Logistics exception handling
- Financial transaction approval flows
- Manufacturing quality decision systems

---

## What this is NOT

- Not a workflow engine (like Temporal, Airflow)
- Not just a rule engine
- Not an LLM application framework
- Not a chat system

👉 This is a **decision execution layer**

---

## The problem

AI systems produce predictions. Production systems need decisions.

A prediction says: "This customer has an 87% probability of churning." That is not a decision. A decision says: "Route this customer to the retention team, within the refund policy, with supervisor approval if the amount exceeds $1,000."

Without a decision layer:

- AI outputs are executed without governance
- There is no audit trail when something goes wrong
- Human review is bolted on inconsistently after the fact
- Replaying or explaining a past decision is impossible
- Boundary violations are caught by production systems, not policy

---

## The solution

Every signal flows through a fixed, deterministic pipeline:

1. A typed **Signal** arrives (confidence score, payload, source)
2. The runtime evaluates it against a **DecisionFlow** — a YAML-defined graph of conditions
3. **Boundary** nodes enforce business rules: block, override, escalate, or redirect
4. **Human Gate** nodes suspend decisions pending manual review when stakes require it
5. Every step is committed to the **Ledger** — an append-only, hash-chained record
6. Confirmed decisions publish a typed **ExecutionRequest** event for downstream systems

The same inputs always produce the same decision path. Every decision is traceable, explainable, and replayable.

---

## Architecture

```
POST /api/runtime/evaluate
        │
        ▼
   FlowRegistry (YAML definitions)
        │
        ▼
 DecisionRuntimeEngine
        ├── ConditionEvaluator   evaluate DECISION nodes (AST-safe, no eval())
        ├── BoundaryEngine       block / override / escalate / redirect / allow
        ├── HumanGateManager     create pending review when status = pending_human
        │       └── POST /approve or /reject
        ├── RuntimeLedgerAdapter commit(trace, result) → Ledger Core v2
        ├── TraceStore           save DecisionTrace (in-memory cache)
        └── EventBus             publish EXECUTION_REQUESTED → ExecutionPublisher
                                         (Redis Streams or in-memory)
                                                    ↓
                                         KafkaExecutionPublisher → external orchestrator
```

---

## Key features (v1.0)

### Core

| Feature | Description |
|---|---|
| Deterministic evaluation | Identical inputs always produce identical decision paths |
| Declarative flows | Decision logic lives in YAML, not code |
| Boundary enforcement | Block, override, escalate, or redirect — priority-ordered rules |
| Human-in-the-loop | First-class approve/reject lifecycle with required-role RBAC |
| Idempotent execution | Repeated calls with the same `idempotency_key` return cached results |
| Explainable decisions | Every evaluation step is recorded and queryable |

### Integrations

| Backend | Role | Config |
|---|---|---|
| PostgreSQL | Append-only ledger (hash-chained) | `LEDGER_BACKEND=postgres` |
| Redis Streams | Persistent, replayable event bus | `EVENT_BUS_BACKEND=redis` |
| Kafka | External execution handoff | `EXECUTION_PUBLISHER_BACKEND=kafka` |

### Observability

- Prometheus-compatible `/metrics` endpoint (counters + summaries)
- Structured JSON access logging (`request_id`, `method`, `path`, `status_code`, `duration_ms`)
- Split health probes: `/health/live` (liveness) and `/health/ready` (readiness, per-dependency)

### Security

- Security headers on every response (`X-Content-Type-Options`, `X-Frame-Options`, `CSP`, `Cache-Control: no-store`)
- X-Api-Key auth for human gate actions (role-based, configurable)
- AST-safe condition evaluation — no `eval()`, no `exec()`

Full feature list and limitations: [docs/features.md](docs/features.md)

---

## Quick start

```bash
docker compose up --build
```

Verify the service is running:

```bash
curl http://localhost:8002/health/live
```

```json
{"status": "ok", "type": "live"}
```

Evaluate a signal:

```bash
curl -X POST http://localhost:8002/api/runtime/evaluate \
  -H "Content-Type: application/json" \
  -d '{
    "flow_id": "call_center_escalation",
    "signal": {
      "type": "customer_complaint",
      "confidence": 0.87,
      "payload": {
        "customer_tier": "vip",
        "refund_amount": 120000,
        "escalation_count": 5
      },
      "source": "interaction-core",
      "idempotency_key": "evt_001:sig_001:v1"
    }
  }'
```

The runtime returns a `DecisionResult`:

```json
{
  "id": "a1b2c3d4-...",
  "trace_id": "f5e6d7c8-...",
  "status": "confirmed",
  "outcome": "pass",
  "selected_node_id": "vip_check",
  "action": {
    "type": "prioritize",
    "target": "support_queue",
    "parameters": {"priority": "high", "lane": "vip"}
  },
  "execution_id": "exec_9a8b7c6d-...",
  "confidence": 0.87
}
```

When boundary rules escalate the decision:

```json
{
  "id": "b2c3d4e5-...",
  "status": "pending_human",
  "human_gate": {
    "id": "gate_7f8e9d0a-...",
    "status": "pending",
    "title": "Human review required",
    "options": [
      {"value": "approve", "label": "Approve", "is_default": true},
      {"value": "reject",  "label": "Reject",  "is_default": false}
    ]
  }
}
```

---

## Production mode

Add to your `.env` (see `.env.example` for the full list):

```env
# Durable ledger (append-only, hash-chained)
LEDGER_ENABLED=true
LEDGER_BACKEND=postgres
LEDGER_DATABASE_URL=postgresql://user:password@db:5432/decisions
LEDGER_MODE=strict

# Persistent event stream (Redis 6.2+)
EVENT_BUS_BACKEND=redis
REDIS_URL=redis://redis:6379/0

# External execution handoff
EXECUTION_PUBLISHER_BACKEND=kafka
KAFKA_BOOTSTRAP_SERVERS=kafka:9092

# Auth for human gate actions
AUTH_ENABLED=true
API_KEY_ROLE_MAP={"your-key": {"actor_id": "alice", "roles": ["reviewer"]}}
```

Production topology:

```
Decision Runtime Core
  ├─ PostgreSQL  (Ledger — append-only facts)
  ├─ Redis       (EventBus — replayable stream)
  └─ Kafka       (ExecutionPublisher — downstream handoff)
```

Full deployment guide: [docs/deployment.md](docs/deployment.md)

---

## View Core integration

DTM View Core does not make decisions.
It calls Decision Runtime Core to evaluate, inspect, compare, and simulate decision traces.

Decision ownership remains inside Runtime:

```
Interaction → Signal → Runtime → Boundary → Human → Ledger → View
```

**Runtime makes decisions. Ledger records them. View makes them understandable.**

Three View-support APIs are exposed:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/runtime/traces` | List trace summaries (status, outcome, action.type, confidence) |
| `POST` | `/api/runtime/compare` | Field-level diff between two traces |
| `POST` | `/api/runtime/simulate` | Re-evaluate with signal overrides — no side effects |

### `GET /api/runtime/traces`

Returns lightweight `TraceSummary` objects sorted newest-first.  Supports
`limit` (max 1 000) and `offset` for pagination.

```bash
curl "http://localhost:8002/api/runtime/traces?limit=20&offset=0"
```

Each summary includes `trace_id`, `decision_id`, `flow_id`, `status`,
`outcome`, `action_type`, `confidence`, `created_at`, and `committed_at`.

### `POST /api/runtime/compare`

Returns field-level diffs between two traces.

```bash
curl -X POST http://localhost:8002/api/runtime/compare \
  -H "Content-Type: application/json" \
  -d '{"base_trace_id": "...", "target_trace_id": "..."}'
```

```json
{
  "base_trace_id": "...",
  "target_trace_id": "...",
  "diffs": [
    {"path": "decision.status",     "base": "confirmed",    "target": "pending_human"},
    {"path": "decision.confidence", "base": 0.9,            "target": 0.6}
  ]
}
```

### `POST /api/runtime/simulate`

Re-evaluates the original signal from a trace with optional overrides.
**Never commits to Ledger, never publishes to EventBus, never creates a
persistent HumanGate.**

```bash
curl -X POST http://localhost:8002/api/runtime/simulate \
  -H "Content-Type: application/json" \
  -d '{
    "trace_id": "...",
    "signal_overrides": {
      "confidence": 0.4,
      "payload": {"refund_amount": 120000}
    }
  }'
```

```json
{
  "mode": "simulation",
  "source_trace_id": "...",
  "result": { "status": "pending_human", ... },
  "trace": { ... },
  "committed": false,
  "events_published": false
}
```

---

## Interaction Core Flow

Decision Runtime Core can evaluate structured Signals from Interaction Core.

Interaction Core captures messages, evidence, and analysis results.
It sends them as Signals to Runtime — it does not make decisions.

```
POST /api/runtime/evaluate
{
  "flow_id": "interaction_default_flow",
  "signal": {
    "type": "interaction_analysis",
    "confidence": 0.87,
    "payload": {
      "channel_id": "ch_001",
      "message_id": "msg_001",
      "summary": "Customer requests refund and mentions legal risk.",
      "keywords": ["refund", "legal"],
      "insights": ["Possible escalation case"],
      "suggested_actions": ["Escalate to support manager"]
    },
    "source": "interaction-core",
    "idempotency_key": "interaction:ch_001:msg_001:v1"
  }
}
```

Runtime evaluates those Signals through `interaction_default_flow`:

```
Interaction Core
  → Structured Signal (Message / Evidence / Analysis)
  → Decision Runtime Core
      ├── [A] High-risk keywords + confidence >= 0.7
      │       → Boundary (escalate) → Human Gate → Ledger
      ├── [B] Suggested actions + confidence >= 0.8
      │       → Decision Candidate → Decision Trace Studio → Ledger
      ├── [C] Insights / keywords + 0.5 <= confidence < 0.8
      │       → Route to Studio → Ledger
      ├── [D] confidence < 0.5
      │       → Notify only → Channel → Ledger
      └── [E] No actionable content
              → Fallback log → Ledger
```

**Important:**

- Interaction Core does not make decisions. Runtime remains the only Decision execution point.
- Suggested actions from AI analysis are treated as Decision Candidates, not committed execution decisions.
- High-risk interaction signals (legal, contract, security, incident, refund, compliance keywords at confidence >= 0.7) are always routed to Human Gate before any action.
- Every evaluation is committed to the Ledger and surfaced in View / Studio.

**High-risk example response:**

```json
{
  "status": "pending_human",
  "action": {
    "type": "escalate",
    "target": "human_review",
    "parameters": { "reason": "high_risk_interaction_signal" }
  },
  "human_gate": {
    "status": "pending",
    "title": "High-Risk Interaction Signal Requires Human Review"
  }
}
```

**Decision Candidate example response:**

```json
{
  "status": "confirmed",
  "action": {
    "type": "create_decision_candidate",
    "target": "decision_trace_studio",
    "parameters": { "reason": "high_confidence_suggested_action" }
  }
}
```

---

## API endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/runtime/evaluate` | Evaluate a signal against a flow |
| `GET` | `/api/runtime/flows` | List all loaded flows |
| `GET` | `/api/runtime/flows/{flow_id}` | Get a flow by ID |
| `GET` | `/api/runtime/traces` | List trace summaries (View Core) |
| `GET` | `/api/runtime/traces/{trace_id}` | Get a decision trace |
| `GET` | `/api/runtime/decision/{decision_id}/explain` | Explain a decision |
| `POST` | `/api/runtime/compare` | Field-level diff between two traces (View Core) |
| `POST` | `/api/runtime/simulate` | Simulate with signal overrides (View Core) |
| `POST` | `/api/runtime/human-gates/{id}/approve` | Approve a pending gate |
| `POST` | `/api/runtime/human-gates/{id}/reject` | Reject a pending gate |
| `GET` | `/api/runtime/events` | List runtime events (cursor pagination) |
| `GET` | `/api/runtime/stats` | Aggregated decision outcome statistics |
| `GET` | `/health/live` | Liveness probe |
| `GET` | `/health/ready` | Readiness probe (per-dependency checks) |
| `GET` | `/metrics` | Prometheus metrics (text format 0.0.4) |

Full API reference: [docs/api.md](docs/api.md)

---

## Project structure

```
backend/
├── app/
│   ├── config.py                      # Settings — env vars + .env
│   ├── main.py                        # FastAPI app, lifespan, health + metrics routes
│   ├── auth.py                        # X-Api-Key actor resolution
│   ├── security_headers.py            # SecurityHeadersMiddleware (ASGI)
│   ├── integrations/
│   │   ├── event_bus.py               # In-memory EventBus
│   │   ├── redis_event_bus.py         # Redis Streams EventBus
│   │   ├── ledger_client.py           # In-memory Ledger client
│   │   ├── postgres_ledger_client.py  # PostgreSQL Ledger client
│   │   ├── runtime_ledger_adapter.py  # DecisionTrace → LedgerEvent
│   │   ├── ledger_projector.py        # LedgerEvent → DecisionTrace (replay)
│   │   ├── kafka_execution_publisher.py  # Kafka ExecutionPublisher
│   │   └── execution_publisher.py     # NoopExecutionPublisher + Protocol
│   ├── models/                        # Pydantic v2 data models
│   ├── observability/
│   │   ├── metrics.py                 # Thread-safe Prometheus registry
│   │   └── logging_middleware.py      # StructuredLoggingMiddleware (ASGI)
│   ├── registry/
│   │   ├── flow_registry.py           # YAML → DecisionFlow loader
│   │   └── flow_validator.py          # Structural + semantic validation
│   └── runtime/
│       ├── engine.py                  # DecisionRuntimeEngine — main pipeline
│       ├── condition_evaluator.py     # AST-safe condition evaluation
│       ├── boundary_engine.py         # Boundary node evaluation
│       ├── human_gate_manager.py      # Approve/reject lifecycle
│       ├── trace_store.py             # In-memory trace storage
│       ├── trace_builder.py           # DecisionTrace construction
│       ├── explanation_builder.py     # Human-readable explanations
│       └── idempotency_store.py       # Result cache by idempotency key
├── flows/                             # Production YAML flow definitions
└── tests/                             # 303 unit + integration tests
```

---

## Status

**v1.0.0-rc1** — Production-ready single-node Decision OS Kernel.

303 tests passing. Core evaluation pipeline, all integrations, observability, security headers, and health probes are complete.

| Layer | Status |
|---|---|
| Decision evaluation engine | Production-ready |
| Boundary enforcement | Production-ready |
| Human gate (approve/reject, RBAC) | Production-ready |
| Ledger (memory + PostgreSQL) | Production-ready |
| EventBus (memory + Redis Streams) | Production-ready |
| ExecutionPublisher (noop + Kafka) | Production-ready |
| Observability (metrics + logging) | Production-ready |
| Security headers + health probes | Production-ready |

### Limitations

- **Single-node only** — no distributed consensus, no leader election
- **No multi-region** — TraceStore and HumanGateManager are in-process
- **No flow hot-reload** — flows are loaded once at startup
- **No rate limiting** — add a reverse proxy (nginx, Envoy) in front for RPS caps
- **PostgreSQL only** for ledger persistence (no MySQL, no MongoDB)
- **Redis 6.2+** required for exclusive-range XRANGE pagination

See [docs/features.md](docs/features.md) for the full capability and limitation list.

---

## Design principles

> AI generates signals.
> Runtime evaluates decisions.
> Boundary controls risk.
> Human approves exceptions.
> Ledger commits facts.
> EventBus announces.
> ExecutionPublisher hands off.
>
> This is a Decision OS Kernel.

## License

MIT License
test token
token switch test 2026年 5月 6日 水曜日 09時02分46秒 JST
token auth final test 2026年 5月 6日 水曜日 09時06分59秒 JST
token auth final test 2026年 5月 6日 水曜日 09時08分24秒 JST
