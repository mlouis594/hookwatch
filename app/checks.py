"""Scheduled API health checks, with alerts only when a check changes state.

Each check in checks.yaml is an HTTP request made every `interval_seconds`.
It passes when the response arrives within `timeout_seconds` with the
expected status code. Every run is stored as a CheckResult, and a
CheckState row tracks whether the check is currently up or down.

Alerting rule (the alert-fatigue lesson): alert when a check goes DOWN or
RECOVERS, never on every failed run. Ten failed runs during one outage
should mean one alert, then one "recovered" message, not ten alerts that
teach people to mute the channel. `failures_before_down` adds a second
guard: with 2, a single blip doesn't page anyone.

The scheduler is a plain asyncio loop: one task per check, started in the
app's lifespan. The check itself is ordinary synchronous code, run in a
worker thread so the HTTP call and database write never block the server.
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml
from pydantic import BaseModel, Field, HttpUrl, ValidationError, field_validator
from sqlalchemy import Engine
from sqlmodel import Session

from app.alerts import Alerter
from app.models import CheckResult, CheckState, utcnow

logger = logging.getLogger("hookwatch.checks")

UNKNOWN = "unknown"
UP = "up"
DOWN = "down"


# --- Configuration ----------------------------------------------------------


class CheckConfig(BaseModel):
    """One entry in checks.yaml."""

    name: str = Field(min_length=1, max_length=100)
    url: HttpUrl
    method: str = "GET"
    expected_status: int = Field(default=200, ge=100, le=599)
    interval_seconds: float = Field(default=60, ge=5)  # floor: don't hammer anyone
    timeout_seconds: float = Field(default=10, gt=0)
    failures_before_down: int = Field(default=1, ge=1)

    @field_validator("method")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()


class ChecksFile(BaseModel):
    checks: list[CheckConfig] = []

    @field_validator("checks")
    @classmethod
    def _unique_names(cls, checks: list[CheckConfig]) -> list[CheckConfig]:
        names = [c.name for c in checks]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"duplicate check names: {', '.join(duplicates)}")
        return checks


class ChecksConfigError(Exception):
    """checks.yaml exists but is invalid. Raised at startup so mistakes are loud."""


def load_checks(path: str | Path) -> list[CheckConfig]:
    """Read checks from a YAML file. A missing file means no checks (and a log line)."""
    path = Path(path)
    if not path.exists():
        logger.info("No checks file at %s; health checks are off", path)
        return []
    try:
        data = yaml.safe_load(path.read_text()) or {}
        return ChecksFile.model_validate(data).checks
    except (yaml.YAMLError, ValidationError) as exc:
        raise ChecksConfigError(f"Invalid {path}: {exc}") from exc


# --- Latency statistics -----------------------------------------------------


def percentile(values: list[int], pct: float) -> int | None:
    """Nearest-rank percentile: the smallest value that pct% of values are at or below.

    p95 is used instead of the average because averages hide the slow
    requests: 19 fast responses and one 9-second one average to "fine".
    p95 answers "how slow is it for the unlucky 1 in 20?"
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(pct / 100 * len(ordered))
    return ordered[max(rank, 1) - 1]


# --- Running one check ------------------------------------------------------


@dataclass
class Probe:
    ok: bool
    status_code: int | None
    latency_ms: int
    error: str | None


def probe(check: CheckConfig, transport: httpx.BaseTransport | None = None) -> Probe:
    """Make the HTTP request and judge it. Never raises."""
    start = time.perf_counter()
    try:
        with httpx.Client(transport=transport, timeout=check.timeout_seconds) as client:
            response = client.request(check.method, str(check.url))
    except httpx.TimeoutException:
        return Probe(False, None, _ms_since(start), f"Timed out after {check.timeout_seconds:g}s")
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        return Probe(False, None, _ms_since(start), error)

    latency = _ms_since(start)
    if response.status_code != check.expected_status:
        error = f"Expected HTTP {check.expected_status}, got {response.status_code}"
        return Probe(False, response.status_code, latency, error)
    return Probe(True, response.status_code, latency, None)


def _ms_since(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


def next_state(current: str, consecutive_failures: int, ok: bool, failures_before_down: int) -> str:
    """Where a check goes after one result. Pure function, the heart of the alert rule."""
    if ok:
        return UP
    if consecutive_failures >= failures_before_down:
        return DOWN
    return current  # failing, but not enough times in a row to call it down yet


def run_check(
    check: CheckConfig,
    engine: Engine,
    alert: Alerter,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
) -> CheckState:
    """Run one check: probe, store the result, update state, alert on a change."""
    result = probe(check, transport)
    now = now or utcnow()

    with Session(engine) as session:
        session.add(
            CheckResult(
                check_name=check.name,
                ok=result.ok,
                status_code=result.status_code,
                latency_ms=result.latency_ms,
                error=result.error,
                checked_at=now,
            )
        )
        state = session.get(CheckState, check.name) or CheckState(name=check.name)
        previous = state.state
        previous_change = state.last_changed_at

        state.consecutive_failures = 0 if result.ok else state.consecutive_failures + 1
        state.state = next_state(previous, state.consecutive_failures, result.ok, check.failures_before_down)
        if state.state != previous:
            state.last_changed_at = now
        state.last_checked_at = now
        state.last_status_code = result.status_code
        state.last_latency_ms = result.latency_ms
        state.last_error = result.error
        session.add(state)
        session.commit()
        session.refresh(state)

    # Alert after the state is saved. "unknown -> up" (first run, all fine) is silent.
    if state.state == DOWN and previous != DOWN:
        alert(_down_message(check, result))
    elif state.state == UP and previous == DOWN:
        alert(_recovered_message(check, result, previous_change, now))
    return state


def _down_message(check: CheckConfig, result: Probe) -> str:
    return f":red_circle: *{check.name}* is DOWN: {result.error} ({check.method} {check.url})"


def _recovered_message(check: CheckConfig, result: Probe, down_since: datetime | None, now: datetime) -> str:
    outage = f" after {_human_duration(now - _aware(down_since))}" if down_since else ""
    return (
        f":large_green_circle: *{check.name}* recovered{outage} "
        f"(HTTP {result.status_code} in {result.latency_ms} ms)"
    )


def _aware(dt: datetime) -> datetime:
    # SQLite hands datetimes back without a timezone; ours are always UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _human_duration(delta) -> str:
    seconds = max(int(delta.total_seconds()), 0)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


# --- Scheduling -------------------------------------------------------------


class CheckScheduler:
    """Runs every check on its own interval until stopped."""

    def __init__(
        self,
        checks: list[CheckConfig],
        engine: Engine,
        alert: Alerter,
        transport: httpx.BaseTransport | None = None,
    ):
        self.checks = checks
        self.engine = engine
        self.alert = alert
        self.transport = transport
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        for check in self.checks:
            self._tasks.append(asyncio.create_task(self._loop(check), name=f"check:{check.name}"))
        if self.checks:
            logger.info("Started %d health check(s)", len(self.checks))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _loop(self, check: CheckConfig) -> None:
        while True:
            try:
                await asyncio.to_thread(run_check, check, self.engine, self.alert, self.transport)
            except Exception:
                # A bug or a database hiccup in one run must not kill the loop for good.
                logger.exception("Health check %r crashed; will try again next interval", check.name)
            await asyncio.sleep(check.interval_seconds)
