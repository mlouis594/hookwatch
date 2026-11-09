# HookWatch

Catch, verify, inspect, and replay webhooks, and monitor the health of the APIs you depend on.

> Work in progress. Milestone 1 (project skeleton) is done.

## Roadmap

- [x] Skeleton: FastAPI app, health endpoint, tests, Docker, CI
- [ ] Webhook receiver that stores every request
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

## Run it with Docker

```bash
docker build -t hookwatch .
docker run --rm -p 8000:8000 hookwatch
```
