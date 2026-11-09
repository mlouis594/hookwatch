"""Database engine and session handling.

The database location comes from HOOKWATCH_DB_URL and defaults to a
hookwatch.db file in the working directory.
"""

import os
from collections.abc import Iterator

from sqlmodel import Session, SQLModel, create_engine

DATABASE_URL = os.getenv("HOOKWATCH_DB_URL", "sqlite:///./hookwatch.db")

# SQLite connections are single-threaded by default; FastAPI may use a
# connection from a different thread than the one that created it.
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args)


def init_db() -> None:
    """Create any tables that don't exist yet."""
    from app import models  # noqa: F401  (importing registers the tables)

    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one database session per request."""
    with Session(engine) as session:
        yield session
