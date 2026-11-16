"""Health check tests.

The checked APIs and Slack are httpx.MockTransports, so nothing touches the
network. Alerts are collected in a list so tests can assert exactly which
messages would have been sent.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.alerts import default_alerter, log_alert, slack_alerter
from app.checks import (
    DOWN,
    UNKNOWN,
    UP,
    CheckConfig,
    ChecksConfigError,
    CheckScheduler,
    load_checks,
    next_state,
    probe,
    run_check,
)
from app.db import get_engine
from app.main import app
from app.models import CheckResult, CheckState
from app.routers.checks import get_check_configs

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def make_check(**overrides) -> CheckConfig:
    fields = {"name": "api", "url": "https://api.example.com/health"}
    return CheckConfig(**(fields | overrides))


def responder(*outcomes):
    """A fake API: returns each outcome in turn (status code or exception), repeating the last."""
    outcomes = list(outcomes)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome)

    handler.calls = calls
    return httpx.MockTransport(handler)


@pytest.fixture
def engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def alerts():
    return []


def run(check, engine, alerts, *outcomes, now=T0):
    """Run a check once per outcome, a minute apart. Returns the last state."""
    transport = responder(*outcomes)
    state = None
    for i in range(len(outcomes)):
        state = run_check(check, engine, alerts.append, transport, now=now + timedelta(minutes=i))
    return state


# --- Configuration ----------------------------------------------------------


def test_load_checks_reads_yaml_and_applies_defaults(tmp_path):
    path = tmp_path / "checks.yaml"
    path.write_text("checks:\n  - name: a\n    url: https://a.example.com\n    method: head\n")

    [check] = load_checks(path)

    assert check.name == "a"
    assert check.method == "HEAD"
    assert check.expected_status == 200
    assert check.interval_seconds == 60
    assert check.timeout_seconds == 10
    assert check.failures_before_down == 1


def test_missing_checks_file_means_no_checks(tmp_path):
    assert load_checks(tmp_path / "nope.yaml") == []


def test_empty_checks_file_means_no_checks(tmp_path):
    path = tmp_path / "checks.yaml"
    path.write_text("")

    assert load_checks(path) == []


@pytest.mark.parametrize(
    "content, message",
    [
        ("checks:\n  - name: a\n", "url"),
        ("checks:\n  - name: a\n    url: not-a-url\n", "url"),
        ("checks:\n  - name: a\n    url: https://a.example\n    interval_seconds: 1\n", "interval_seconds"),
        (
            "checks:\n  - {name: a, url: 'https://a.example'}\n  - {name: a, url: 'https://b.example'}\n",
            "duplicate check names: a",
        ),
        ("checks: [unclosed", "Invalid"),
    ],
)
def test_invalid_checks_file_fails_loudly(tmp_path, content, message):
    path = tmp_path / "checks.yaml"
    path.write_text(content)

    with pytest.raises(ChecksConfigError, match=message):
        load_checks(path)


def test_example_config_is_valid():
    checks = load_checks("checks.example.yaml")

    assert len(checks) >= 1


# --- Probing ----------------------------------------------------------------


def test_probe_passes_on_expected_status():
    result = probe(make_check(), responder(200))

    assert result.ok
    assert result.status_code == 200
    assert result.error is None
    assert result.latency_ms >= 0


def test_probe_uses_configured_method_and_expected_status():
    transport = responder(401)

    result = probe(make_check(method="HEAD", expected_status=401), transport)

    assert result.ok
    assert transport.handler.calls[0].method == "HEAD"


def test_probe_fails_on_unexpected_status():
    result = probe(make_check(), responder(503))

    assert not result.ok
    assert result.status_code == 503
    assert result.error == "Expected HTTP 200, got 503"


def test_probe_fails_on_connection_error():
    result = probe(make_check(), responder(httpx.ConnectError("Connection refused")))

    assert not result.ok
    assert result.status_code is None
    assert result.error == "ConnectError: Connection refused"


def test_probe_fails_on_timeout():
    result = probe(make_check(timeout_seconds=2.5), responder(httpx.ReadTimeout("slow")))

    assert not result.ok
    assert result.error == "Timed out after 2.5s"


# --- State machine ----------------------------------------------------------


@pytest.mark.parametrize(
    "current, failures, ok, threshold, expected",
    [
        (UNKNOWN, 0, True, 1, UP),
        (UNKNOWN, 1, False, 1, DOWN),
        (UP, 1, False, 1, DOWN),
        (UP, 1, False, 2, UP),  # one blip isn't an outage yet
        (UP, 2, False, 2, DOWN),
        (UNKNOWN, 1, False, 3, UNKNOWN),
        (DOWN, 5, False, 1, DOWN),
        (DOWN, 0, True, 3, UP),  # one success is a recovery
    ],
)
def test_next_state(current, failures, ok, threshold, expected):
    assert next_state(current, failures, ok, threshold) == expected


# --- Alerting only on change ------------------------------------------------


def test_first_success_is_up_and_silent(engine, alerts):
    state = run(make_check(), engine, alerts, 200)

    assert state.state == UP
    assert state.last_changed_at is not None
    assert alerts == []


def test_going_down_alerts_once_however_long_it_lasts(engine, alerts):
    state = run(make_check(), engine, alerts, 200, 500, 500, 500, 500)

    assert state.state == DOWN
    assert state.consecutive_failures == 4
    assert alerts == [
        ":red_circle: *api* is DOWN: Expected HTTP 200, got 500 (GET https://api.example.com/health)"
    ]


def test_recovery_alerts_once_with_outage_length(engine, alerts):
    run(make_check(), engine, alerts, 200, 500, 500, 500, 200, 200)

    assert len(alerts) == 2
    assert alerts[1].startswith(":large_green_circle: *api* recovered after 3m 0s (HTTP 200 in ")


def test_down_on_first_ever_run_alerts(engine, alerts):
    """If it's broken when we start watching, someone should know."""
    run(make_check(), engine, alerts, httpx.ConnectError("refused"))

    assert len(alerts) == 1
    assert "DOWN" in alerts[0]


def test_failures_before_down_ignores_a_single_blip(engine, alerts):
    check = make_check(failures_before_down=2)

    state = run(check, engine, alerts, 200, 500, 200)

    assert state.state == UP
    assert alerts == []


def test_failures_before_down_alerts_on_the_nth_failure(engine, alerts):
    check = make_check(failures_before_down=3)

    run(check, engine, alerts, 200, 500, 500)
    assert alerts == []

    run(check, engine, alerts, 500)
    assert len(alerts) == 1


def test_flapping_alerts_on_every_change(engine, alerts):
    run(make_check(), engine, alerts, 200, 500, 200, 500)

    assert [a.split()[0] for a in alerts] == [":red_circle:", ":large_green_circle:", ":red_circle:"]


def test_state_survives_a_restart(engine, alerts):
    """State lives in the database, so a fresh scheduler doesn't re-alert an ongoing outage."""
    check = make_check()
    run(check, engine, alerts, 500)

    run(check, engine, alerts, 500, now=T0 + timedelta(hours=1))  # "after a restart"

    assert len(alerts) == 1


def test_every_run_is_stored(engine, alerts):
    run(make_check(), engine, alerts, 200, 503, httpx.ConnectError("refused"))

    with Session(engine) as session:
        results = session.exec(select(CheckResult).order_by(CheckResult.id)).all()

    assert [(r.ok, r.status_code) for r in results] == [(True, 200), (False, 503), (False, None)]
    assert results[2].error == "ConnectError: refused"
    assert all(r.check_name == "api" for r in results)


def test_last_changed_at_only_moves_on_a_change(engine, alerts):
    check = make_check()
    run(check, engine, alerts, 200, 500, 500, 500)

    with Session(engine) as session:
        state = session.get(CheckState, "api")

    # Compare wall-clock values; whether SQLite keeps the tzinfo isn't the point here.
    assert state.last_changed_at.replace(tzinfo=None) == datetime(2026, 11, 16, 12, 1)
    assert state.last_checked_at.replace(tzinfo=None) == datetime(2026, 11, 16, 12, 3)


# --- Slack ------------------------------------------------------------------


def test_slack_alerter_posts_text():
    sent = []

    def slack(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, text="ok")

    slack_alerter("https://hooks.slack.test/T/B/x", httpx.MockTransport(slack))("hello")

    [request] = sent
    assert str(request.url) == "https://hooks.slack.test/T/B/x"
    assert json.loads(request.content) == {"text": "hello"}


def test_slack_failure_is_logged_not_raised(caplog):
    alert = slack_alerter("https://hooks.slack.test/x", responder(httpx.ConnectError("down")))

    with caplog.at_level(logging.ERROR, logger="hookwatch.alerts"):
        alert("hello")  # must not raise

    assert "Couldn't send Slack alert" in caplog.text


def test_slack_error_response_is_logged(caplog):
    alert = slack_alerter("https://hooks.slack.test/x", responder(404))

    with caplog.at_level(logging.ERROR, logger="hookwatch.alerts"):
        alert("hello")

    assert "HTTP 404" in caplog.text


def test_without_slack_url_alerts_go_to_the_log(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)

    assert default_alerter() is log_alert


def test_with_slack_url_alerts_go_to_slack(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/x")

    assert default_alerter() is not log_alert


# --- Scheduler --------------------------------------------------------------


def test_scheduler_runs_checks_until_stopped(engine, alerts):
    async def main():
        scheduler = CheckScheduler([make_check(interval_seconds=5)], engine, alerts.append, responder(200))
        scheduler.start()
        for _ in range(200):  # wait up to ~2 s for the first run
            await asyncio.sleep(0.01)
            with Session(engine) as session:
                if session.get(CheckState, "api"):
                    break
        await scheduler.stop()

    asyncio.run(main())

    with Session(engine) as session:
        assert session.get(CheckState, "api").state == UP


def test_scheduler_survives_a_crashing_run(engine, alerts, monkeypatch, caplog):
    import app.checks as checks_module

    calls = []

    def boom(*args):
        calls.append(1)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(checks_module, "run_check", boom)

    async def main():
        scheduler = CheckScheduler([make_check()], engine, alerts.append)
        scheduler.start()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if calls:
                break
        await asyncio.sleep(0.01)
        await scheduler.stop()

    with caplog.at_level(logging.ERROR, logger="hookwatch.checks"):
        asyncio.run(main())

    assert calls
    assert "crashed; will try again" in caplog.text


# --- API --------------------------------------------------------------------


@pytest.fixture
def configured(client):
    checks = [make_check(name="api"), make_check(name="db", url="https://db.example.com")]
    app.dependency_overrides[get_check_configs] = lambda: checks
    return checks


def test_list_checks_unknown_before_first_run(client, configured):
    body = client.get("/checks").json()

    assert [(c["name"], c["state"]) for c in body] == [("api", "unknown"), ("db", "unknown")]
    assert body[0]["last_latency_ms"] is None
    assert body[0]["url"] == "https://api.example.com/health"


def test_list_checks_shows_state_latency_and_last_change(client, configured, alerts):
    engine = app.dependency_overrides[get_engine]()
    run(configured[0], engine, alerts, 200, 500)

    api = client.get("/checks").json()[0]

    assert api["state"] == "down"
    assert api["last_status_code"] == 500
    assert api["last_error"] == "Expected HTTP 200, got 500"
    assert api["last_latency_ms"] >= 0
    assert api["last_changed_at"].startswith("2026-11-16T12:01")


def test_list_checks_empty_when_none_configured(client):
    assert client.get("/checks").json() == []


def test_check_results_newest_first(client, configured, alerts):
    engine = app.dependency_overrides[get_engine]()
    run(configured[0], engine, alerts, 200, 500, 200)

    results = client.get("/checks/api/results", params={"limit": 2}).json()

    assert [r["status_code"] for r in results] == [200, 500]


def test_results_for_unknown_check_returns_404(client, configured):
    assert client.get("/checks/nope/results").status_code == 404
