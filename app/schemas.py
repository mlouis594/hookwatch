"""Response shapes returned by the API."""

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.models import WebhookEvent


class EventSummary(BaseModel):
    id: int
    source: str
    method: str
    content_type: str | None
    size_bytes: int
    signature_status: str
    received_at: datetime


class EventDetail(EventSummary):
    path: str
    query: str
    headers: dict[str, str]
    body: str
    json_body: Any | None = None


def to_summary(event: WebhookEvent) -> EventSummary:
    return EventSummary(
        id=event.id,
        source=event.source,
        method=event.method,
        content_type=event.content_type,
        size_bytes=event.size_bytes,
        signature_status=event.signature_status,
        received_at=event.received_at,
    )


def to_detail(event: WebhookEvent) -> EventDetail:
    body_text = event.body.decode("utf-8", errors="replace")
    json_body = None
    if "json" in (event.content_type or ""):
        try:
            json_body = json.loads(body_text)
        except ValueError:
            pass  # claimed to be JSON but wasn't; the raw body is still shown
    return EventDetail(
        **to_summary(event).model_dump(),
        path=event.path,
        query=event.query,
        headers=json.loads(event.headers),
        body=body_text,
        json_body=json_body,
    )
