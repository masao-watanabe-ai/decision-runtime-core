# Decision Runtime Core — Architecture

## Position in the stack

```
┌─────────────────────────────────────────┐
│           Signal Sources                │
│   AI models / user events / sensors     │
└────────────────┬────────────────────────┘
                 │  POST /api/runtime/evaluate
                 ▼
┌─────────────────────────────────────────┐
│         Decision Runtime Core           │
│                                         │
│  ┌──────────────────────────────────┐   │
│  │      FlowRegistry (YAML)         │   │
│  └──────────────┬───────────────────┘   │
│                 ▼                        │
│  ┌──────────────────────────────────┐   │
│  │   DecisionRuntimeEngine          │   │
│  │                                  │   │
│  │  1. ConditionEvaluator           │   │
│  │     (AST-safe, no eval/exec)     │   │
│  │                                  │   │
│  │  2. BoundaryEngine               │   │
│  │     block / override /           │   │
│  │     escalate / redirect / allow  │   │
│  │                                  │   │
│  │  3. HumanGateManager             │   │
│  │     pending → approve / reject   │   │
│  │                                  │   │
│  │  4. RuntimeLedgerAdapter         │   │
│  │     commit to Ledger (strict)    │   │
│  │                                  │   │
│  │  5. TraceStore (in-memory cache) │   │
│  │                                  │   │
│  │  6. EventBus → ExecutionPublisher│   │
│  └──────────────────────────────────┘   │
└──────┬──────────────────┬───────────────┘
       │                  │
       ▼                  ▼
┌────────────┐    ┌────────────────────┐
│  Ledger    │    │  EventBus          │
│ (Postgres) │    │  (Redis Streams)   │
│            │    │          ↓         │
│ append-only│    │ ExecutionPublisher │
│ hash-chain │    │  (Kafka)           │
└────────────┘    └────────┬───────────┘
                           │
                           ▼
              ┌─────────────────────────┐
              │   External Orchestrator │
              │   (acts on confirmed    │
              │    decisions)           │
              └─────────────────────────┘
```

---

## Evaluation pipeline (step by step)

```
DecisionRuntimeEngine.evaluate(signal, flow)
│
├── 1. Idempotency check
│       signal.idempotency_key? → IdempotencyStore lookup
│       cache hit → return cached result immediately
│
├── 2. Evaluate DECISION nodes
│       ConditionEvaluator.evaluate(condition, context)
│       context = {type, confidence, payload, source, created_at}
│       → collect matched[] nodes
│
├── 3. Select winner
│       strategy: first_match (default) or priority
│       → selected DecisionNode → status = CONFIRMED, outcome = PASS
│       OR → FALLBACK node → status = FALLBACK, outcome = FAIL
│
├── 4. Apply BOUNDARY nodes (all active, sorted by severity)
│       effect: allow     → no change
│                block     → status = BLOCKED, action = None
│                escalate  → status = PENDING_HUMAN
│                override  → replace action + node
│                redirect  → replace action + node
│
├── 5. Human gate (if status = PENDING_HUMAN)
│       HumanGateManager.create_request(result)
│       → gate is suspended until POST /approve or /reject
│
├── 6. Ledger commit (if ledger_enabled=true)
│       strict mode:  ACCEPTED → continue; FAILED → status = ERROR, stop
│       parallel mode: fire-and-forget, failures do not block result
│
├── 7. EventBus publish (if status = CONFIRMED)
│       EventBus.publish(RuntimeEvent(EXECUTION_REQUESTED, ...))
│       → ExecutionPublisher.publish(event) [Kafka or noop]
│
├── 8. Save trace
│       TraceStore.save(DecisionTrace)
│
└── 9. Return DecisionResult
        status: confirmed | fallback | pending_human | blocked | rejected | error
```

---

## Decision status lifecycle

```
             evaluate()
                 │
         ┌───────┴───────┐
         │               │
    conditions      no match
     matched
         │               │
         ▼               ▼
     CONFIRMED       FALLBACK
         │
    boundary
     check
         │
    ┌────┴──────────────────┐
    │           │            │
   allow      block      escalate / override / redirect
    │           │            │
    ▼           ▼            ▼
CONFIRMED    BLOCKED    PENDING_HUMAN
                              │
                    ┌─────────┴─────────┐
                    │                   │
                 approve             reject
                    │                   │
                    ▼                   ▼
               CONFIRMED            REJECTED
```

---

## Integration topology

### Development (default)

```
Decision Runtime Core
  └─ in-memory (EventBus, TraceStore, Ledger, IdempotencyStore)
```

All backends are in-process. No external dependencies. Zero config.

### Production (recommended)

```
Decision Runtime Core
  ├─ PostgreSQL   LEDGER_BACKEND=postgres
  ├─ Redis 6.2+   EVENT_BUS_BACKEND=redis
  └─ Kafka        EXECUTION_PUBLISHER_BACKEND=kafka
```

### Hybrid (common during migration)

```
Decision Runtime Core
  ├─ PostgreSQL   (Ledger)
  └─ in-memory    (EventBus, IdempotencyStore)
```

---

## State management

All runtime state lives in `app.state` singletons wired at startup (`main.py` lifespan):

| Singleton | Type | Production swap |
|---|---|---|
| `flow_registry` | `FlowRegistry` | Reload from DB or S3 |
| `trace_store` | `TraceStore` | Postgres / Redis |
| `human_gate_manager` | `HumanGateManager` | Persist to DB |
| `event_bus` | `EventBus` / `RedisEventBus` | Redis Streams (done) |
| `idempotency_store` | `IdempotencyStore` | Redis with TTL |
| `ledger_client` | `LedgerClient` / `PostgresLedgerClient` | PostgreSQL (done) |
| `execution_publisher` | `NoopExecutionPublisher` / `KafkaExecutionPublisher` | Kafka (done) |

---

## Security model

- **Condition evaluation**: AST whitelist — comparison ops, boolean ops, attribute access only. No `eval()`, no `exec()`, no imports.
- **Auth**: X-Api-Key → `api_key_role_map` → `Actor{actor_id, roles}`. Enforced at human gate actions only. Disabled by default (`AUTH_ENABLED=false`).
- **RBAC**: `required_role` on a flow node; actors without the role receive HTTP 403.
- **Ledger**: append-only hash chain. `event_hash = SHA-256(canonical JSON)` seals each record; `prev_hash` links within a trace.
- **HTTP**: `SecurityHeadersMiddleware` injects defensive headers on all responses (including 4xx/5xx).

---

## Non-goals (v1.0)

- Distributed consensus / multi-node leader election
- Multi-region replication
- Flow hot-reload
- Rate limiting (add a reverse proxy)
- MySQL, MongoDB ledger backends
- GraphQL / gRPC interface
