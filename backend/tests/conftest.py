"""Shared pytest fixtures for the decision-runtime-core test suite."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from backend.app.config import settings
from backend.app.main import app

_TEST_FLOWS_DIR = os.path.join(os.path.dirname(__file__), "flows")


@pytest.fixture
def test_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Return a TestClient wired to the real FastAPI app but using test flows.

    Each call creates a fresh TestClient (fresh lifespan → fresh app.state),
    so EventBus, TraceStore, HumanGateManager, and IdempotencyStore all start
    empty.  The flow_dir is patched to point at backend/tests/flows/ so only
    the controlled test YAML files are loaded.
    """
    monkeypatch.setattr(settings, "flow_dir", _TEST_FLOWS_DIR)
    with TestClient(app) as client:
        yield client
