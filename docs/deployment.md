# Deployment Guide — v1.0

## Local Development

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and edit environment config
cp .env.example .env

# 3. Start with Docker Compose (includes Postgres + Redis)
docker compose up --build
# Service available at http://localhost:8002

# Or run directly (in-memory backends, zero config)
uvicorn backend.app.main:app --reload --port 8000
```

Visit `http://localhost:8000/docs` for the interactive OpenAPI UI.

---

## Production Configuration

All settings are read from environment variables or a `.env` file.
See `.env.example` for a full annotated list.

### Minimum Required

| Variable | Default | Description |
|---|---|---|
| `FLOW_DIR` | `backend/flows` | Path to YAML flow definitions |
| `LOG_LEVEL` | `INFO` | Python logging level |

---

## Health Checks

Two endpoints are available for orchestrators (Kubernetes, ECS, etc.):

| Endpoint | Purpose | Returns |
|---|---|---|
| `GET /health/live` | Liveness probe — is the process alive? | 200 always |
| `GET /health/ready` | Readiness probe — are all dependencies reachable? | 200 / 503 |

`/health/ready` checks each configured external dependency:

- **Redis** (when `EVENT_BUS_BACKEND=redis`) — `PING`
- **Postgres** (when `LEDGER_BACKEND=postgres`) — `SELECT 1`
- **Kafka** (when `EXECUTION_PUBLISHER_BACKEND=kafka`) — producer initialised

Response body (503 example):

```json
{
  "status": "degraded",
  "type": "ready",
  "checks": {
    "event_bus": "error",
    "ledger": "ok"
  }
}
```

### Kubernetes example

```yaml
livenessProbe:
  httpGet:
    path: /health/live
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 10

readinessProbe:
  httpGet:
    path: /health/ready
    port: 8000
  initialDelaySeconds: 10
  periodSeconds: 15
  failureThreshold: 3
```

---

## Postgres Ledger

```bash
LEDGER_ENABLED=true
LEDGER_BACKEND=postgres
LEDGER_DATABASE_URL=postgresql://user:password@db-host:5432/decision_ledger
LEDGER_MODE=parallel   # or strict
```

Apply the schema before starting:

```bash
psql $LEDGER_DATABASE_URL -f docs/sql/ledger_schema.sql
```

`LEDGER_MODE=parallel` (default): ledger write failures are logged but do not
block or modify the `DecisionResult` returned to the caller.

`LEDGER_MODE=strict`: ledger write failures return HTTP 500. Use when
auditability is a hard requirement (e.g. financial / compliance workflows).

---

## Redis Event Bus

```bash
EVENT_BUS_BACKEND=redis
REDIS_URL=redis://redis-host:6379/0
REDIS_EVENT_STREAM=runtime:events   # optional, defaults to runtime:events
```

Requires Redis 6.2+ for exclusive-range XRANGE (`(id` syntax).

---

## Kafka Execution Publisher

```bash
EXECUTION_PUBLISHER_BACKEND=kafka
KAFKA_BOOTSTRAP_SERVERS=kafka-host:9092
KAFKA_EXECUTION_TOPIC=runtime.execution.requested
```

Messages are keyed by `flow_id` (partitions by flow for ordering).
Consumers should be idempotent; use `execution_id` (UUID in the payload) as
the deduplication key.

---

## Auth / RBAC

```bash
AUTH_ENABLED=true
API_KEY_ROLE_MAP={"key-abc": {"actor_id": "alice", "roles": ["reviewer"]}, "key-xyz": {"actor_id": "svc-bot", "roles": ["admin"]}}
```

When `AUTH_ENABLED=true`, every `POST /api/runtime/human-gates/{id}/approve`
and `reject` request must include an `X-Api-Key` header.  The key is looked
up in `API_KEY_ROLE_MAP` to resolve the actor identity and roles.

Flows can declare a `required_role` on a `human_gate` node; actors without
that role receive HTTP 403.

---

## Observability

### Structured Logging

```bash
STRUCTURED_LOGGING_ENABLED=true
LOG_LEVEL=INFO
```

Every request produces a JSON access log line:

```json
{"request_id": "uuid", "method": "POST", "path": "/api/runtime/evaluate", "status_code": 200, "duration_ms": 12.4}
```

### Prometheus Metrics

```bash
OBSERVABILITY_ENABLED=true
METRICS_ENABLED=true
```

Scrape `GET /metrics` for Prometheus text format (version 0.0.4).

Key metrics:

| Metric | Type | Labels |
|---|---|---|
| `decision_evaluate_total` | counter | `status` |
| `decision_evaluate_duration_seconds` | summary | — |
| `human_gate_action_total` | counter | `action` |
| `ledger_append_total` | counter | `result` |

---

## Production Checklist

- [ ] `DEBUG=false`
- [ ] `AUTH_ENABLED=true` with strong API keys
- [ ] `LEDGER_ENABLED=true` + Postgres for audit trail
- [ ] `EVENT_BUS_BACKEND=redis` for persistence across restarts
- [ ] `LEDGER_MODE=strict` if write-or-fail semantics required
- [ ] Readiness probe configured in orchestrator
- [ ] `/metrics` scraping wired into Prometheus
- [ ] `STRUCTURED_LOGGING_ENABLED=true` with log aggregation (e.g. Datadog, Loki)
- [ ] `FLOW_DIR` mounted as a read-only volume (or baked into image)
- [ ] TLS termination at load balancer / ingress
