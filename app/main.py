"""FastAPI application entry point.

Run locally with:
    uvicorn app.main:app --reload
"""

from fastapi import FastAPI

from app import __version__

app = FastAPI(
    title="HookWatch",
    version=__version__,
    description="Catch, verify, inspect, and replay webhooks, and monitor API health.",
)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    """Liveness check: returns 200 when the service is up."""
    return {"status": "ok", "version": __version__}
