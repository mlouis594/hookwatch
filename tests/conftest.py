"""Shared test fixtures.

Each test gets a fresh in-memory database, so tests never touch
hookwatch.db and never affect each other.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from app import models  # noqa: F401  (registers the tables)
from app.db import get_engine
from app.main import app


@pytest.fixture(autouse=True)
def no_webhook_secrets(monkeypatch):
    """Start every test with no secrets set, whatever the developer's shell has.

    Tests that need a secret set it themselves with monkeypatch.setenv.
    """
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)


@pytest.fixture
def client():
    # StaticPool keeps one connection open, so the in-memory DB survives
    # across requests within a test.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)

    # Sessions (per request and in background replays) all come from this engine.
    app.dependency_overrides[get_engine] = lambda: engine
    # Not used as a context manager, so the startup hook (which would
    # create hookwatch.db) doesn't run.
    yield TestClient(app)
    app.dependency_overrides.clear()
