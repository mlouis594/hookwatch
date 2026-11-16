"""Endpoints for browsing stored webhook events."""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from app.db import get_engine, get_session
from app.models import DeliveryAttempt, WebhookEvent
from app.replay import PENDING, ReplayConfig, claim_for_replay, deliver, get_replay_config
from app.schemas import (
    AttemptOut,
    EventDetail,
    EventSummary,
    ReplayAccepted,
    ReplayRequest,
    to_attempt,
    to_detail,
    to_summary,
)

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=list[EventSummary])
def list_events(
    session: Annotated[Session, Depends(get_session)],
    source: str | None = None,
    signature_status: str | None = None,
    replay_status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[EventSummary]:
    """List events, newest first.

    Filter with ?source=stripe, ?signature_status=rejected, or
    ?replay_status=dead_letter (replays that gave up and need a human).
    """
    statement = select(WebhookEvent)
    if source:
        statement = statement.where(WebhookEvent.source == source)
    if signature_status:
        statement = statement.where(WebhookEvent.signature_status == signature_status)
    if replay_status:
        statement = statement.where(WebhookEvent.replay_status == replay_status)
    statement = statement.order_by(col(WebhookEvent.id).desc()).offset(offset).limit(limit)
    return [to_summary(event) for event in session.exec(statement)]


@router.get("/{event_id}", response_model=EventDetail)
def get_event(
    event_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> EventDetail:
    """Everything about one event: headers, raw body, and parsed JSON."""
    return to_detail(_get_or_404(session, event_id))


@router.post("/{event_id}/replay", response_model=ReplayAccepted, status_code=202)
def replay_event(
    event_id: int,
    request: ReplayRequest,
    background_tasks: BackgroundTasks,
    session: Annotated[Session, Depends(get_session)],
    engine: Annotated[Engine, Depends(get_engine)],
    config: Annotated[ReplayConfig, Depends(get_replay_config)],
) -> ReplayAccepted:
    """Re-send the event's original body and headers to target_url.

    Answers 202 right away; delivery and retries happen in the background.
    Follow progress at /events/{id}/attempts. Returns 409 if a replay of
    this event is still running.
    """
    _get_or_404(session, event_id)
    if not claim_for_replay(session, event_id):
        raise HTTPException(
            status_code=409,
            detail=f"Event {event_id} is already being replayed; see /events/{event_id}/attempts",
        )

    target_url = str(request.target_url)
    background_tasks.add_task(deliver, engine, event_id, target_url, config)
    return ReplayAccepted(
        event_id=event_id,
        target_url=target_url,
        replay_status=PENDING,
        attempts_url=f"/events/{event_id}/attempts",
    )


@router.get("/{event_id}/attempts", response_model=list[AttemptOut])
def list_attempts(
    event_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> list[AttemptOut]:
    """Every delivery attempt for this event, oldest first."""
    _get_or_404(session, event_id)
    statement = (
        select(DeliveryAttempt)
        .where(DeliveryAttempt.event_id == event_id)
        .order_by(col(DeliveryAttempt.id))
    )
    return [to_attempt(attempt) for attempt in session.exec(statement)]


def _get_or_404(session: Session, event_id: int) -> WebhookEvent:
    event = session.get(WebhookEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"Event {event_id} not found")
    return event
