"""Replay a stored webhook to a target URL, with retries and a dead-letter state.

A replay re-sends the event exactly as it arrived: the same raw body bytes
and the original headers (including the provider's signature header), plus
an X-HookWatch-Replay header so the target can tell it's a replay.

Retry policy:
- Retry on things that might fix themselves: connection errors, timeouts,
  5xx responses, and 429 (rate limited). Wait 1, 2, 4, then 8 seconds
  between tries (exponential backoff), so a struggling target gets room to
  recover instead of being hammered. Five attempts at most.
- Don't retry other 4xx responses. The target understood the request and
  refused it (bad signature, unknown endpoint); sending the same bytes again
  will get the same answer.
- When no attempt succeeds, the event is marked "dead_letter": parked for
  a human to look at, the same idea as a dead-letter queue.

The retries run in a FastAPI background task, so the API answers 202
straight away. That's fine for a single-process tool; the trade-off is that
a restart mid-retry loses the remaining attempts. A production system would
put replays on a durable queue (Celery, RQ, SQS) instead.
"""

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import httpx
from sqlalchemy import Engine, or_, update
from sqlmodel import Session, col

from app.models import DeliveryAttempt, WebhookEvent

PENDING = "pending"
DELIVERED = "delivered"
DEAD_LETTER = "dead_letter"

# Seconds to wait before attempts 2, 3, 4, and 5.
RETRY_DELAYS: tuple[float, ...] = (1, 2, 4, 8)
TIMEOUT_SECONDS = 10.0
RESPONSE_EXCERPT_CHARS = 500
REPLAY_HEADER = "X-HookWatch-Replay"

# Not forwarded: these describe the original connection, not the webhook,
# and httpx sets correct values for the new request itself.
SKIPPED_HEADERS = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
    "proxy-connection",
    "accept-encoding",
    REPLAY_HEADER.lower(),
}


@dataclass
class ReplayConfig:
    """Knobs for the retry loop. Tests swap in a fake transport and sleep."""

    delays: Sequence[float] = RETRY_DELAYS
    timeout: float = TIMEOUT_SECONDS
    transport: httpx.BaseTransport | None = None  # None means real network
    sleep: Callable[[float], None] = field(default=time.sleep)

    @property
    def max_attempts(self) -> int:
        return len(self.delays) + 1


def get_replay_config() -> ReplayConfig:
    """FastAPI dependency, overridden in tests."""
    return ReplayConfig()


def claim_for_replay(session: Session, event_id: int) -> bool:
    """Mark the event pending, unless a replay of it is already running.

    One conditional UPDATE does the check and the write together, so two
    replay requests arriving at once can't both start: only one changes a row.
    Returns False if the event was already pending.
    """
    result = session.execute(
        update(WebhookEvent)
        .where(col(WebhookEvent.id) == event_id)
        .where(or_(col(WebhookEvent.replay_status).is_(None), col(WebhookEvent.replay_status) != PENDING))
        .values(replay_status=PENDING)
    )
    session.commit()
    return result.rowcount == 1


def recover_interrupted_replays(engine: Engine) -> int:
    """At startup, move events left "pending" by a restart to dead_letter.

    Background retries live in memory and die with the process. Without
    this, those events would claim to be pending forever and could never
    be replayed again. Returns how many were recovered.
    """
    with Session(engine) as session:
        result = session.execute(
            update(WebhookEvent)
            .where(col(WebhookEvent.replay_status) == PENDING)
            .values(replay_status=DEAD_LETTER)
        )
        session.commit()
        return result.rowcount


def replay_headers(event: WebhookEvent) -> dict[str, str]:
    """The headers to send: the original ones, minus connection-level and redacted ones."""
    original: dict[str, str] = json.loads(event.headers)
    headers = {
        name: value
        for name, value in original.items()
        if name not in SKIPPED_HEADERS and value != "[redacted]"
    }
    headers[REPLAY_HEADER] = str(event.id)
    return headers


def is_retryable(status_code: int) -> bool:
    return status_code >= 500 or status_code == 429


@dataclass
class _Outcome:
    status_code: int | None
    error: str | None
    response_excerpt: str | None
    duration_ms: int

    @property
    def delivered(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300

    @property
    def retryable(self) -> bool:
        # No status code means the request never got an answer: always worth a retry.
        return self.status_code is None or is_retryable(self.status_code)


def _send(client: httpx.Client, url: str, body: bytes, headers: dict[str, str]) -> _Outcome:
    start = time.perf_counter()
    try:
        response = client.post(url, content=body, headers=headers)
    except httpx.TimeoutException as exc:
        error = f"Timed out ({type(exc).__name__})"
        return _Outcome(None, error, None, _ms_since(start))
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        return _Outcome(None, error, None, _ms_since(start))

    duration = _ms_since(start)
    excerpt = response.text[:RESPONSE_EXCERPT_CHARS] or None
    error = None
    if not 200 <= response.status_code < 300:
        error = f"HTTP {response.status_code} {response.reason_phrase}".strip()
    return _Outcome(response.status_code, error, excerpt, duration)


def _ms_since(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


def _set_status(engine: Engine, event_id: int, status: str) -> None:
    with Session(engine) as session:
        event = session.get(WebhookEvent, event_id)
        if event is not None:
            event.replay_status = status
            session.add(event)
            session.commit()


def deliver(engine: Engine, event_id: int, target_url: str, config: ReplayConfig) -> None:
    """Send one event to target_url, retrying with backoff. Runs in the background."""
    with Session(engine) as session:
        event = session.get(WebhookEvent, event_id)
        if event is None:
            return
        body = event.body
        headers = replay_headers(event)

    try:
        with httpx.Client(
            transport=config.transport, timeout=config.timeout, follow_redirects=False
        ) as client:
            for attempt_number in range(1, config.max_attempts + 1):
                outcome = _send(client, target_url, body, headers)
                with Session(engine) as session:
                    session.add(
                        DeliveryAttempt(
                            event_id=event_id,
                            target_url=target_url,
                            attempt_number=attempt_number,
                            status_code=outcome.status_code,
                            error=outcome.error,
                            response_excerpt=outcome.response_excerpt,
                            duration_ms=outcome.duration_ms,
                        )
                    )
                    session.commit()

                if outcome.delivered:
                    _set_status(engine, event_id, DELIVERED)
                    return
                if not outcome.retryable or attempt_number == config.max_attempts:
                    break
                config.sleep(config.delays[attempt_number - 1])
    except Exception:
        # Anything unexpected must not leave the event stuck on "pending".
        _set_status(engine, event_id, DEAD_LETTER)
        raise

    _set_status(engine, event_id, DEAD_LETTER)
