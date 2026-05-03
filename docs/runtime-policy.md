# Runtime Policy

This document describes the behavioral guarantees and operational rules enforced by the Decision Runtime Core.

---

## Determinism

**The `evaluate()` function is a pure function.**

Given identical inputs — the same `Signal` and the same `DecisionFlow` — the engine always produces a `DecisionResult` with:

- The same `selected_node_id`
- The same `status`
- The same `outcome`
- The same `action`
- The same `conditions_evaluated` and `conditions_passed`

The only fields that legitimately differ between calls with identical inputs are the auto-generated UUID fields (`id`, `trace_id`), and timestamps.

**Enforcement:**

- Condition evaluation is deterministic (AST-based, no randomness, no I/O)
- Boundary evaluation processes nodes in a fixed order (sorted by severity, then by position)
- Resolution policy is deterministic for a given node list and priority values
- No global mutable state is read or written during evaluation; all singletons (`TraceStore`, `EventBus`, etc.) are written only after the result is fully computed

**Implication for callers:**

If you need to re-evaluate a signal for debugging or replay, use the same flow version. Different flow versions are not guaranteed to produce the same result.

---

## Idempotency

**Repeated API calls with the same `idempotency_key` return the cached `DecisionResult` without re-evaluating.**

Rules:

1. The `idempotency_key` is a caller-supplied string on the signal body
2. The first call evaluates normally, stores the result, and returns it
3. All subsequent calls with the same key return the cached result immediately — no second evaluation, no trace, no event
4. The cached result is stored for the lifetime of the process (in-memory MVP)
5. An idempotent response is byte-identical to the original: same `id`, same `execution_id`, same `status`

**When to use it:**

- At-most-once delivery for upstream event systems
- Preventing duplicate execution events on retry
- Safe re-submission after a network timeout

**Recommended key format:**

```
{event_id}:{signal_id}:{flow_version}
```

Example: `evt_001:sig_001:v1`

---

## Boundary priority

When multiple BOUNDARY nodes trigger simultaneously, exactly one effect is applied — the one from the highest-severity triggered boundary.

**Severity order:**

```
critical (4) > high (3) > medium (2) > low (1)
```

**Tie-breaking:**

When two triggered boundaries share the same severity, the one that appears earlier in `flow.nodes` wins.

**Effect hierarchy:**

All boundary nodes are evaluated regardless of edges. The engine does not stop at the first triggered boundary — it evaluates all of them, then applies only the winning effect.

**Effect precedence** (independent of severity — severity determines which node wins, not which effect wins):

| Effect | Outcome |
|---|---|
| `block` | Decision is blocked; no execution |
| `escalate` | Decision requires human review |
| `override` | Decision action is replaced; execution continues |
| `redirect` | Decision action and node are replaced; execution continues |
| `allow` | No change; decision proceeds normally |

A boundary with `effect: allow` never overrides a boundary with `effect: block` unless the `allow` boundary has higher severity. The effect applied is always the effect of the highest-severity triggered boundary.

---

## Human gate rules

Human gates are created when a BOUNDARY node with `effect: escalate` triggers.

**Lifecycle:**

```
pending → approved → confirmed (status on DecisionResult)
pending → rejected → rejected  (status on DecisionResult)
```

**Rules:**

1. A gate request can only be acted on when it is in `PENDING` state. Attempting to approve or reject a non-PENDING gate returns HTTP 409.
2. Approving a gate sets `DecisionResult.status = CONFIRMED` but does NOT re-emit an `ExecutionRequest` event. The caller is responsible for triggering execution after approval if needed.
3. Rejecting a gate sets `DecisionResult.status = REJECTED` and clears `action = null`.
4. A gate can only be approved or rejected once. The `responded_at` timestamp is set on response.
5. Human gates do not time out automatically in the MVP. Timeout enforcement is reserved for a future async worker.
6. The `actor_id` field is recorded but not validated against a role system in the MVP.

---

## Execution request policy

An `ExecutionRequest` is created and an `EXECUTION_REQUESTED` event is emitted **only when `DecisionResult.status == CONFIRMED`**.

| Decision status | ExecutionRequest emitted? |
|---|---|
| `confirmed` | Yes — always |
| `fallback` | No |
| `blocked` | No |
| `pending_human` | No |
| `rejected` | No |
| `error` | No |

The `execution_id` is a unique UUID per confirmed evaluation. No two confirmed decisions share an `execution_id`.

After a human gate approve, the status becomes `confirmed` but no new `ExecutionRequest` is emitted. If the downstream orchestrator needs to be triggered post-approval, that integration point must be added explicitly.

---

## Fallback policy

Every flow must contain exactly one active FALLBACK node. The runtime validates this at load time.

The fallback node is selected when no DECISION node's condition evaluates to `true`. Its `action` payload is used as the decision action, and `status` is set to `FALLBACK` (not `CONFIRMED`).

Fallback decisions do not produce execution requests.

If a flow has no active fallback node at evaluation time, the engine raises a `RuntimeError`, which the API translates to HTTP 500.

---

## Failure handling

| Failure mode | Behavior |
|---|---|
| Invalid condition expression | `ConditionEvaluationError` → HTTP 400 |
| Flow not found | `FlowNotFoundError` → HTTP 404 |
| No fallback node | `RuntimeError` → HTTP 500 |
| Gate not found | `HumanGateNotFoundError` → HTTP 404 |
| Gate not in PENDING state | `HumanGateInvalidStateError` → HTTP 409 |
| Trace not found | `TraceNotFoundError` → HTTP 404 |
| Unexpected exception | HTTP 500 with error detail |

**Condition errors are not silently absorbed.** If a node's condition expression is syntactically invalid or uses a disallowed construct, the evaluation raises immediately. Flows with bad conditions fail at load time if `validate_conditions=True` is set on the `FlowValidator`.

---

## Condition expression safety

Conditions are evaluated using Python's `ast` module with a strict whitelist. The following constructs are permitted:

- Comparison operators: `==`, `!=`, `<`, `<=`, `>`, `>=`
- Boolean operators: `and`, `or`, `not`
- Attribute access: `payload.customer_tier` (resolves to dict lookup)
- Constants: strings, integers, floats, booleans, `None`
- Context variables: `type`, `confidence`, `payload`, `source`, `created_at`

The following are **structurally impossible**:

- Function calls
- Imports
- Assignments
- Subscript access (`payload["key"]` — use dot notation instead)
- Dunder attribute access (`.__class__`, etc.)
- Any variable name not in the allowed set

JSON-style literals are normalized before parsing:
- `true` → `True`
- `false` → `False`
- `null` → `None`

Missing dict keys resolve to `False` rather than raising a `KeyError`.

---

## Ordering guarantees

**Within a single `evaluate()` call:**

1. DECISION nodes are evaluated in `flow.nodes` declaration order
2. BOUNDARY nodes are evaluated in `flow.nodes` declaration order (before effect application)
3. Boundary effects are applied after all boundaries have been evaluated
4. Human gate creation happens after boundary application
5. ExecutionRequest creation and event emission happen after human gate creation
6. Trace save happens last — the trace captures the final `execution_id` and gate state

**EventBus:**

Events are stored in publication order and returned in the same order from `GET /api/runtime/events`. FIFO is guaranteed within a single process.

---

## In-memory MVP limitations

The current runtime is fully in-memory. This means:

- **No persistence across restarts.** Traces, gate requests, idempotency cache, and events are lost when the process stops.
- **Single-process only.** State is not shared between multiple instances. Do not run multiple replicas against the same workload in the MVP.
- **No TTL.** The idempotency store, trace store, and event bus grow unboundedly. In production, cap these with a Redis TTL or eviction policy.
- **No durability.** An `ExecutionRequest` event lost before the orchestrator reads it cannot be recovered without a persistent broker.

These limitations are design constraints of the MVP, not bugs. The interfaces are designed for swap-in with persistent backends without changing callers.
