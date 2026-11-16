"""Replay tests.

The target server is an httpx.MockTransport: a function that receives each
outgoing request and decides the response, so nothing touches the network.
Sleeps are recorded instead of slept, so the backoff runs instantly and
the tests can check the exact delays.
"""

import hashlib
import hmac
import time

import httpx
import pytest
from sqlmodel import Session

from app.db import get_engine
from app.main import app
from app.models import WebhookEvent
from app.replay import ReplayConfig, get_replay_config, recover_interrupted_replays
from app.verification import VERIFIED, verify_stripe

TARGET = "http://target.example/webhooks"
BODY = b'{"id": "evt_1",   "type": "invoice.paid"}'  # odd spacing on purpose


class FakeTarget:
    """Plays a webhook receiver: answers with a scripted list of responses."""

    def __init__(self, *responses):
        # Each item is a status code, or an exception to raise (connection failure).
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(result, Exception):
            raise result
        return httpx.Response(result, text=f"target says {result}")


@pytest.fixture
def replay(client):
    """Returns a function: replay(handler) runs a replay of event 1 against a fake target."""
    sleeps: list[float] = []

    def run(handler, event_id=1, target_url=TARGET):
        config = ReplayConfig(transport=httpx.MockTransport(handler), sleep=sleeps.append)
        app.dependency_overrides[get_replay_config] = lambda: config
        return client.post(f"/events/{event_id}/replay", json={"target_url": target_url})

    run.sleeps = sleeps
    return run


def store_event(client, body=BODY, **headers):
    headers.setdefault("Content-Type", "application/json")
    return client.post("/hooks/test", content=body, headers=headers).json()["id"]


def attempts(client, event_id=1):
    return client.get(f"/events/{event_id}/attempts").json()


def replay_status(client, event_id=1):
    return client.get(f"/events/{event_id}").json()["replay_status"]


# --- Happy path -------------------------------------------------------------


def test_replay_delivers_on_first_try(client, replay):
    store_event(client)
    target = FakeTarget(200)

    response = replay(target)

    assert response.status_code == 202
    assert response.json() == {
        "event_id": 1,
        "target_url": TARGET,
        "replay_status": "pending",
        "attempts_url": "/events/1/attempts",
    }
    # TestClient runs background tasks before returning, so it's finished by now.
    assert replay_status(client) == "delivered"
    [attempt] = attempts(client)
    assert attempt["attempt_number"] == 1
    assert attempt["status_code"] == 200
    assert attempt["error"] is None
    assert attempt["response_excerpt"] == "target says 200"
    assert attempt["target_url"] == TARGET
    assert replay.sleeps == []


def test_replay_sends_exact_body_and_original_headers(client, replay):
    store_event(
        client,
        **{"Stripe-Signature": "t=1,v1=abc", "Authorization": "Bearer secret", "X-Custom": "yes"},
    )
    target = FakeTarget(200)

    replay(target)

    [sent] = target.requests
    assert sent.method == "POST"
    assert sent.content == BODY  # byte for byte, odd spacing kept
    assert sent.headers["content-type"] == "application/json"
    assert sent.headers["x-hookwatch-replay"] == "1"
    assert sent.headers["stripe-signature"] == "t=1,v1=abc"
    assert sent.headers["x-custom"] == "yes"
    # Redacted at storage time, so there's nothing real to forward.
    assert "authorization" not in sent.headers
    # Describes the original connection; httpx sets the right one itself.
    assert sent.headers["host"] == "target.example"
    assert sent.headers["content-length"] == str(len(BODY))


def test_non_json_body_is_replayed_unchanged(client, replay):
    store_event(client, body=b"a=1&b=2", **{"Content-Type": "application/x-www-form-urlencoded"})
    target = FakeTarget(204)

    replay(target)

    assert target.requests[0].content == b"a=1&b=2"
    assert target.requests[0].headers["content-type"] == "application/x-www-form-urlencoded"
    assert replay_status(client) == "delivered"


# --- Retries and backoff ----------------------------------------------------


def test_5xx_is_retried_until_success(client, replay):
    store_event(client)
    target = FakeTarget(503, 500, 200)

    replay(target)

    assert [a["status_code"] for a in attempts(client)] == [503, 500, 200]
    assert [a["attempt_number"] for a in attempts(client)] == [1, 2, 3]
    assert replay.sleeps == [1, 2]
    assert replay_status(client) == "delivered"


def test_gives_up_after_five_attempts_with_exponential_backoff(client, replay):
    store_event(client)
    target = FakeTarget(500)

    replay(target)

    history = attempts(client)
    assert len(history) == 5
    assert all(a["error"] == "HTTP 500 Internal Server Error" for a in history)
    assert replay.sleeps == [1, 2, 4, 8]
    assert replay_status(client) == "dead_letter"


def test_connection_errors_are_retried(client, replay):
    store_event(client)
    target = FakeTarget(httpx.ConnectError("Connection refused"), 200)

    replay(target)

    first, second = attempts(client)
    assert first["status_code"] is None
    assert first["error"] == "ConnectError: Connection refused"
    assert second["status_code"] == 200
    assert replay_status(client) == "delivered"


def test_timeouts_are_retried(client, replay):
    store_event(client)
    target = FakeTarget(httpx.ReadTimeout("slow"))

    replay(target)

    history = attempts(client)
    assert len(history) == 5
    assert history[0]["error"] == "Timed out (ReadTimeout)"
    assert replay_status(client) == "dead_letter"


def test_429_is_retried(client, replay):
    store_event(client)
    target = FakeTarget(429, 200)

    replay(target)

    assert [a["status_code"] for a in attempts(client)] == [429, 200]


@pytest.mark.parametrize("status", [400, 401, 404, 410])
def test_other_4xx_is_not_retried(client, replay, status):
    store_event(client)
    target = FakeTarget(status)

    replay(target)

    [attempt] = attempts(client)
    assert attempt["status_code"] == status
    assert replay.sleeps == []
    assert replay_status(client) == "dead_letter"


def test_redirect_is_not_followed(client, replay):
    store_event(client)
    target = FakeTarget(301)

    replay(target)

    assert len(target.requests) == 1
    assert replay_status(client) == "dead_letter"


# --- Dead-letter list and history -------------------------------------------


def test_dead_letter_filter(client, replay):
    store_event(client)
    store_event(client)
    store_event(client)
    replay(FakeTarget(500), event_id=1)
    replay(FakeTarget(200), event_id=2)

    dead = client.get("/events", params={"replay_status": "dead_letter"}).json()

    assert [e["id"] for e in dead] == [1]
    assert client.get("/events/3").json()["replay_status"] is None


def test_replaying_again_appends_a_new_run(client, replay):
    store_event(client)
    replay(FakeTarget(404))
    replay(FakeTarget(200))

    history = attempts(client)

    assert [(a["attempt_number"], a["status_code"]) for a in history] == [(1, 404), (1, 200)]
    assert replay_status(client) == "delivered"


def test_second_replay_while_pending_is_refused(client, replay):
    store_event(client)
    # Simulate a replay still in its retry loop.
    engine = app.dependency_overrides[get_engine]()
    with Session(engine) as session:
        event = session.get(WebhookEvent, 1)
        event.replay_status = "pending"
        session.add(event)
        session.commit()
    target = FakeTarget(200)

    response = replay(target)

    assert response.status_code == 409
    assert target.requests == []


def test_restart_moves_pending_replays_to_dead_letter(client):
    store_event(client)
    store_event(client)
    engine = app.dependency_overrides[get_engine]()
    with Session(engine) as session:
        event = session.get(WebhookEvent, 1)
        event.replay_status = "pending"
        session.add(event)
        session.commit()

    recovered = recover_interrupted_replays(engine)

    assert recovered == 1
    assert replay_status(client, 1) == "dead_letter"
    assert replay_status(client, 2) is None


# --- Validation -------------------------------------------------------------


def test_replay_missing_event_returns_404(client, replay):
    response = replay(FakeTarget(200), event_id=99)

    assert response.status_code == 404


def test_attempts_for_missing_event_returns_404(client):
    assert client.get("/events/99/attempts").status_code == 404


def test_attempts_empty_before_any_replay(client):
    store_event(client)

    assert attempts(client) == []


@pytest.mark.parametrize("target_url", ["not a url", "ftp://example.com/x", ""])
def test_replay_rejects_invalid_target_url(client, replay, target_url):
    store_event(client)

    response = replay(FakeTarget(200), target_url=target_url)

    assert response.status_code == 422


# --- The Stripe talking point -----------------------------------------------

SECRET = "whsec_target_secret"


def signed_stripe_header(body: bytes, timestamp: int) -> str:
    sig = hmac.new(SECRET.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={sig}"


def stripe_checking_target(request: httpx.Request) -> httpx.Response:
    """A receiver that verifies Stripe signatures, like a real customer's endpoint."""
    result = verify_stripe(request.content, request.headers, SECRET)
    if result.status == VERIFIED:
        return httpx.Response(200)
    return httpx.Response(400, text=result.detail)


def test_fresh_stripe_event_replays_with_a_valid_signature(client, replay):
    store_event(client, **{"Stripe-Signature": signed_stripe_header(BODY, int(time.time()))})

    replay(stripe_checking_target)

    assert replay_status(client) == "delivered"


def test_old_stripe_event_fails_the_targets_timestamp_check(client, replay):
    """Why Stripe has its own resend: our replay carries the original timestamp.

    Ten minutes after Stripe signed it, the signature is still correct but
    outside the target's 5-minute window, and we can't re-sign it without
    the target's secret.
    """
    signed_at = int(time.time()) - 600
    store_event(client, **{"Stripe-Signature": signed_stripe_header(BODY, signed_at)})

    replay(stripe_checking_target)

    [attempt] = attempts(client)  # a 400 isn't retried
    assert attempt["status_code"] == 400
    assert attempt["response_excerpt"].startswith("Stale timestamp")
    assert replay_status(client) == "dead_letter"
