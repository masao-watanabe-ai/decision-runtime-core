# Architecture

## Decision Runtime Core — role and position

The Decision Runtime Core is the **governing layer** between AI inference and execution. Its job is to transform raw AI signals into auditable, controllable decisions — and to ensure those decisions pass through the right rules, reviews, and records before anything happens.

```
┌─────────────────────┐
│   interaction-core  │  ← emits Signals (AI outputs, user events)
└────────┬────────────┘
         │ Signal
         ▼
┌─────────────────────────────────────────────────────────┐
│               decision-runtime-core                      │
│                                                         │
│  FlowRegistry → Engine → Boundary → HumanGate → Trace  │
│                                         │               │
│                          ExecutionRequest → EventBus    │
└─────────────────────────────────────────────────────────┘
         │ ExecutionRequest event
         ▼
┌─────────────────────┐
│     orchestrator    │  ← acts on confirmed decisions
└─────────────────────┘
         │ Decision record
         ▼
┌─────────────────────┐
│       ledger        │  ← immutable audit log
└─────────────────────┘
```

---

## Why Decision != Signal

A Signal is a measurement: a probability, a classification, a score. It is the output of an AI model or a data pipeline.

A Decision is an act with consequences: it routes a customer, approves a transaction, holds a shipment. It must be:

- **Traceable** — who decided, based on what, when
- **Governed** — boundary rules enforced before execution
- **Accountable** — human review when the stakes require it
- **Reproducible** — same inputs must yield the same decision logic path
- **Explainable** — any decision must be interpretable after the fact

The Decision Runtime Core provides all of these properties. Signals provide none of them.

---

## System relationships

### interaction-core

The interaction-core is the upstream signal producer. It handles raw events (calls, messages, transactions, sensor data) and converts them into typed Signals with confidence scores and payloads.

Interaction with this system:
- Sends `POST /api/runtime/evaluate` with a Signal payload
- Receives `DecisionResult` synchronously
- The `idempotency_key` field on the signal prevents duplicate evaluations for at-most-once delivery

### decision-trace-model

The decision-trace-model defines the canonical data shape for decision records. The `DecisionTrace`, `DecisionResult`, `BoundaryResult`, and `HumanGateRequest` models in this runtime conform to that specification.

Every evaluation produces a `DecisionTrace` stored in the `TraceStore`. Traces are queryable by `trace_id` or `decision_id`.

### orchestrator

The orchestrator is the downstream execution system. It receives `ExecutionRequest` events from the `EventBus` when a decision is confirmed and acts on the `action` payload (routing, notification, scheduling, etc.).

In the current MVP, the `EventBus` is in-memory. In production, this will be replaced by Kafka or Redis Streams, and the orchestrator will consume from those topics.

The `ExecutionRequest` contains:
- `execution_id` — unique per confirmed decision
- `decision_id` — links back to the `DecisionResult`
- `trace_id` — links to the full audit trace
- `action` — the action payload from the selected decision node

### ledger

The ledger is the immutable audit log for all decisions. It receives decision records for compliance, replay, and retrospective analysis.

In the current MVP, traces are stored in-memory. The `TraceStore` interface is designed for swap-in with a persistent implementation (Postgres, Redis, S3) without changing callers.

---

## Internal component map

```
backend/app/
│
├── main.py                    ← FastAPI app; lifespan wires all singletons into app.state
│
├── config.py                  ← Settings (env vars / .env); flow_dir, app_name, version
│
├── models/                    ← Pydantic v2 data models (all immutable)
│   ├── signal.py              ← Signal — typed, confidence-scored AI output
│   ├── flow.py                ← DecisionFlow, DecisionNode, FlowEdge, NodeType
│   ├── decision.py            ← DecisionResult, DecisionStatus, DecisionOutcome
│   ├── boundary.py            ← BoundaryResult, BoundaryEffect, BoundarySeverity
│   ├── human_gate.py          ← HumanGateRequest, HumanGateStatus, HumanGateOption
│   ├── trace.py               ← DecisionTrace (full evaluation record)
│   ├── event.py               ← RuntimeEvent, EventType
│   ├── execution.py           ← ExecutionRequest (emitted on confirmed decisions)
│   ├── contract.py            ← DecisionContract (inline contract spec)
│   └── runtime.py             ← RuntimeState enum
│
├── registry/                  ← Flow loading and validation
│   ├── flow_registry.py       ← Parses YAML → DecisionFlow; caches by (flow_id, version)
│   ├── flow_validator.py      ← Structural and semantic validation rules
│   └── contract_registry.py   ← Inline contract type/version validation
│
├── routes/
│   └── runtime.py             ← All API route handlers; thin — delegates to runtime layer
│
├── integrations/
│   └── event_bus.py           ← In-memory EventBus; publish/subscribe for RuntimeEvents
│
└── runtime/                   ← Core evaluation pipeline
    ├── engine.py              ← DecisionRuntimeEngine.evaluate() — orchestrates all steps
    ├── condition_evaluator.py ← AST-based safe condition evaluation (no eval/exec)
    ├── boundary_engine.py     ← BoundaryEngine.apply() — evaluate all BOUNDARY nodes
    ├── human_gate_manager.py  ← Approve/reject lifecycle for pending_human decisions
    ├── trace_builder.py       ← Builds DecisionTrace from evaluation artifacts
    ├── trace_store.py         ← In-memory trace storage indexed by trace_id + decision_id
    ├── explanation_builder.py ← Converts DecisionTrace to human-readable explanation dict
    └── idempotency_store.py   ← Caches DecisionResult by idempotency key
```

---

## Evaluation pipeline (detail)

```
DecisionRuntimeEngine.evaluate(signal, flow)
│
├── 1. Build context
│       signal.{type, confidence, payload, source, timestamp}
│       → context dict for condition evaluation
│
├── 2. Evaluate DECISION nodes (in flow.nodes order)
│       ConditionEvaluator.evaluate(condition, context)
│       → collect matched[] list
│
├── 3. Resolve winner (if any matched)
│       strategy: first_match (default) or priority
│       → selected DecisionNode
│       → status = CONFIRMED, outcome = PASS
│
│   OR: select FALLBACK node (if nothing matched)
│       → status = FALLBACK, outcome = FAIL
│
├── 4. Apply BOUNDARY nodes
│       BoundaryEngine.apply(signal, flow, initial_result)
│       → evaluate ALL active BOUNDARY nodes
│       → sort triggered boundaries by severity (critical > high > medium > low)
│       → apply highest-severity effect:
│           allow     → no change
│           block     → status = BLOCKED, action = None
│           escalate  → status = PENDING_HUMAN
│           override  → replace action + selected_node_id
│           redirect  → replace action + selected_node_id
│
├── 5. Create HumanGateRequest (if status = PENDING_HUMAN)
│       HumanGateManager.create_request(result)
│       → stores gate request + result for later approve/reject
│       → attaches gate to result.human_gate
│
├── 6. Create ExecutionRequest + emit event (if status = CONFIRMED)
│       ExecutionRequest(execution_id, decision_id, trace_id, action)
│       result.execution_id = exec_req.execution_id
│       EventBus.publish(RuntimeEvent(EXECUTION_REQUESTED, ...))
│
├── 7. Save trace
│       TraceBuilder.create_trace(signal, flow, result, evaluated_nodes)
│       TraceStore.save(trace)
│
└── 8. Return final DecisionResult
```

---

## State management

All state is held in `app.state` singletons, wired at startup in `main.py`:

| Singleton | Type | Purpose |
|---|---|---|
| `flow_registry` | `FlowRegistry` | Loaded flows, read-only after startup |
| `human_gate_manager` | `HumanGateManager` | Pending gate requests + associated results |
| `trace_store` | `TraceStore` | All DecisionTraces, indexed by trace_id and decision_id |
| `event_bus` | `EventBus` | Ordered list of all RuntimeEvents |
| `idempotency_store` | `IdempotencyStore` | Cached results keyed by idempotency_key |

In the MVP these are in-memory. Each has a clean interface designed for swap-in with persistent backends:

- `FlowRegistry` → add a `reload()` method backed by a database or S3
- `TraceStore` → implement the same `save/get/get_by_decision_id` interface against Postgres
- `EventBus` → implement `publish` as a Kafka producer
- `IdempotencyStore` → implement `get/set` against Redis with TTL

---

## Security model (MVP)

Condition expressions are evaluated using a strict AST whitelist — no `eval()`, no `exec()`, no imports, no function calls. Only comparison operators, boolean operators, and attribute access against a fixed set of context variables (`type`, `confidence`, `payload`, `source`, `created_at`) are permitted.

Authentication and authorization are out of scope for the MVP. The human gate `actor_id` field is a string identifier only — role enforcement is reserved for a future auth layer.

---

## Non-goals (MVP)

- Persistent storage (Redis, Postgres, S3)
- Kafka or message broker integration
- Authentication / authorization
- Multi-tenancy
- Rate limiting
- External execution engine integration
- Flow hot-reload (flows are loaded once at startup)
