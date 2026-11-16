# HookWatch

Catch, verify, inspect, and replay webhooks, and monitor the health of the APIs you depend on.

> Work in progress. Milestones 1–5 are done.

## Roadmap

- [x] Skeleton: FastAPI app, health endpoint, tests, Docker, CI
- [x] Webhook receiver that stores every request
- [x] Stripe and GitHub signature verification
- [x] Replay with retries, backoff, and a dead-letter list
- [x] Scheduled API health checks with Slack alerts
- [ ] Dashboard page

## Run it locally (WSL / Linux)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

pytest -v                      # run the tests
uvicorn app.main:app --reload  # start the server on http://localhost:8000
```

Then check it's up:

```bash
curl http://localhost:8000/health
# {"status":"ok","version":"0.1.0"}
```

Interactive API docs are at http://localhost:8000/docs.

## Endpoints

| Method | Path | What it does |
| --- | --- | --- |
| `POST` | `/hooks/{source}` | Receive a webhook, verify its signature, and store it (source is lowercase, e.g. `stripe`). Returns 400 if the signature is rejected; the event is stored anyway |
| `GET` | `/events` | List events, newest first. Supports `?source=`, `?signature_status=`, `?replay_status=`, `?limit=`, `?offset=` |
| `GET` | `/events/{id}` | One event in full: headers, raw body, parsed JSON |
| `POST` | `/events/{id}/replay` | Re-send the event to `{"target_url": "..."}` with retries. Returns 202; 409 if a replay is already running |
| `GET` | `/events/{id}/attempts` | Every delivery attempt for the event: status code, error, response excerpt, duration |
| `GET` | `/checks` | Every configured health check: `up`/`down`/`unknown`, last latency, last error, when it last changed |
| `GET` | `/checks/{name}/results` | Recent runs of one check, newest first. Supports `?limit=` |
| `GET` | `/health` | Liveness check |

Try it:

```bash
curl -X POST http://localhost:8000/hooks/test \
  -H "Content-Type: application/json" \
  -d '{"type": "order.created", "id": 42}'

curl http://localhost:8000/events
curl http://localhost:8000/events/1
```

Events are stored in `hookwatch.db` (SQLite). Set `HOOKWATCH_DB_URL` to use a different location.
`Authorization` and `Cookie` headers are redacted before storage.

## Signature verification

Stripe and GitHub sign every webhook with HMAC-SHA256 using a secret only you and they know.
HookWatch recomputes the signature over the **raw body bytes** and compares it in constant time
(`hmac.compare_digest`). The code is in [`app/verification.py`](app/verification.py), written by
hand with `hmac` and `hashlib` rather than a provider SDK.

| Source | Secret env var | Header | What's signed |
| --- | --- | --- | --- |
| `stripe` | `STRIPE_WEBHOOK_SECRET` (`whsec_...`) | `Stripe-Signature: t=<ts>,v1=<hex>` | `"<ts>." + body`; rejected if `ts` is over 5 minutes old |
| `github` | `GITHUB_WEBHOOK_SECRET` | `X-Hub-Signature-256: sha256=<hex>` | `body` |

Every event gets a `signature_status`:

- `verified`: the signature matched.
- `rejected`: missing or malformed header, signature mismatch, or a stale Stripe timestamp.
  `signature_detail` says which. The event is **still stored** so you can inspect it, and the
  sender gets a 400 so the failure shows up on their side too.
- `unchecked`: no secret is set for that source, or the source has no verifier.

Find failures with `curl "http://localhost:8000/events?signature_status=rejected"`.

### Try it with Stripe

Put secrets in a `.env` file (gitignored) and start the server with it:

```bash
echo "STRIPE_WEBHOOK_SECRET=whsec_..." >> .env   # printed by `stripe listen`
uvicorn app.main:app --reload --env-file .env
```

In another terminal:

```bash
stripe listen --all-snapshot --forward-to localhost:8000/hooks/stripe
stripe trigger payment_intent.succeeded
```

### Try it with GitHub (no account needed)

```bash
BODY='{"action":"opened"}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$GITHUB_WEBHOOK_SECRET" | awk '{print $2}')"
curl -X POST http://localhost:8000/hooks/github -H "X-Hub-Signature-256: $SIG" -d "$BODY"
```

> Upgrading from milestone 2: the events table gained a `signature_detail` column. There are no
> migrations, so delete `hookwatch.db` and restart.

## Replay

`POST /events/{id}/replay` re-sends a stored webhook to any URL, which is useful for re-running a
failed delivery against your fixed endpoint, or sending production traffic to a local build.

- **What's sent:** the original raw body, byte for byte, and the original headers (signature
  headers included), plus `X-HookWatch-Replay: <event id>`. Connection-level headers like `Host`
  are regenerated, and redacted headers (`Authorization`, `Cookie`) are left out.
- **Retries:** connection errors, timeouts, `5xx`, and `429` are retried up to 5 attempts, waiting
  1, 2, 4, then 8 seconds between them (exponential backoff). Other `4xx` responses aren't retried:
  the target understood the request and refused it, so sending it again won't help.
- **Dead letter:** if nothing succeeds, the event's `replay_status` becomes `dead_letter`.
  List them with `GET /events?replay_status=dead_letter`.
- **Background:** the API answers `202 Accepted` immediately and retries run in a background task.
  Retries don't survive a restart, so on startup any event still `pending` is moved to
  `dead_letter`. A production system would use a durable queue (Celery, RQ, SQS) instead.

Every attempt is recorded with its status code, error, duration, and the first 500 characters of
the target's response, which usually says exactly why it refused.

```bash
curl -X POST http://localhost:8000/events/1/replay \
  -H "Content-Type: application/json" \
  -d '{"target_url": "http://localhost:8000/hooks/replayed"}'

curl http://localhost:8000/events/1/attempts
```

> **Why a replayed Stripe event can fail:** the replay carries Stripe's original
> `Stripe-Signature`, whose timestamp is part of what's signed. Once it's more than 5 minutes old
> the target rejects it as stale, and HookWatch can't re-sign it without the target's secret.
> That's why Stripe offers its own "Resend" button: Stripe signs the resend with a fresh timestamp.

> Upgrading from milestone 3: the events table gained a `replay_status` column. Delete
> `hookwatch.db` and restart.

## Health checks and alerts

HookWatch can also watch the APIs you depend on. List them in `checks.yaml`:

```bash
cp checks.example.yaml checks.yaml   # then edit; checks.yaml is gitignored
```

```yaml
checks:
  - name: GitHub API
    url: https://api.github.com
    interval_seconds: 60        # default 60, minimum 5
    timeout_seconds: 10         # default 10
    expected_status: 200        # default 200
    failures_before_down: 2     # default 1
```

Each check runs on its own interval from the moment the server starts. Every run is stored
(status code, latency, error), and `GET /checks` shows where each one stands.

**Alerts only fire when a check changes state:** once when it goes down, once when it recovers
(with how long the outage lasted). A check that fails for an hour produces one alert, not sixty.
Alerting on every failure trains people to mute the channel, and then real outages get missed.
`failures_before_down` goes further: with `2`, a single failed run doesn't page anyone.

```
🔴 GitHub API is DOWN: Timed out after 10s (GET https://api.github.com)
🟢 GitHub API recovered after 4m 10s (HTTP 200 in 182 ms)
```

Alerts go to Slack when `SLACK_WEBHOOK_URL` is set (an
[incoming webhook](https://api.slack.com/messaging/webhooks) URL; put it in `.env`), and to the
server log otherwise. Check state is kept in the database, so a restart doesn't re-alert an
outage that's already been reported.

> Tip: set `SLACK_WEBHOOK_URL=http://localhost:8000/hooks/slack` to have HookWatch catch its own
> alerts, then read them at `/events?source=slack`.

Notes: use a different file with `HOOKWATCH_CHECKS_FILE`. Run a single worker, because each
uvicorn worker would run every check. An invalid `checks.yaml` stops the server at startup with
a message saying what's wrong.

> Upgrading from milestone 4: two new tables (`checkresult`, `checkstate`) are created
> automatically; no need to delete `hookwatch.db`.

## Run it with Docker

```bash
docker build -t hookwatch .
docker run --rm -p 8000:8000 hookwatch
```

The database lives in `/app/data` inside the container. To keep events when the container is
recreated, mount a volume there. To run health checks, mount your config. Secrets come from `.env`:

```bash
docker run --rm -p 8000:8000 --env-file .env \
  -v hookwatch-data:/app/data \
  -v "$PWD/checks.yaml:/app/checks.yaml:ro" hookwatch
```
