"""Signature verification tests.

Every payload is signed inside the test, the same way Stripe and GitHub
sign them, so the tests also document the signing schemes.
"""

import hashlib
import hmac
import time

import pytest

from app.verification import (
    REJECTED,
    STRIPE_TOLERANCE_SECONDS,
    UNCHECKED,
    VERIFIED,
    verify,
    verify_github,
    verify_stripe,
)

STRIPE_SECRET = "whsec_test_secret"
GITHUB_SECRET = "github_test_secret"
BODY = b'{"id": "evt_1", "type": "payment_intent.succeeded"}'


def hmac_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def stripe_header(body: bytes, secret: str = STRIPE_SECRET, timestamp: int | None = None) -> str:
    t = int(time.time()) if timestamp is None else timestamp
    return f"t={t},v1={hmac_hex(secret, f'{t}.'.encode() + body)}"


def github_header(body: bytes, secret: str = GITHUB_SECRET) -> str:
    return f"sha256={hmac_hex(secret, body)}"


# --- Stripe -----------------------------------------------------------------


def test_stripe_valid_signature():
    result = verify_stripe(BODY, {"stripe-signature": stripe_header(BODY)}, STRIPE_SECRET)

    assert result.status == VERIFIED
    assert result.detail is None


def test_stripe_wrong_secret():
    header = stripe_header(BODY, secret="whsec_someone_else")

    result = verify_stripe(BODY, {"stripe-signature": header}, STRIPE_SECRET)

    assert result.status == REJECTED
    assert "mismatch" in result.detail


def test_stripe_body_changed_after_signing():
    header = stripe_header(BODY)
    tampered = BODY.replace(b"evt_1", b"evt_2")

    result = verify_stripe(tampered, {"stripe-signature": header}, STRIPE_SECRET)

    assert result.status == REJECTED
    assert "mismatch" in result.detail


def test_stripe_reformatted_json_fails():
    """Same data, different bytes: why we never re-serialize the body."""
    header = stripe_header(BODY)
    reformatted = b'{"id":"evt_1","type":"payment_intent.succeeded"}'

    result = verify_stripe(reformatted, {"stripe-signature": header}, STRIPE_SECRET)

    assert result.status == REJECTED


def test_stripe_stale_timestamp():
    now = 1_700_000_000
    header = stripe_header(BODY, timestamp=now - STRIPE_TOLERANCE_SECONDS - 1)

    result = verify_stripe(BODY, {"stripe-signature": header}, STRIPE_SECRET, now=now)

    assert result.status == REJECTED
    assert "Stale timestamp" in result.detail


def test_stripe_timestamp_at_edge_of_tolerance_passes():
    now = 1_700_000_000
    header = stripe_header(BODY, timestamp=now - STRIPE_TOLERANCE_SECONDS)

    result = verify_stripe(BODY, {"stripe-signature": header}, STRIPE_SECRET, now=now)

    assert result.status == VERIFIED


def test_stripe_timestamp_is_part_of_the_signature():
    """Swapping in a fresh timestamp to dodge the stale check breaks the signature."""
    now = int(time.time())
    old_header = stripe_header(BODY, timestamp=now - 3600)
    old_sig = old_header.split("v1=")[1]

    result = verify_stripe(BODY, {"stripe-signature": f"t={now},v1={old_sig}"}, STRIPE_SECRET)

    assert result.status == REJECTED
    assert "mismatch" in result.detail


def test_stripe_missing_header():
    result = verify_stripe(BODY, {}, STRIPE_SECRET)

    assert result.status == REJECTED
    assert result.detail == "Missing Stripe-Signature header"


def test_stripe_multiple_v1_any_match_passes():
    """During secret rotation Stripe sends one v1 per active secret."""
    t = int(time.time())
    old = hmac_hex("whsec_old_secret", f"{t}.".encode() + BODY)
    new = hmac_hex(STRIPE_SECRET, f"{t}.".encode() + BODY)
    header = f"t={t},v1={old},v1={new}"

    result = verify_stripe(BODY, {"stripe-signature": header}, STRIPE_SECRET)

    assert result.status == VERIFIED


def test_stripe_multiple_v1_none_match_fails():
    t = int(time.time())
    header = f"t={t},v1={'0' * 64},v1={'f' * 64}"

    result = verify_stripe(BODY, {"stripe-signature": header}, STRIPE_SECRET)

    assert result.status == REJECTED


@pytest.mark.parametrize(
    "header",
    ["garbage", "v1=abc", "t=notanumber,v1=abc", f"t={int(time.time())}", f"t={int(time.time())},v0=abc"],
)
def test_stripe_malformed_header(header):
    result = verify_stripe(BODY, {"stripe-signature": header}, STRIPE_SECRET)

    assert result.status == REJECTED
    assert "Malformed" in result.detail


# --- GitHub -----------------------------------------------------------------


def test_github_valid_signature():
    result = verify_github(BODY, {"x-hub-signature-256": github_header(BODY)}, GITHUB_SECRET)

    assert result.status == VERIFIED


def test_github_wrong_secret():
    header = github_header(BODY, secret="not_the_secret")

    result = verify_github(BODY, {"x-hub-signature-256": header}, GITHUB_SECRET)

    assert result.status == REJECTED
    assert "mismatch" in result.detail


def test_github_body_changed_after_signing():
    header = github_header(BODY)

    result = verify_github(BODY + b" ", {"x-hub-signature-256": header}, GITHUB_SECRET)

    assert result.status == REJECTED


def test_github_missing_header():
    result = verify_github(BODY, {}, GITHUB_SECRET)

    assert result.status == REJECTED
    assert result.detail == "Missing X-Hub-Signature-256 header"


@pytest.mark.parametrize("header", ["abc123", "sha1=abc123", "sha256="])
def test_github_malformed_header(header):
    result = verify_github(BODY, {"x-hub-signature-256": header}, GITHUB_SECRET)

    assert result.status == REJECTED
    assert "Malformed" in result.detail


# --- Registry ---------------------------------------------------------------


def test_no_secret_configured_leaves_event_unchecked():
    result = verify("stripe", BODY, {"stripe-signature": stripe_header(BODY)})

    assert result.status == UNCHECKED
    assert result.detail == "STRIPE_WEBHOOK_SECRET is not set"


def test_unknown_source_is_unchecked(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", STRIPE_SECRET)

    result = verify("shopify", BODY, {})

    assert result.status == UNCHECKED


def test_registry_uses_secret_from_env(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", GITHUB_SECRET)

    result = verify("github", BODY, {"x-hub-signature-256": github_header(BODY)})

    assert result.status == VERIFIED


# --- Through the endpoint ---------------------------------------------------


def test_endpoint_verified_stripe_event(client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", STRIPE_SECRET)

    response = client.post(
        "/hooks/stripe",
        content=BODY,
        headers={"Content-Type": "application/json", "Stripe-Signature": stripe_header(BODY)},
    )

    assert response.status_code == 200
    assert response.json()["signature_status"] == "verified"
    assert client.get("/events/1").json()["signature_status"] == "verified"


def test_endpoint_rejected_event_is_stored_and_returns_400(client, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", GITHUB_SECRET)

    response = client.post(
        "/hooks/github",
        content=BODY,
        headers={"X-Hub-Signature-256": github_header(BODY, secret="wrong")},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["received"] is True
    assert body["signature_status"] == "rejected"
    assert "mismatch" in body["signature_detail"]

    # Stored anyway, with the reason, so support can see what went wrong.
    detail = client.get(f"/events/{body['id']}").json()
    assert detail["signature_status"] == "rejected"
    assert "mismatch" in detail["signature_detail"]
    assert detail["body"] == BODY.decode()


def test_endpoint_missing_header_is_rejected(client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", STRIPE_SECRET)

    response = client.post("/hooks/stripe", content=BODY)

    assert response.status_code == 400
    assert response.json()["signature_detail"] == "Missing Stripe-Signature header"


def test_endpoint_no_secret_stays_unchecked(client):
    response = client.post("/hooks/stripe", content=BODY)

    assert response.status_code == 200
    assert response.json()["signature_status"] == "unchecked"


def test_list_filters_by_signature_status(client, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", GITHUB_SECRET)
    client.post("/hooks/github", content=BODY, headers={"X-Hub-Signature-256": github_header(BODY)})
    client.post("/hooks/github", content=BODY)  # no header: rejected
    client.post("/hooks/test", content=BODY)  # no verifier: unchecked

    rejected = client.get("/events", params={"signature_status": "rejected"}).json()

    assert [e["id"] for e in rejected] == [2]
    assert rejected[0]["signature_detail"] == "Missing X-Hub-Signature-256 header"
