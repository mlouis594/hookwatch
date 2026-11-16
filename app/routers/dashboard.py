"""The dashboard: one server-rendered HTML page, no JavaScript build.

It reads the same data as the JSON API, so anything on the page can also
be fetched with curl. Jinja2 escapes every value, so a webhook or check
name can't inject HTML into the page.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlmodel import Session, col, select

from app import __version__
from app.checks import DOWN, CheckConfig
from app.db import get_session
from app.models import WebhookEvent
from app.replay import DEAD_LETTER
from app.routers.checks import check_statuses, get_check_configs
from app.schemas import to_summary

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")

RECENT_EVENTS = 50
REFRESH_SECONDS = 30


def _ago(value: datetime | None, now: datetime | None = None) -> str:
    """'just now', '42s ago', '5m ago', '3h ago', '2d ago'."""
    if value is None:
        return "never"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)  # SQLite drops the zone; ours are UTC
    seconds = int(((now or datetime.now(timezone.utc)) - value).total_seconds())
    if seconds < 5:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return f"{seconds}s ago"


def _utc(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")


templates.env.filters["ago"] = _ago
templates.env.filters["utc"] = _utc


def _count(session: Session, *conditions) -> int:
    return session.exec(select(func.count()).select_from(WebhookEvent).where(*conditions)).one()


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    checks: Annotated[list[CheckConfig], Depends(get_check_configs)],
) -> HTMLResponse:
    events = session.exec(
        select(WebhookEvent).order_by(col(WebhookEvent.id).desc()).limit(RECENT_EVENTS)
    ).all()
    statuses = check_statuses(checks, session)
    totals = {
        "events": _count(session),
        "rejected": _count(session, WebhookEvent.signature_status == "rejected"),
        "dead_letter": _count(session, WebhookEvent.replay_status == DEAD_LETTER),
        "checks_down": sum(1 for s in statuses if s.state == DOWN),
        "checks": len(statuses),
    }
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "events": [to_summary(e) for e in events],
            "checks": statuses,
            "totals": totals,
            "recent_limit": RECENT_EVENTS,
            "refresh_seconds": REFRESH_SECONDS,
            "version": __version__,
        },
    )
