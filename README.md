# HookWatch

Catch, verify, inspect, and replay webhooks, and monitor the health of the APIs you depend on.

> Work in progress. Milestones 1–2 are done.

## Roadmap

- [x] Skeleton: FastAPI app, health endpoint, tests, Docker, CI
- [x] Webhook receiver that stores every request
- [ ] Stripe and GitHub signature verification
- [ ] Replay with retries, backoff, and a dead-letter list
- [ ] Scheduled API health checks with Slack alerts
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
| `POST` | `/hooks/{source}` | Receive a webhook and store it (source is lowercase, e.g. `stripe`) |
| `GET` | `/events` | List events, newest first. Supports `?source=`, `?limit=`, `?offset=` |
| `GET` | `/events/{id}` | One event in full: headers, raw body, parsed JSON |
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

## Run it with Docker

```bash
docker build -t hookwatch .
docker run --rm -p 8000:8000 hookwatch
```
