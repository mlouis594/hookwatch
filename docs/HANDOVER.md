# Handover: portfolio projects

*Last updated November 16, 2026. Original build plan: https://claude.ai/artifact/5mqkRXEtEdEdxqa5mkDdxF*

## Goal

Marlon Louis (GitHub `mlouis594`) is building two portfolio projects for an **Integrations and API Support Advisor** role. His background is 3+ years of QoE test automation and monitoring at Witbe (Python, Selenium), then Technical Customer Success Manager. The projects need to show the problems integration support teams handle: webhooks, OAuth, rate limits, retries, and debugging failed requests.

1. **HookWatch** (this repo): webhook inspector and API health monitor. **In progress.**
2. **Stripe to HubSpot Sync** (separate repo, not created yet): OAuth integration that syncs Stripe customers and payments into HubSpot.

Each project ends as a public repo with a README he can walk an interviewer through.

## Status

| Milestone | State |
| --- | --- |
| 1. Skeleton: FastAPI, `/health`, Dockerfile, pytest, CI | Done |
| 2. Receiver: store webhooks in SQLite, list and view events | Done |
| 3. Signature verification (Stripe, GitHub) | Done |
| 4. Replay with retries, backoff, dead-letter | Done |
| 5. Scheduled API health checks + Slack alerts | Done |
| 6. Dashboard page, README polish | **Next** |

All 109 tests pass on CI. One harmless warning remains: Starlette's test client deprecating `httpx`. It's not from our code.

### What exists today

- `POST /hooks/{source}`: stores method, path, query, headers (Authorization, Proxy-Authorization, and Cookie redacted), raw body bytes, content type, and size. Returns `{"id": n, "received": true}`. Sources must match `^[a-z0-9][a-z0-9_-]{0,49}$`. Bodies over 1 MB get a 413.
- `GET /events` (newest first; `source`, `limit` 1–200, `offset`) and `GET /events/{id}` (headers, body text, parsed `json_body` when the content type is JSON).
- Signature verification (`app/verification.py`): Stripe and GitHub HMAC-SHA256 checks, a `PROVIDERS` registry keyed by source, secrets read from env on each request. Events get `signature_status` (`verified`/`rejected`/`unchecked`) and `signature_detail`; rejected ones are stored and answered with 400. `GET /events?signature_status=rejected` lists failures.
- Replay (`app/replay.py`): `POST /events/{id}/replay` re-sends raw body + original headers + `X-HookWatch-Replay`, in a background task. Retries connection errors, timeouts, 5xx, and 429 with 1/2/4/8 s backoff (5 attempts), then `dead_letter`; other 4xx aren't retried. Each try is a `DeliveryAttempt` row (`GET /events/{id}/attempts`). A second replay while one is pending gets 409; startup moves leftover `pending` to `dead_letter`. `get_engine` is now the DB dependency tests override.
- Health checks (`app/checks.py`, `app/alerts.py`): `checks.yaml` (gitignored; `checks.example.yaml` committed, path via `HOOKWATCH_CHECKS_FILE`) loaded and validated in the lifespan; a `CheckScheduler` runs one asyncio task per check, calling sync `run_check` via `asyncio.to_thread`. Results go to `CheckResult`; current state (`unknown`/`up`/`down`, consecutive failures, last change) to `CheckState`. Slack alert (`SLACK_WEBHOOK_URL`, else log) only on transition to down or down to up; optional `failures_before_down`. `GET /checks`, `GET /checks/{name}/results`.

## Remaining HookWatch milestones

### 3. Signature verification

Implement both checks by hand with `hmac` and `hashlib`, not the Stripe SDK. Explaining them is an interview talking point.

- **Secrets** come from env vars: `STRIPE_WEBHOOK_SECRET` (`whsec_...`) and `GITHUB_WEBHOOK_SECRET`. If a source has no secret configured, leave its events `unchecked`.
- **Stripe:** the `Stripe-Signature` header looks like `t=<unix ts>,v1=<hex>[,v1=<hex>...]`. The signed payload is `f"{t}.".encode() + raw_body`. Compute HMAC-SHA256 with the secret and compare to every `v1` with `hmac.compare_digest`. Reject if the timestamp is more than 300 seconds old.
- **GitHub:** the `X-Hub-Signature-256` header is `sha256=<hex>`, an HMAC-SHA256 of the raw body.
- **Behavior:** always store the event, even if rejected, because support needs to see failures. Set `signature_status` to `verified` or `rejected`, and add a `signature_detail` column with the reason (missing header, stale timestamp, mismatch). Return 400 on rejected so the sender sees the failure. This changes the table, so the local `hookwatch.db` has to be deleted.
- **Structure:** put verifiers in `app/verification.py` with one function per provider, plus a registry keyed by source.
- **Tests:** sign payloads inside the tests. Cover valid, wrong secret, stale timestamp, missing header, multiple `v1` values, a body changed after signing, and no secret configured.
- **Manual test:** run `stripe listen --all-snapshot --forward-to localhost:8000/hooks/stripe` (it prints the `whsec_` secret), then `stripe trigger payment_intent.succeeded`.

### 4. Replay

- `POST /events/{id}/replay` with a `{"target_url": ...}` body. Send the original raw body, its content type, and an `X-HookWatch-Replay: <event id>` header.
- Add a `DeliveryAttempt` table: event id, attempt number, status code, error, duration in ms, timestamp.
- Retry on connection errors and 5xx responses with exponential backoff (1, 2, 4, 8, 16 s; 5 attempts max). After the last failure, mark the event `dead_letter`. Run retries in the background (FastAPI `BackgroundTasks`).
- Add `GET /events/{id}/attempts` and a `?status=dead_letter` filter on `/events`.
- Test with `httpx.MockTransport`, and make the backoff delays injectable so tests don't sleep.
- Talking point: a replayed Stripe event fails the target's signature check once its timestamp passes the tolerance window. That's why Stripe offers its own resend.

### 5. Health checks and alerts

- A `checks.yaml` lists each check's name, URL, method, expected status, interval in seconds, and timeout. Commit a `checks.example.yaml`.
- Run checks with a scheduler started in the app's lifespan (APScheduler, or a plain asyncio loop). Store each result: status code, latency in ms, ok or failed, error, time.
- Send a Slack alert through `SLACK_WEBHOOK_URL` **only when a check's state changes** (up to down, or down to up). This is the alert-fatigue lesson from Witbe.
- Add `GET /checks`, showing each check's current state, last latency, and when it last changed.

### 6. Dashboard and polish

- `GET /` serves one Jinja2 page with recent events (signature status, replay status) and a table of check status. Keep it plain; no frontend build.
- README: screenshots, an architecture sketch, a CI badge, and a Troubleshooting section (signature mismatches, stale timestamps, why a replay fails).
- Stretch goals: a password on the dashboard, p95 latency, deploying it somewhere so it runs 24/7.

## Project 2: Stripe to HubSpot Sync

Start this after HookWatch, in a new repo (for example `mlouis594/stripe-hubspot-sync`) with the same stack.

**Mapping (first version):** Stripe `customer.email` becomes the HubSpot contact `email` (the match key; contacts with no email are skipped and logged). `name` splits into `firstname` and `lastname`. Three custom contact properties: `stripe_customer_id` (prevents duplicates when an email changes), `stripe_total_spend` (sum of successful payments), and `stripe_last_payment`.

**Milestones:**
1. Accounts: Stripe in test mode with sample data, a HubSpot developer account, and a test account. HubSpot reworked its developer platform recently, so check its current docs for how to create an OAuth app before building.
2. OAuth 2.0 authorization-code flow (`/oauth/start` and `/oauth/callback`), with tokens stored and refreshed before they expire.
3. Paginated reads of Stripe customers and payments, mapped to HubSpot fields.
4. Idempotent upserts (search first, then create or update), the custom properties, a dry-run mode, and a sync report (created, updated, skipped, failed).
5. Robustness: back off on 429 and respect `Retry-After`, refresh the token on 401, retry on 5xx. Add structured logs and tests with mocked APIs.
6. Real time: Stripe webhooks (`customer.created`, `customer.updated`, `payment_intent.succeeded`) for incremental updates, reusing HookWatch's verification code.
7. Docs: README, setup guide, mapping table, and `TROUBLESHOOTING.md` (`invalid_grant`, missing scopes, 429s).

## Resume targets

Add these bullets to the resume only once the features actually work.

- HookWatch: "Built a webhook monitoring service in Python (FastAPI) with Stripe and GitHub signature verification, request logging, and replay with exponential-backoff retries" / "Added scheduled API health checks with response-time tracking and Slack alerts on outages and recoveries"
- Sync: "Built an integration syncing Stripe customers and payments to HubSpot CRM via OAuth 2.0, with automatic token refresh and idempotent upserts" / "Handled rate limits, pagination, and transient failures with retry logic, and wrote a troubleshooting guide for common integration errors"

## Interview stories to prepare

1. How webhook signature verification works, and why a valid body can still be rejected (stale timestamp, body changed in transit).
2. A real bug from the build and how it was tracked down. Keep notes as you go.
3. What happens when an OAuth token expires mid-sync, and how the code recovers.
4. How to help a customer whose webhooks "stopped working," using HookWatch as the example.
5. How the Witbe monitoring and alerting work shaped HookWatch's alerts.
