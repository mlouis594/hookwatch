"""FastAPI application entry point.

Run locally with:
    uvicorn app.main:app --reload
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.alerts import default_alerter
from app.checks import CheckScheduler, load_checks
from app.db import engine, init_db
from app.replay import recover_interrupted_replays
from app.routers import checks, dashboard, events, hooks

logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs every request at INFO


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()  # create tables on startup
    recover_interrupted_replays(engine)  # retries don't survive a restart

    # Health checks run for as long as the app does. Note: with several
    # workers (uvicorn --workers 4), each would run every check; run one.
    app.state.checks = load_checks(os.getenv("HOOKWATCH_CHECKS_FILE", "checks.yaml"))
    scheduler = CheckScheduler(app.state.checks, engine, default_alerter())
    scheduler.start()
    yield
    await scheduler.stop()


app = FastAPI(
    title="HookWatch",
    version=__version__,
    description="Catch, verify, inspect, and replay webhooks, and monitor API health.",
    lifespan=lifespan,
)
app.include_router(hooks.router)
app.include_router(events.router)
app.include_router(checks.router)
app.include_router(dashboard.router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    """Liveness check: returns 200 when the service is up."""
    return {"status": "ok", "version": __version__}
