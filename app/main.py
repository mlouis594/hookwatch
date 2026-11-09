"""FastAPI application entry point.

Run locally with:
    uvicorn app.main:app --reload
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.db import init_db
from app.routers import events, hooks


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()  # create tables on startup
    yield


app = FastAPI(
    title="HookWatch",
    version=__version__,
    description="Catch, verify, inspect, and replay webhooks, and monitor API health.",
    lifespan=lifespan,
)
app.include_router(hooks.router)
app.include_router(events.router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    """Liveness check: returns 200 when the service is up."""
    return {"status": "ok", "version": __version__}
