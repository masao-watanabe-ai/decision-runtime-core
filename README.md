# Decision Runtime Core

**AI is not prediction. It is decision.**
Turn AI outputs into reproducible, controllable decisions.

---

## What this is

The Decision Runtime Core is a **Decision OS Kernel** — a production-grade runtime layer that sits between AI signal producers and execution systems.

It solves the fundamental gap in AI-powered products: **AI generates signals, not decisions.** A signal is a probability. A decision is an accountable, traceable, governed act. This runtime turns one into the other.

### Core responsibilities

| Layer | What it does |
|---|---|
| **Signal ingestion** | Accepts typed, confidence-scored signals from upstream systems |
| **Decision evaluation** | Evaluates signals against declarative flow definitions |
| **Boundary enforcement** | Applies block / override / escalate / redirect rules |
| **Human gating** | Suspends decisions pending human review when required |
| **Trace & explain** | Records every evaluation step for audit and replay |
| **Execution request** | Emits a typed event when a confirmed decision is ready to act |

---

## The problem

AI systems produce predictions. Production systems need decisions.

A prediction says: "This customer has an 87% probability of churning." That is not a decision. A decision says: "Route this customer to the retention team, within the refund policy, with supervisor approval if the amount exceeds $1,000."

Without a decision layer:

- AI outputs are executed without governance
- There is no audit trail when something goes wrong
- Human review is bolted on after the fact, inconsistently
- Replaying or explaining a past decision is impossible
- Boundary violations are caught by production systems, not policy

---

## The solution

The Decision Trace Model:

```
Event → Signal → Decision → Boundary → Human → Log
```

Every decision flows through the same pipeline:

1. A typed **Signal** arrives (confidence score, payload, source)
2. The runtime evaluates the signal against a **DecisionFlow** (YAML-defined graph of conditions)
3. **Boundary** nodes enforce business rules — blocking, overriding, or escalating the decision
4. **Human Gate** nodes suspend the decision for manual review when boundaries escalate
5. Every step is captured in a **DecisionTrace** for audit, explain, and replay
6. Confirmed decisions produce an **ExecutionRequest** event for downstream orchestration

---

## Runtime pipeline

```
POST /api/runtime/evaluate
        │
        ▼
   FlowRegistry  ──────────────────────────────────────────────┐
        │                                                       │
        ▼                                                       │
 DecisionRuntimeEngine                                          │
        │                                                       │
        ├── ConditionEvaluator  → evaluate DECISION nodes       │
        │                                                       │
        ├── BoundaryEngine      → evaluate BOUNDARY nodes       │
        │        │                                              │
        │        └── effect: block / override / escalate /      │
        │                    redirect / allow                   │
        │                                                       │
        ├── HumanGateManager    → create pending review         │
        │        │                 when status = pending_human  │
        │        └── POST /approve or /reject → confirmed /     │
        │                                       rejected        │
        │                                                       │
        ├── TraceStore          → save DecisionTrace            │
        │        │                                              │
        │        └── GET /traces/{trace_id}                     │
        │            GET /decision/{id}/explain                 │
        │                                                       │
        └── EventBus            → publish                       │
                 │                runtime.execution.requested   │
                 └── GET /events                                │
                                                                │
   DecisionResult ◄──────────────────────────────────────────┘
```

---

## Features

- **Deterministic evaluation** — identical inputs always produce identical decisions (excluding auto-generated UUIDs)
- **Declarative flows** — decision logic lives in YAML, not code
- **Boundary enforcement** — block, override, escalate, or redirect decisions via priority-ordered rules
- **Human-in-the-loop** — first-class approve/reject lifecycle with audit trail
- **Explainable decisions** — structured explanation of every evaluation step
- **Full trace** — every decision is recorded with matched/unmatched conditions and boundary results
- **Idempotent API** — repeated calls with the same `idempotency_key` return the cached result without duplicate events
- **Event-driven** — emits typed `RuntimeEvent` objects for downstream integration

---

## Quick start

```bash
docker compose up --build
```

Verify the service is running:

```bash
curl http://localhost:8000/health
```

```json
{"status": "ok"}
```

---

## Example request

```bash
curl -X POST http://localhost:8000/api/runtime/evaluate \
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

---

## Example responses

### Confirmed decision

```json
{
  "id": "a1b2c3d4-...",
  "trace_id": "f5e6d7c8-...",
  "flow_id": "...",
  "flow_version": "1.0.0",
  "status": "confirmed",
  "outcome": "pass",
  "selected_node_id": "vip_check",
  "action": {
    "type": "prioritize",
    "target": "support_queue",
    "parameters": {"priority": "high", "lane": "vip"}
  },
  "execution_id": "exec_9a8b7c6d-...",
  "human_gate": null,
  "confidence": 0.87,
  "conditions_evaluated": 1,
  "conditions_passed": 1
}
```

### Pending human review

```json
{
  "id": "b2c3d4e5-...",
  "trace_id": "a1b2c3d4-...",
  "status": "pending_human",
  "outcome": "pass",
  "selected_node_id": "vip_check",
  "action": {
    "type": "prioritize",
    "target": "support_queue"
  },
  "execution_id": null,
  "human_gate": {
    "id": "gate_7f8e9d0a-...",
    "status": "pending",
    "title": "Human review required for escalated decision",
    "question": "Review the escalated decision and choose to approve or reject it",
    "options": [
      {"value": "approve", "label": "Approve", "is_default": true},
      {"value": "reject",  "label": "Reject",  "is_default": false}
    ]
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
| `GET` | `/api/runtime/traces/{trace_id}` | Get a decision trace |
| `GET` | `/api/runtime/decision/{decision_id}/explain` | Explain a decision |
| `POST` | `/api/runtime/human-gates/{id}/approve` | Approve a pending gate |
| `POST` | `/api/runtime/human-gates/{id}/reject` | Reject a pending gate |
| `GET` | `/api/runtime/events` | List all runtime events |

Full schema documentation: [docs/api.md](docs/api.md)

---

## Project structure

```
backend/
├── app/
│   ├── config.py                  # Settings (flow_dir, app_name, etc.)
│   ├── main.py                    # FastAPI app + lifespan (startup singletons)
│   ├── integrations/
│   │   └── event_bus.py           # In-memory publish/subscribe
│   ├── models/
│   │   ├── signal.py              # Signal — typed AI output
│   │   ├── flow.py                # DecisionFlow, DecisionNode, FlowEdge
│   │   ├── decision.py            # DecisionResult, DecisionStatus
│   │   ├── boundary.py            # BoundaryResult, BoundaryEffect
│   │   ├── human_gate.py          # HumanGateRequest, HumanGateStatus
│   │   ├── trace.py               # DecisionTrace
│   │   ├── event.py               # RuntimeEvent, EventType
│   │   └── execution.py           # ExecutionRequest
│   ├── registry/
│   │   ├── flow_registry.py       # Loads and caches YAML flow files
│   │   ├── flow_validator.py      # Structural + semantic flow validation
│   │   └── contract_registry.py   # Inline contract validation
│   ├── routes/
│   │   └── runtime.py             # All API route handlers
│   └── runtime/
│       ├── engine.py              # DecisionRuntimeEngine — main pipeline
│       ├── condition_evaluator.py # Safe AST-based condition evaluation
│       ├── boundary_engine.py     # Boundary node evaluation + effect application
│       ├── human_gate_manager.py  # Approve/reject lifecycle for escalated decisions
│       ├── trace_builder.py       # Builds DecisionTrace from evaluation data
│       ├── trace_store.py         # In-memory trace storage (dual index)
│       ├── explanation_builder.py # Converts trace to human-readable explanation
│       └── idempotency_store.py   # Caches results by idempotency key
├── flows/
│   ├── call_center_flow.yaml
│   ├── logistics_flow.yaml
│   └── manufacturing_quality_flow.yaml
└── tests/
    ├── conftest.py
    ├── flows/                     # Test-specific flow YAML files
    └── test_*.py                  # Unit + E2E test files
```

---

## Status

```
MVP complete — in-memory runtime, fully tested (82 tests passing)

Next:
  - Redis-backed EventBus (persistent pub/sub)
  - Kafka integration for execution event delivery
  - Ledger integration for immutable decision log
  - External execution engine (orchestrator integration)
  - Authentication and role-based gate assignment
  - Metrics and observability (Prometheus / OpenTelemetry)
```

## License

MIT License © 2026 Masao Watanabe
