# API Reference

Base URL: `http://localhost:8000`

All runtime endpoints are prefixed with `/api/runtime`.

---

## Table of contents

- [POST /api/runtime/evaluate](#post-apiruntimeevaluate)
- [GET /api/runtime/flows](#get-apiruntimeflows)
- [GET /api/runtime/flows/{flow_id}](#get-apiruntimeflowsflow_id)
- [GET /api/runtime/traces/{trace_id}](#get-apiruntimetracestrace_id)
- [GET /api/runtime/decision/{decision_id}/explain](#get-apiruntimedecisiondecision_idexplain)
- [POST /api/runtime/human-gates/{id}/approve](#post-apiruntimehuman-gatesidapprove)
- [POST /api/runtime/human-gates/{id}/reject](#post-apiruntimehuman-gatesidreject)
- [GET /api/runtime/events](#get-apiruntimeevents)
- [GET /health](#get-health)
- [Error codes](#error-codes)

---

## POST /api/runtime/evaluate

Evaluate a signal against a decision flow and return the resulting `DecisionResult`.

The engine runs the full pipeline: condition evaluation → boundary enforcement → human gate creation (if escalated) → trace save → execution event (if confirmed).

### Request body

```json
{
  "flow_id": "call_center_escalation",
  "flow_version": "1.0.0",
  "signal": {
    "signal_id": "sig_001",
    "event_id": "evt_001",
    "type": "customer_complaint",
    "confidence": 0.87,
    "payload": {
      "customer_tier": "vip",
      "refund_amount": 120000,
      "escalation_count": 5
    },
    "source": "interaction-core",
    "created_at": "2026-05-03T12:00:00Z",
    "idempotency_key": "evt_001:sig_001:v1"
  }
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `flow_id` | string | yes | ID of the flow to evaluate |
| `flow_version` | string | no | Semantic version; omit for latest |
| `signal.signal_id` | string (UUID) | no | Caller-supplied signal ID |
| `signal.event_id` | string | no | Originating event ID (metadata) |
| `signal.name` | string | no | Signal name; defaults to `type` |
| `signal.type` | string | no | Domain type classification |
| `signal.confidence` | float [0.0–1.0] | no | Confidence score (default: 1.0) |
| `signal.payload` | object | no | Key-value data for condition evaluation |
| `signal.source` | string | no | Origin system identifier (default: "api") |
| `signal.created_at` | ISO 8601 datetime | no | Signal emission time (default: now) |
| `signal.idempotency_key` | string | no | Cache key; repeated calls return same result |

### Response — confirmed

```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "trace_id": "f5e6d7c8-b9a0-1234-cdef-567890abcdef",
  "flow_id": "550e8400-e29b-41d4-a716-446655440000",
  "flow_version": "1.0.0",
  "selected_node_id": "vip_check",
  "source_signal_id": "11223344-5566-7788-99aa-bbccddeeff00",
  "state": "confirmed",
  "status": "confirmed",
  "outcome": "pass",
  "confidence": 0.87,
  "action": {
    "type": "prioritize",
    "target": "support_queue",
    "parameters": {"priority": "high", "lane": "vip"}
  },
  "execution_id": "exec_9a8b7c6d-0e1f-2a3b-4c5d-6e7f8a9b0c1d",
  "contract_version": "1.0.0",
  "signals_used": ["customer_complaint"],
  "conditions_evaluated": 1,
  "conditions_passed": 1,
  "boundary_results": [],
  "human_gate": null,
  "evaluated_at": "2026-05-03T12:00:00.123Z",
  "created_at": "2026-05-03T12:00:00.123Z",
  "updated_at": "2026-05-03T12:00:00.123Z"
}
```

### Response — pending_human

When a boundary with `effect: escalate` triggers, `status` is `pending_human`, `execution_id` is null, and `human_gate` is populated.

```json
{
  "id": "b2c3d4e5-...",
  "status": "pending_human",
  "execution_id": null,
  "boundary_results": [
    {
      "boundary_id": "rate_limit_boundary",
      "triggered": true,
      "severity": "high",
      "effect": "escalate",
      "action": null,
      "reason": "boundary triggered with effect=escalate",
      "evaluated_at": "2026-05-03T12:00:00.123Z"
    }
  ],
  "human_gate": {
    "id": "gate_7f8e9d0a-...",
    "status": "pending",
    "node_id": "vip_check",
    "title": "Human review required for escalated decision",
    "question": "Review the escalated decision and choose to approve or reject it",
    "options": [
      {"value": "approve", "label": "Approve", "description": "Confirm the escalated decision and allow it to proceed", "is_default": true},
      {"value": "reject",  "label": "Reject",  "description": "Deny the escalated decision and mark it as rejected",  "is_default": false}
    ],
    "created_at": "2026-05-03T12:00:00.123Z"
  }
}
```

### Response — blocked

When a boundary with `effect: block` triggers:

```json
{
  "status": "blocked",
  "action": null,
  "execution_id": null,
  "boundary_results": [
    {
      "boundary_id": "rate_limit_boundary",
      "triggered": true,
      "severity": "high",
      "effect": "block",
      "reason": "boundary triggered with effect=block"
    }
  ]
}
```

### Idempotency

When `idempotency_key` is present:

- The first call evaluates normally and caches the `DecisionResult`
- Subsequent calls with the same key return the cached result immediately
- No second evaluation, no duplicate events

---

## GET /api/runtime/flows

List all flows loaded in the registry at startup.

### Response

```json
[
  {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "flow_id": "call_center_escalation",
    "name": "Call Center Escalation Flow",
    "version": "1.0.0",
    "entry_node_id": "vip_check",
    "is_active": true,
    "nodes": [...],
    "edges": [...]
  }
]
```

---

## GET /api/runtime/flows/{flow_id}

Get a specific flow by ID. Accepts an optional `version` query parameter.

### Query parameters

| Parameter | Type | Description |
|---|---|---|
| `version` | string | Semantic version; omit for latest |

### Response

Same as a single element from `GET /flows`.

### Errors

| Condition | Status |
|---|---|
| Flow not found | 404 |

---

## GET /api/runtime/traces/{trace_id}

Retrieve the full `DecisionTrace` for a completed evaluation. The `trace_id` equals the `trace_id` field of the `DecisionResult`.

### Response

```json
{
  "id": "f5e6d7c8-b9a0-1234-cdef-567890abcdef",
  "flow_id": "550e8400-...",
  "flow_version": "1.0.0",
  "decision_id": "a1b2c3d4-...",
  "signal_id": "11223344-...",
  "evaluated_nodes": [
    {
      "node_id": "vip_check",
      "node_type": "decision",
      "matched": true,
      "condition": "payload.customer_tier == \"vip\"",
      "reason": "condition matched"
    }
  ],
  "decision_results": [...],
  "boundary_results": [],
  "human_gate_requests": [],
  "created_at": "2026-05-03T12:00:00.123Z"
}
```

### Errors

| Condition | Status |
|---|---|
| Trace not found | 404 |

---

## GET /api/runtime/decision/{decision_id}/explain

Return a human-readable structured explanation for a decision. Looks up the trace by `decision_id`.

### Response

```json
{
  "decision_id": "a1b2c3d4-...",
  "trace_id": "f5e6d7c8-...",
  "selected_node": "vip_check",
  "matched_conditions": [
    {
      "node_id": "vip_check",
      "node_type": "decision",
      "condition": "payload.customer_tier == \"vip\"",
      "reason": "condition matched"
    }
  ],
  "unmatched_conditions": [],
  "boundary_effects": [],
  "human_gate": null,
  "final_action": {
    "type": "prioritize",
    "target": "support_queue"
  },
  "final_status": "confirmed"
}
```

### Errors

| Condition | Status |
|---|---|
| Decision not found | 404 |

---

## POST /api/runtime/human-gates/{id}/approve

Approve a pending `HumanGateRequest`. Transitions the linked `DecisionResult` from `pending_human` to `confirmed`.

### Path parameters

| Parameter | Description |
|---|---|
| `id` | UUID string of the `HumanGateRequest` |

### Request body

```json
{
  "actor_id": "supervisor_01",
  "comment": "Reviewed and approved — within policy"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `actor_id` | string | yes | Identifier of the reviewer |
| `comment` | string | no | Optional note (max 4096 chars) |

### Response

Updated `DecisionResult` with `status: "confirmed"`.

### Errors

| Condition | Status |
|---|---|
| Gate request not found | 404 |
| Request not in PENDING state | 409 |

---

## POST /api/runtime/human-gates/{id}/reject

Reject a pending `HumanGateRequest`. Transitions the linked `DecisionResult` from `pending_human` to `rejected` and clears `action`.

### Request body

Same as approve.

### Response

Updated `DecisionResult` with `status: "rejected"` and `action: null`.

### Errors

| Condition | Status |
|---|---|
| Gate request not found | 404 |
| Request not in PENDING state | 409 |

---

## GET /api/runtime/events

Return all `RuntimeEvent` objects stored in the in-memory `EventBus`, in publication order.

Intended for development and debugging. Events are not paginated in the MVP.

### Response

```json
[
  {
    "id": "evt_1a2b3c4d-...",
    "event_type": "runtime.execution.requested",
    "flow_id": "550e8400-...",
    "trace_id": "f5e6d7c8-...",
    "decision_id": "a1b2c3d4-...",
    "timestamp": "2026-05-03T12:00:00.456Z",
    "payload": {
      "execution_id": "exec_9a8b7c6d-...",
      "decision_id": "a1b2c3d4-...",
      "action": {"type": "prioritize", "target": "support_queue"}
    }
  }
]
```

### Event types

| Event type | When emitted |
|---|---|
| `runtime.execution.requested` | Decision confirmed; execution ready |
| `runtime.execution.completed` | Execution completed (reserved) |
| `decision.made` | General decision lifecycle event (reserved) |
| `boundary.checked` | Boundary evaluated (reserved) |
| `human_gate.opened` | Human gate created (reserved) |
| `human_gate.responded` | Gate approve/reject received (reserved) |

---

## GET /health

Service liveness check. No authentication required.

### Response

```json
{"status": "ok"}
```

---

## Error codes

| HTTP status | Meaning | Example causes |
|---|---|---|
| 200 | Success | Request processed |
| 400 | Bad request | Invalid condition expression in flow |
| 404 | Not found | Unknown `flow_id`, `trace_id`, or `decision_id` |
| 409 | Conflict | Gate already approved/rejected (not in PENDING state) |
| 500 | Server error | No fallback node in flow; unexpected runtime failure |

### Error response shape

```json
{
  "detail": "Flow 'unknown_flow' not found in registry"
}
```
