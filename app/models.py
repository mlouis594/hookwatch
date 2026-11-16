"""Database tables."""

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WebhookEvent(SQLModel, table=True):
    """One incoming webhook request, stored exactly as it arrived."""

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)  # e.g. "stripe", "github"
    method: str
    path: str
    query: str = ""
    headers: str  # JSON-encoded dict of request headers (sensitive ones redacted)
    # Raw bytes, untouched: signature checks and replays need the exact body.
    body: bytes
    content_type: str | None = None
    size_bytes: int
    # "verified", "rejected", or "unchecked" (no verifier or no secret configured).
    signature_status: str = Field(default="unchecked", index=True)
    # Why it was rejected (or left unchecked), e.g. "Missing Stripe-Signature header".
    signature_detail: str | None = None
    received_at: datetime = Field(default_factory=utcnow, index=True)
    # None until replayed; then "pending", "delivered", or "dead_letter".
    replay_status: str | None = Field(default=None, index=True)


class DeliveryAttempt(SQLModel, table=True):
    """One try at delivering a replayed event to a target URL."""

    id: int | None = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="webhookevent.id", index=True)
    target_url: str
    attempt_number: int  # 1-based; restarts at 1 for each new replay
    status_code: int | None = None  # None when no response came back
    error: str | None = None  # connection error, timeout, or why the status counts as failed
    # First part of the target's response body: often says exactly why it refused.
    response_excerpt: str | None = None
    duration_ms: int
    attempted_at: datetime = Field(default_factory=utcnow)


class CheckResult(SQLModel, table=True):
    """One run of one health check."""

    id: int | None = Field(default=None, primary_key=True)
    check_name: str = Field(index=True)
    ok: bool
    status_code: int | None = None  # None when no response came back
    latency_ms: int  # time to the response, or to the failure
    error: str | None = None
    checked_at: datetime = Field(default_factory=utcnow, index=True)


class CheckState(SQLModel, table=True):
    """The current state of one check: one row per check, updated on every run.

    Kept in the database (not memory) so a restart doesn't forget a check
    was down and alert about it all over again.
    """

    name: str = Field(primary_key=True)
    state: str = "unknown"  # "unknown" (never ran), "up", or "down"
    consecutive_failures: int = 0
    last_changed_at: datetime | None = None  # when state last flipped
    last_checked_at: datetime | None = None
    last_status_code: int | None = None
    last_latency_ms: int | None = None
    last_error: str | None = None
