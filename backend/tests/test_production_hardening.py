"""
Tests for Production Hardening (Step 14).

Contract under test:
    Health endpoints:
    1.  GET /health/live returns 200 {"status": "ok", "type": "live"}.
    2.  GET /health/ready returns 200 when all in-memory backends are used.
    3.  GET /health/ready returns 503 when RedisEventBus.is_ready() → False.
    4.  GET /health/ready returns 503 when PostgresLedgerClient.is_ready() → False.
    5.  GET /health/ready returns 503 when KafkaExecutionPublisher.is_ready() → False.
    6.  GET /health/ready body includes per-dependency "checks" dict.
    7.  GET /health/ready 503 body has status="degraded".
    8.  GET /health (legacy) still returns 200.

    Security headers:
    9.  X-Content-Type-Options: nosniff present on 200 responses.
    10. X-Frame-Options: DENY present on 200 responses.
    11. Referrer-Policy: no-referrer present on 200 responses.
    12. Content-Security-Policy present on 200 responses.
    13. Cache-Control: no-store present on 200 responses.
    14. Security headers present on 4xx responses.
    15. Security headers present on 5xx responses (404 from unknown route).

    is_ready() unit tests:
    16. RedisEventBus.is_ready() → True when ping succeeds.
    17. RedisEventBus.is_ready() → False when ping raises.
    18. PostgresLedgerClient.is_ready() → True when SELECT 1 succeeds.
    19. PostgresLedgerClient.is_ready() → False when connect raises.
    20. KafkaExecutionPublisher.is_ready() → True when producer set.
    21. KafkaExecutionPublisher.is_ready() → False when producer is None.

    SecurityHeadersMiddleware unit test:
    22. Headers are injected on non-2xx (e.g. 404) responses too.
"""
from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.app.config import settings
from backend.app.integrations.kafka_execution_publisher import KafkaExecutionPublisher
from backend.app.integrations.postgres_ledger_client import PostgresLedgerClient
from backend.app.integrations.redis_event_bus import RedisEventBus
from backend.app.main import app

_TEST_FLOWS_DIR = os.path.join(os.path.dirname(__file__), "flows")


# ------------------------------------------------------------------ #
# Fixtures                                                             #
# ------------------------------------------------------------------ #


@pytest.fixture
def api_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(settings, "flow_dir", _TEST_FLOWS_DIR)
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------------ #
# 1–8. Health endpoints                                                #
# ------------------------------------------------------------------ #


def test_health_live_returns_200(api_client: TestClient) -> None:
    resp = api_client.get("/health/live")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["type"] == "live"


def test_health_ready_all_memory_returns_200(api_client: TestClient) -> None:
    resp = api_client.get("/health/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["type"] == "ready"


def test_health_ready_redis_failure_returns_503(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing_bus = MagicMock()
    failing_bus.is_ready.return_value = False
    monkeypatch.setattr(app.state, "event_bus", failing_bus)

    resp = api_client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["event_bus"] == "error"


def test_health_ready_postgres_failure_returns_503(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing_ledger = MagicMock()
    failing_ledger.is_ready.return_value = False
    monkeypatch.setattr(app.state, "ledger_client", failing_ledger)

    resp = api_client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["ledger"] == "error"


def test_health_ready_kafka_failure_returns_503(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing_pub = MagicMock()
    failing_pub.is_ready.return_value = False
    monkeypatch.setattr(app.state, "execution_publisher", failing_pub)

    resp = api_client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["execution_publisher"] == "error"


def test_health_ready_body_includes_checks(api_client: TestClient) -> None:
    resp = api_client.get("/health/ready")
    assert resp.status_code == 200
    assert "checks" in resp.json()


def test_health_ready_503_body_has_degraded_status(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = MagicMock()
    failing.is_ready.return_value = False
    monkeypatch.setattr(app.state, "event_bus", failing)

    resp = api_client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded"


def test_health_legacy_returns_200(api_client: TestClient) -> None:
    resp = api_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ------------------------------------------------------------------ #
# 9–15. Security headers                                               #
# ------------------------------------------------------------------ #


def test_security_header_x_content_type_options(api_client: TestClient) -> None:
    resp = api_client.get("/health/live")
    assert resp.headers.get("x-content-type-options") == "nosniff"


def test_security_header_x_frame_options(api_client: TestClient) -> None:
    resp = api_client.get("/health/live")
    assert resp.headers.get("x-frame-options") == "DENY"


def test_security_header_referrer_policy(api_client: TestClient) -> None:
    resp = api_client.get("/health/live")
    assert resp.headers.get("referrer-policy") == "no-referrer"


def test_security_header_content_security_policy(api_client: TestClient) -> None:
    resp = api_client.get("/health/live")
    csp = resp.headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp


def test_security_header_cache_control(api_client: TestClient) -> None:
    resp = api_client.get("/health/live")
    assert resp.headers.get("cache-control") == "no-store"


def test_security_headers_on_4xx_response(api_client: TestClient) -> None:
    resp = api_client.get("/api/runtime/flows/nonexistent-flow-id")
    assert resp.status_code == 404
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"
    assert resp.headers.get("cache-control") == "no-store"


def test_security_headers_on_unknown_route(api_client: TestClient) -> None:
    resp = api_client.get("/no-such-route")
    assert resp.status_code == 404
    assert resp.headers.get("x-content-type-options") == "nosniff"


# ------------------------------------------------------------------ #
# 16–21. is_ready() unit tests                                         #
# ------------------------------------------------------------------ #


def test_redis_event_bus_is_ready_true() -> None:
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    bus = RedisEventBus(redis_url="redis://x", stream_name="s", redis_factory=lambda: mock_client)
    assert bus.is_ready() is True


def test_redis_event_bus_is_ready_false_on_exception() -> None:
    mock_client = MagicMock()
    mock_client.ping.side_effect = ConnectionError("refused")
    bus = RedisEventBus(redis_url="redis://x", stream_name="s", redis_factory=lambda: mock_client)
    assert bus.is_ready() is False


def test_postgres_ledger_client_is_ready_true() -> None:
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    client = PostgresLedgerClient(
        database_url="postgresql://x", connection_factory=lambda: mock_conn
    )
    assert client.is_ready() is True
    mock_cur.execute.assert_called_once_with("SELECT 1")


def test_postgres_ledger_client_is_ready_false_on_exception() -> None:
    def bad_factory():
        raise OSError("connection refused")

    client = PostgresLedgerClient(database_url="postgresql://x", connection_factory=bad_factory)
    assert client.is_ready() is False


def test_kafka_execution_publisher_is_ready_true() -> None:
    mock_producer = MagicMock()
    pub = KafkaExecutionPublisher(
        bootstrap_servers="localhost:9092",
        topic="t",
        producer_factory=lambda: mock_producer,
    )
    assert pub.is_ready() is True


def test_kafka_execution_publisher_is_ready_false_when_none() -> None:
    pub = KafkaExecutionPublisher(
        bootstrap_servers="localhost:9092",
        topic="t",
        producer_factory=lambda: None,
    )
    assert pub.is_ready() is False


# ------------------------------------------------------------------ #
# 22. SecurityHeadersMiddleware unit test                              #
# ------------------------------------------------------------------ #


def test_security_headers_middleware_on_503(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = MagicMock()
    failing.is_ready.return_value = False
    monkeypatch.setattr(app.state, "event_bus", failing)

    resp = api_client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("cache-control") == "no-store"
