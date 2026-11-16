"""Webhook signature verification, done by hand with hmac and hashlib.

Both Stripe and GitHub sign webhooks the same basic way: they compute
HMAC-SHA256 over the raw request body using a secret shared with you,
and send the result in a header. We recompute it with our copy of the
secret and compare. A match proves two things: the sender knows the
secret, and the body wasn't changed on the way.

Rules that matter:
- Sign the *raw bytes*. Parsing the JSON and re-serializing it changes
  whitespace and key order, and the signature no longer matches.
- Compare with hmac.compare_digest, not ==. A plain == stops at the first
  differing character, so response timing leaks how much of a guessed
  signature was right. compare_digest takes the same time either way.

Each verifier returns a VerificationResult. The registry at the bottom
maps a webhook source (the {source} in /hooks/{source}) to its verifier
and the environment variable holding its secret.
"""

import hashlib
import hmac
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

VERIFIED = "verified"
REJECTED = "rejected"
UNCHECKED = "unchecked"

# Stripe's own libraries default to a 5-minute tolerance.
STRIPE_TOLERANCE_SECONDS = 300


@dataclass(frozen=True)
class VerificationResult:
    status: str  # VERIFIED, REJECTED, or UNCHECKED
    detail: str | None = None  # human-readable reason, for support


def _hmac_sha256_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_stripe(
    body: bytes, headers: Mapping[str, str], secret: str, now: float | None = None
) -> VerificationResult:
    """Check a Stripe-Signature header: ``t=<unix ts>,v1=<hex>[,v1=<hex>...]``.

    Stripe signs ``"<t>." + body``, so the timestamp is covered by the
    signature and can't be swapped out. Rejecting old timestamps stops
    replay attacks: someone who captured a valid request can't resend it
    later. More than one v1 appears while you roll your secret, since
    Stripe then signs with both the old and new secret; any match passes.

    The secret is used as-is, ``whsec_`` prefix included.
    """
    header = headers.get("stripe-signature")
    if not header:
        return VerificationResult(REJECTED, "Missing Stripe-Signature header")

    timestamp: str | None = None
    signatures: list[str] = []
    for part in header.split(","):
        key, sep, value = part.strip().partition("=")
        if not sep:
            continue
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)
        # Other schemes (e.g. v0, used in test mode) are ignored, like Stripe's SDKs do.

    if timestamp is None or not timestamp.isdigit():
        return VerificationResult(REJECTED, "Malformed Stripe-Signature header: no valid t= timestamp")
    if not signatures:
        return VerificationResult(REJECTED, "Malformed Stripe-Signature header: no v1= signature")

    expected = _hmac_sha256_hex(secret, f"{timestamp}.".encode() + body)
    if not any(hmac.compare_digest(expected, sig) for sig in signatures):
        return VerificationResult(REJECTED, "Signature mismatch: wrong secret or body changed after signing")

    # Checked after the signature, so we only trust a timestamp we know Stripe sent.
    age = (time.time() if now is None else now) - int(timestamp)
    if age > STRIPE_TOLERANCE_SECONDS:
        return VerificationResult(
            REJECTED,
            f"Stale timestamp: signed {int(age)}s ago, tolerance is {STRIPE_TOLERANCE_SECONDS}s",
        )
    return VerificationResult(VERIFIED)


def verify_github(
    body: bytes, headers: Mapping[str, str], secret: str, now: float | None = None
) -> VerificationResult:
    """Check an X-Hub-Signature-256 header: ``sha256=<hex>`` over the raw body.

    GitHub's signature has no timestamp, so there's no replay window to
    enforce. (``now`` is accepted only so every verifier has the same shape.)
    """
    header = headers.get("x-hub-signature-256")
    if not header:
        return VerificationResult(REJECTED, "Missing X-Hub-Signature-256 header")

    scheme, sep, signature = header.partition("=")
    if scheme != "sha256" or not sep or not signature:
        return VerificationResult(REJECTED, "Malformed X-Hub-Signature-256 header: expected sha256=<hex>")

    expected = _hmac_sha256_hex(secret, body)
    if not hmac.compare_digest(expected, signature):
        return VerificationResult(REJECTED, "Signature mismatch: wrong secret or body changed after signing")
    return VerificationResult(VERIFIED)


Verifier = Callable[..., VerificationResult]


@dataclass(frozen=True)
class Provider:
    verifier: Verifier
    secret_env: str  # environment variable that holds this provider's secret


# Keyed by the {source} in /hooks/{source}. Add a provider by adding a line.
PROVIDERS: dict[str, Provider] = {
    "stripe": Provider(verify_stripe, "STRIPE_WEBHOOK_SECRET"),
    "github": Provider(verify_github, "GITHUB_WEBHOOK_SECRET"),
}


def verify(source: str, body: bytes, headers: Mapping[str, str]) -> VerificationResult:
    """Verify a webhook for ``source``.

    Returns UNCHECKED when the source has no verifier or no secret is
    configured, so HookWatch still works as a plain inspector. The secret
    is read on every call, so changing the env var needs no code reload.
    """
    provider = PROVIDERS.get(source)
    if provider is None:
        return VerificationResult(UNCHECKED, f"No verifier for source '{source}'")
    secret = os.getenv(provider.secret_env)
    if not secret:
        return VerificationResult(UNCHECKED, f"{provider.secret_env} is not set")
    return provider.verifier(body, headers, secret)
