"""Shared test fixtures.

Each test gets a fresh in-memory database, so tests never touch
hookwatch.db and never affect each other.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401  (registers the tables)
from app.db import get_session
from app.main import app


@pytest.fixture
def client():
    # StaticPool keeps one connection open, so the in-memory DB survives
    # across requests within a test.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    # Not used as a context manager, so the startup hook (which would
    # create hookwatch.db) doesn't run.
    yield TestClient(app)
    app.dependency_overrides.clear()
