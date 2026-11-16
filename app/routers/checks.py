"""Endpoints for the scheduled API health checks."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlmodel import Session, col, select

from app.checks import UNKNOWN, CheckConfig, percentile
from app.db import get_session
from app.models import CheckResult, CheckState
from app.schemas import CheckResultOut, CheckStatus

router = APIRouter(prefix="/checks", tags=["checks"])


def get_check_configs(request: Request) -> list[CheckConfig]:
    """The checks loaded from checks.yaml at startup. Overridden in tests."""
    return getattr(request.app.state, "checks", [])


Checks = Annotated[list[CheckConfig], Depends(get_check_configs)]


P95_WINDOW = 100  # runs


def check_statuses(checks: list[CheckConfig], session: Session) -> list[CheckStatus]:
    """Current status of every configured check. Shared by the API and the dashboard."""
    statuses = []
    for check in checks:
        state = session.get(CheckState, check.name) or CheckState(name=check.name, state=UNKNOWN)
        recent = session.exec(
            select(CheckResult.latency_ms)
            .where(CheckResult.check_name == check.name)
            .order_by(col(CheckResult.id).desc())
            .limit(P95_WINDOW)
        ).all()
        statuses.append(
            CheckStatus(
                name=check.name,
                url=str(check.url),
                method=check.method,
                interval_seconds=check.interval_seconds,
                state=state.state,
                last_status_code=state.last_status_code,
                last_latency_ms=state.last_latency_ms,
                p95_latency_ms=percentile(list(recent), 95),
                last_error=state.last_error,
                last_checked_at=state.last_checked_at,
                last_changed_at=state.last_changed_at,
            )
        )
    return statuses


@router.get("", response_model=list[CheckStatus])
def list_checks(checks: Checks, session: Annotated[Session, Depends(get_session)]) -> list[CheckStatus]:
    """Every configured check: up, down, or unknown, latency (last and p95), and when it last changed."""
    return check_statuses(checks, session)


@router.get("/{name}/results", response_model=list[CheckResultOut])
def list_results(
    name: str,
    checks: Checks,
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[CheckResultOut]:
    """Recent runs of one check, newest first."""
    if name not in {c.name for c in checks}:
        raise HTTPException(status_code=404, detail=f"No check named {name!r}")
    statement = (
        select(CheckResult)
        .where(CheckResult.check_name == name)
        .order_by(col(CheckResult.id).desc())
        .limit(limit)
    )
    return [CheckResultOut.model_validate(r, from_attributes=True) for r in session.exec(statement)]
