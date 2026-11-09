"""Endpoints for browsing stored webhook events."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, col, select

from app.db import get_session
from app.models import WebhookEvent
from app.schemas import EventDetail, EventSummary, to_detail, to_summary

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=list[EventSummary])
def list_events(
    session: Annotated[Session, Depends(get_session)],
    source: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[EventSummary]:
    """List events, newest first. Filter with ?source=stripe."""
    statement = select(WebhookEvent)
    if source:
        statement = statement.where(WebhookEvent.source == source)
    statement = statement.order_by(col(WebhookEvent.id).desc()).offset(offset).limit(limit)
    return [to_summary(event) for event in session.exec(statement)]


@router.get("/{event_id}", response_model=EventDetail)
def get_event(
    event_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> EventDetail:
    """Everything about one event: headers, raw body, and parsed JSON."""
    event = session.get(WebhookEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"Event {event_id} not found")
    return to_detail(event)
