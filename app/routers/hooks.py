"""The endpoint webhook providers send to."""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from sqlmodel import Session

from app.db import get_session
from app.models import WebhookEvent

router = APIRouter(tags=["webhooks"])

MAX_BODY_BYTES = 1_000_000  # 1 MB; real webhook payloads are far smaller
REDACTED_HEADERS = {"authorization", "proxy-authorization", "cookie"}
SOURCE_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,49}$"


@router.post("/hooks/{source}")
async def receive_webhook(
    request: Request,
    source: Annotated[str, Path(pattern=SOURCE_PATTERN, description="Who sent it, e.g. stripe")],
    session: Annotated[Session, Depends(get_session)],
) -> dict:
    """Store an incoming webhook and acknowledge it.

    Providers retry when they don't get a fast 2xx, so this does as little
    as possible: save the request and return.
    """
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Body is {len(raw)} bytes; the limit is {MAX_BODY_BYTES}",
        )

    headers = {
        name: "[redacted]" if name in REDACTED_HEADERS else value
        for name, value in request.headers.items()
    }
    event = WebhookEvent(
        source=source,
        method=request.method,
        path=request.url.path,
        query=request.url.query,
        headers=json.dumps(headers),
        body=raw,
        content_type=request.headers.get("content-type"),
        size_bytes=len(raw),
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return {"id": event.id, "received": True}
