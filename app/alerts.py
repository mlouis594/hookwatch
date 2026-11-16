"""Sending alerts: to Slack when SLACK_WEBHOOK_URL is set, otherwise to the log.

A Slack incoming webhook is just a URL you POST {"text": "..."} to.
An alert that fails to send is logged, never raised: a Slack outage must
not stop the health checks themselves.
"""

import logging
import os
from collections.abc import Callable

import httpx

logger = logging.getLogger("hookwatch.alerts")

Alerter = Callable[[str], None]

SLACK_TIMEOUT_SECONDS = 10.0


def log_alert(text: str) -> None:
    logger.warning("ALERT (SLACK_WEBHOOK_URL not set): %s", text)


def slack_alerter(webhook_url: str, transport: httpx.BaseTransport | None = None) -> Alerter:
    """Return a function that posts a message to a Slack incoming webhook."""

    def send(text: str) -> None:
        try:
            with httpx.Client(transport=transport, timeout=SLACK_TIMEOUT_SECONDS) as client:
                response = client.post(webhook_url, json={"text": text})
            if response.status_code != 200:
                logger.error("Slack refused the alert: HTTP %s %s", response.status_code, response.text[:200])
        except httpx.HTTPError as exc:
            logger.error("Couldn't send Slack alert (%s): %s", type(exc).__name__, text)

    return send


def default_alerter() -> Alerter:
    url = os.getenv("SLACK_WEBHOOK_URL")
    return slack_alerter(url) if url else log_alert
