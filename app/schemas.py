"""Response shapes returned by the API."""

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, HttpUrl

from app.models import DeliveryAttempt, WebhookEvent


class EventSummary(BaseModel):
    id: int
    source: str
    method: str
    content_type: str | None
    size_bytes: int
    signature_status: str
    signature_detail: str | None
    replay_status: str | None
    received_at: datetime


class EventDetail(EventSummary):
    path: str
    query: str
    headers: dict[str, str]
    body: str
    json_body: Any | None = None


class ReplayRequest(BaseModel):
    target_url: HttpUrl


class ReplayAccepted(BaseModel):
    event_id: int
    target_url: str
    replay_status: str
    attempts_url: str


class AttemptOut(BaseModel):
    attempt_number: int
    target_url: str
    status_code: int | None
    error: str | None
    response_excerpt: str | None
    duration_ms: int
    attempted_at: datetime


class CheckStatus(BaseModel):
    name: str
    url: str
    method: str
    interval_seconds: float
    state: str  # "unknown", "up", or "down"
    last_status_code: int | None
    last_latency_ms: int | None
    p95_latency_ms: int | None  # over the last 100 runs
    last_error: str | None
    last_checked_at: datetime | None
    last_changed_at: datetime | None


class CheckResultOut(BaseModel):
    ok: bool
    status_code: int | None
    latency_ms: int
    error: str | None
    checked_at: datetime


def to_attempt(attempt: DeliveryAttempt) -> AttemptOut:
    return AttemptOut.model_validate(attempt, from_attributes=True)


def to_summary(event: WebhookEvent) -> EventSummary:
    return EventSummary(
        id=event.id,
        source=event.source,
        method=event.method,
        content_type=event.content_type,
        size_bytes=event.size_bytes,
        signature_status=event.signature_status,
        signature_detail=event.signature_detail,
        replay_status=event.replay_status,
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
