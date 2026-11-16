"""The endpoint webhook providers send to."""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.db import get_session
from app.models import WebhookEvent
from app.verification import REJECTED, verify

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
    """Store an incoming webhook, verify its signature, and acknowledge it.

    Providers retry when they don't get a fast 2xx, so this does as little
    as possible: verify, save the request, and return.

    Rejected events are still stored (support needs to see failures), but
    the response is a 400 so the sender's dashboard shows the failure too.
    """
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,  # Content Too Large
            detail=f"Body is {len(raw)} bytes; the limit is {MAX_BODY_BYTES}",
        )

    # Verify against the raw bytes and the real (unredacted) headers.
    result = verify(source, raw, request.headers)

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
        signature_status=result.status,
        signature_detail=result.detail,
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    response = {
        "id": event.id,
        "received": True,
        "signature_status": event.signature_status,
        "signature_detail": event.signature_detail,
    }
    if result.status == REJECTED:
        return JSONResponse(status_code=400, content=response)
    return response
