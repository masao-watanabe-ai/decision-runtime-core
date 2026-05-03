# Flow Format

Flows are defined in YAML and loaded at startup from the directory configured in `settings.flow_dir` (default: `backend/flows/`).

Each YAML file defines one `DecisionFlow`. The runtime loads all `.yaml` and `.yml` files from the configured directory and validates them on startup.

---

## Top-level structure

```yaml
flow_id: call_center_escalation        # required — unique identifier used in API calls
name: Call Center Escalation Flow      # required — human-readable label
version: 1.0.0                         # required — semantic version (MAJOR.MINOR.PATCH)
description: "..."                     # optional — human-readable description
entry_node_id: vip_check               # required — ID of the first node to evaluate
is_active: true                        # optional — false disables the flow (default: true)
metadata: {}                           # optional — resolution_policy, execution_policy, etc.

nodes:
  - ...                                # required — at least one DECISION + one FALLBACK

edges:
  - ...                                # optional — directed connections between nodes
```

---

## Node types

### decision

A node that evaluates a condition against the signal context. The engine collects all matching DECISION nodes and selects one using the resolution policy.

```yaml
- id: vip_check
  name: VIP Customer Check
  node_type: decision
  condition: 'payload.customer_tier == "vip"'
  priority: 20
  action:
    type: prioritize
    target: support_queue
    parameters:
      priority: high
      lane: vip
  is_active: true
  config:
    contract_type: customer_tier_check
    contract_version: 1.0.0
```

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Unique within the flow |
| `name` | string | yes | Human-readable label |
| `node_type` | `decision` | yes | Node type identifier |
| `condition` | string | no | Boolean expression; `null` always matches |
| `priority` | integer ≥ 0 | no | Used by `priority` resolution strategy (default: 0) |
| `action` | object | no | Payload passed to execution when this node is selected |
| `is_active` | boolean | no | Inactive nodes are skipped (default: true) |
| `config.contract_type` | string | yes | Non-empty contract type identifier |
| `config.contract_version` | string | yes | Semantic version of the inline contract |

**Condition expressions** are evaluated against a fixed context:

| Variable | Type | Source |
|---|---|---|
| `type` | string | `signal.type` |
| `confidence` | float | `signal.confidence` |
| `payload` | object | `signal.payload` |
| `source` | string | `signal.source` |
| `created_at` | datetime | `signal.timestamp` |

Dot notation resolves to nested dict keys: `payload.customer_tier` → `signal.payload["customer_tier"]`.

Supported operators: `==`, `!=`, `<`, `<=`, `>`, `>=`, `and`, `or`, `not`.

JSON literals are normalized: `true` → `True`, `false` → `False`, `null` → `None`.

Missing keys resolve to `False` rather than raising an error.

---

### boundary

A node that enforces a business rule. The `BoundaryEngine` evaluates ALL active BOUNDARY nodes in the flow, independent of edges. When multiple boundaries trigger, the one with the highest severity wins.

```yaml
- id: rate_limit_boundary
  name: Escalation Rate Limit Boundary
  node_type: boundary
  condition: "payload.escalation_count >= 100"
  severity: high
  effect: block
  config:
    contract_type: escalation_rate_limit
    contract_version: 1.0.0
```

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Unique within the flow |
| `node_type` | `boundary` | yes | |
| `condition` | string | no | When `null`, boundary always triggers |
| `severity` | string | yes | `critical` / `high` / `medium` / `low` |
| `effect` | string | yes | Action taken when triggered (see below) |
| `action` | object | no | Used with `override` and `redirect` effects |
| `config.contract_type` | string | yes | |
| `config.contract_version` | string | yes | |

**Severity order** (higher wins when multiple trigger):

```
critical (4) > high (3) > medium (2) > low (1)
```

**Effects:**

| Effect | What happens to the decision |
|---|---|
| `allow` | No change — boundary is informational only |
| `block` | `status = blocked`, `action = null` — execution halted |
| `escalate` | `status = pending_human` — decision sent to human review |
| `override` | `action` replaced by boundary's `action` payload |
| `redirect` | `action` replaced and `selected_node_id` updated |

---

### human_gate

A structural node indicating a human review checkpoint. In the current runtime, `HUMAN_GATE` nodes are not evaluated directly — escalation is triggered by a `BOUNDARY` node with `effect: escalate`. The `human_gate` node type is preserved for flow visualization and future gate-first flows.

```yaml
- id: supervisor_gate
  name: Supervisor Approval Gate
  node_type: human_gate
  config:
    timeout_seconds: 1800
    assignee_role: supervisor
    title: VIP Escalation Requires Supervisor Approval
    question: Please review and approve or reject this VIP escalation request
```

---

### fallback

The catch-all node selected when no DECISION node's condition matches. Every flow must have exactly one active FALLBACK node.

```yaml
- id: default_fallback
  name: Default Fallback Handler
  node_type: fallback
  action:
    type: route
    target: support_queue
    parameters:
      priority: normal
  config:
    contract_type: default_queue_routing
    contract_version: 1.0.0
```

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Unique within the flow |
| `node_type` | `fallback` | yes | |
| `action` | object | no | Fallback action payload |
| `config.contract_type` | string | yes | |
| `config.contract_version` | string | yes | |

---

## Edges

Edges define valid traversal paths between nodes. They are used for:
- Determining reachability (the validator rejects unreachable non-fallback nodes)
- Visualizing flow topology in diagram tools

Edges do not control the engine's evaluation order. The engine evaluates all DECISION nodes and all BOUNDARY nodes independently.

```yaml
edges:
  - id: vip_to_boundary
    source_node_id: vip_check
    target_node_id: rate_limit_boundary
    condition_expression: "outcome == 'pass'"
    label: VIP Confirmed
    priority: 0
```

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Unique within the flow |
| `source_node_id` | string | yes | Must reference an existing node |
| `target_node_id` | string | yes | Must reference an existing node |
| `condition_expression` | string | no | Gate expression for the edge |
| `label` | string | no | Human-readable label for visualization |
| `priority` | integer ≥ 0 | no | Traversal priority (lower = higher priority) |

---

## Metadata

The `metadata` block holds flow-level policy settings.

### resolution_policy

Controls how the engine selects a winner when multiple DECISION nodes match.

```yaml
metadata:
  resolution_policy:
    strategy: priority        # "priority" | "first_match" (default)
    priority_field: customer_tier
    tiebreaker: fifo
```

| Strategy | Behavior |
|---|---|
| `first_match` | First matching node in `nodes` declaration order wins (default) |
| `priority` | Node with highest `priority` value wins; ties broken by position |

### execution_policy

Hints for downstream execution systems. Not evaluated by the runtime core.

```yaml
metadata:
  execution_policy:
    mode: sequential
    retry: true
    max_retries: 3
    timeout_seconds: 300
```

### error_policy

Defines fallback behavior on runtime errors. Not currently enforced by the core.

```yaml
metadata:
  error_policy:
    on_error: fallback
    fallback_node_id: default_fallback
    max_retries: 3
    alert_on_failure: true
```

---

## Validation rules

The `FlowValidator` enforces these rules on every loaded flow:

| Rule | Description |
|---|---|
| At least one node | Flow must have at least one node |
| Unique node IDs | No two nodes may share an ID |
| Valid node types | All `node_type` values must be known |
| Entry node exists | `entry_node_id` must reference a node in the flow |
| Integer priorities | All edge `priority` values must be integers |
| Valid edge references | Edge source and target must reference existing nodes |
| Exactly one fallback | Each flow must have exactly one active FALLBACK node |
| No orphan nodes | All non-fallback nodes must be reachable from `entry_node_id` via edges |
| No cycles | Directed cycles are disallowed unless `metadata.allow_cycles: true` |
| Valid inline contracts | Every DECISION, BOUNDARY, and FALLBACK node must declare a non-empty `contract_type` and valid semver `contract_version` in `config` |

---

## Complete example

```yaml
flow_id: call_center_escalation
name: Call Center Escalation Flow
version: 1.0.0
description: Routes incoming call center requests by customer tier, enforces rate limits, and requires supervisor approval for VIP escalations
entry_node_id: vip_check
is_active: true
metadata:
  resolution_policy:
    strategy: priority
  execution_policy:
    mode: sequential
    retry: true
    max_retries: 3
    timeout_seconds: 300

nodes:
  - id: vip_check
    name: VIP Customer Check
    node_type: decision
    condition: 'payload.customer_tier == "vip"'
    priority: 20
    action:
      type: prioritize
      target: support_queue
      parameters:
        priority: high
        lane: vip
    config:
      contract_type: customer_tier_check
      contract_version: 1.0.0

  - id: rate_limit_boundary
    name: Escalation Rate Limit Boundary
    node_type: boundary
    condition: "payload.escalation_count >= 100"
    severity: high
    effect: block
    config:
      contract_type: escalation_rate_limit
      contract_version: 1.0.0

  - id: default_fallback
    name: Default Fallback Handler
    node_type: fallback
    action:
      type: route
      target: support_queue
      parameters:
        priority: normal
    config:
      contract_type: default_queue_routing
      contract_version: 1.0.0

edges:
  - id: vip_to_boundary
    source_node_id: vip_check
    target_node_id: rate_limit_boundary
    priority: 0
```
