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
