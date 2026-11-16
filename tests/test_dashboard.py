"""Dashboard and p95 tests."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel import Session

from app.checks import CheckConfig, percentile, run_check
from app.db import get_engine
from app.main import app
from app.models import WebhookEvent
from app.routers.checks import get_check_configs
from app.routers.dashboard import _ago

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def use_checks(*checks):
    app.dependency_overrides[get_check_configs] = lambda: list(checks)


def engine():
    return app.dependency_overrides[get_engine]()


def run_with(check, *statuses):
    transport = httpx.MockTransport(lambda req, it=iter(statuses): httpx.Response(next(it)))
    for _ in statuses:
        run_check(check, engine(), lambda text: None, transport)


# --- Page -------------------------------------------------------------------


def test_dashboard_renders_when_empty(client):
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "No webhooks yet" in response.text
    assert "No health checks configured" in response.text


def test_dashboard_lists_events_with_statuses(client, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s")
    client.post("/hooks/github", content=b"{}")  # missing header: rejected
    client.post("/hooks/test", content=b"{}")

    page = client.get("/").text

    assert 'href="/events/1"' in page
    assert 'href="/events/2"' in page
    assert ">rejected<" in page
    assert "Missing X-Hub-Signature-256 header" in page
    assert ">unchecked<" in page


def test_dashboard_shows_replay_status_linked_to_attempts(client):
    client.post("/hooks/test", content=b"{}")
    with Session(engine()) as session:
        event = session.get(WebhookEvent, 1)
        event.replay_status = "dead_letter"
        session.add(event)
        session.commit()

    page = client.get("/").text

    assert 'href="/events/1/attempts"' in page
    assert ">dead_letter<" in page


def test_dashboard_summary_counts(client, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s")
    client.post("/hooks/github", content=b"{}")
    client.post("/hooks/github", content=b"{}")
    client.post("/hooks/test", content=b"{}")
    up = CheckConfig(name="up-one", url="https://a.example")
    down = CheckConfig(name="down-one", url="https://b.example")
    use_checks(up, down)
    run_with(up, 200)
    run_with(down, 500)

    response = client.get("/")
    page = response.text

    assert '<div class="n">3</div><div class="muted">webhooks received' in page
    assert '<div class="n">2</div>\n      <div class="muted"><a href="/events?signature_status=rejected"' in page
    assert '<div class="n">1/2</div>' in page


def test_dashboard_shows_checks(client):
    check = CheckConfig(name="GitHub API", url="https://api.github.com")
    use_checks(check)
    run_with(check, 200, 503)

    page = client.get("/").text

    assert "GitHub API" in page
    assert 'href="/checks/GitHub%20API/results"' in page
    assert ">down<" in page
    assert "Expected HTTP 200, got 503" in page


def test_dashboard_escapes_html(client):
    """Names come from config and data, so they must never be rendered as HTML."""
    use_checks(CheckConfig(name="<script>alert(1)</script>", url="https://a.example"))

    page = client.get("/").text

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_dashboard_not_in_api_docs(client):
    assert "/" not in client.get("/openapi.json").json()["paths"]


# --- p95 --------------------------------------------------------------------


def test_percentile_nearest_rank():
    values = list(range(1, 101))  # 1..100

    assert percentile(values, 95) == 95
    assert percentile(values, 50) == 50
    assert percentile(values, 100) == 100


def test_percentile_catches_the_slow_tail_an_average_hides():
    latencies = [100] * 19 + [9000]  # 1 in 20 is terrible

    assert sum(latencies) / len(latencies) == 545  # average: looks fine
    assert percentile(latencies, 95) == 100
    assert percentile(latencies + [9000], 95) == 9000  # 2 in 21: now p95 sees it


def test_percentile_edge_cases():
    assert percentile([], 95) is None
    assert percentile([42], 95) == 42


def test_checks_api_includes_p95(client):
    check = CheckConfig(name="api", url="https://a.example")
    use_checks(check)

    assert client.get("/checks").json()[0]["p95_latency_ms"] is None

    run_with(check, 200, 200, 200)

    assert client.get("/checks").json()[0]["p95_latency_ms"] >= 0


# --- Relative time ----------------------------------------------------------


@pytest.mark.parametrize(
    "delta, expected",
    [
        (timedelta(seconds=2), "just now"),
        (timedelta(seconds=42), "42s ago"),
        (timedelta(minutes=5, seconds=30), "5m ago"),
        (timedelta(hours=3), "3h ago"),
        (timedelta(days=2, hours=1), "2d ago"),
    ],
)
def test_ago(delta, expected):
    assert _ago(NOW - delta, now=NOW) == expected


def test_ago_handles_naive_sqlite_datetimes_and_none():
    assert _ago((NOW - timedelta(minutes=1)).replace(tzinfo=None), now=NOW) == "1m ago"
    assert _ago(None) == "never"
