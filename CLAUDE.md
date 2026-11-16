# HookWatch: instructions for Claude

Read `docs/HANDOVER.md` before starting work. It has the project goal, current status, the spec for every remaining milestone, and the follow-on project.

## Working agreement

- Marlon wants to move fast: build each milestone end to end (code, tests, README update), then explain what you did in plain language. He needs to be able to walk an interviewer through every part, so explain the *why*, not only the *what*.
- One commit per milestone, message `Milestone N: <summary>`, with a short bullet list in the body.
- Keep `pytest -v` green. CI (GitHub Actions) runs the tests on every push.
- Tick the milestone's box in the README roadmap and keep the README's endpoint table current.

## Environment

- Development happens in WSL (Ubuntu) on Windows. The repo lives in the WSL filesystem.
- Python 3.12, virtualenv in `.venv` (`source .venv/bin/activate`, `pip install -r requirements-dev.txt`).
- Run: `uvicorn app.main:app --reload`, then http://localhost:8000/docs from the Windows browser.
- Docker Desktop with WSL integration is available.

## Code conventions

- FastAPI + SQLModel on SQLite. Routers live in `app/routers/`, tables in `app/models.py`, response shapes in `app/schemas.py`.
- Webhook bodies are stored as raw `bytes`. Never decode or re-serialize them before signature checks or replays.
- Tests use the `client` fixture in `tests/conftest.py` (fresh in-memory DB per test). Mock outbound HTTP; tests never hit the network.
- Config comes from environment variables (`HOOKWATCH_DB_URL`, and the secrets listed in the handover). Never commit secrets; `.env` is gitignored.
- There's no migration tool. A change to a table means deleting the local `hookwatch.db`; say so when a milestone does that.
