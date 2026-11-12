# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this project is
Repo Guide: a web app where a user pastes a public GitHub repo URL, gets an automatic overview of its structure, then chats with an AI agent that explores the repo with tools (list files, search, read file) and cites the files it used. See `ROADMAP.md` for the build plan and progress.

## Layout
- `app/main.py`: FastAPI routes
- `app/config.py`: settings from env vars / `.env` (never hardcode secrets)
- `app/tools.py`: GitHub tools the agent can call
- `app/agent.py`: the Claude tool-use loop
- `app/sessions.py`: in-memory chat sessions + the overview prompt
- `app/static/`: the web page (plain HTML/CSS/JS, no build step)
- `app/cli.py`: terminal chat (`python -m app.cli <repo-url>`)
- `tests/`: pytest. Claude and GitHub are always mocked, so tests are free and offline.

## Commands
- Install: `python -m venv .venv` then `.venv/Scripts/pip install -e ".[dev]"` (Windows) or `.venv/bin/pip ...`
- Run: `uvicorn app.main:app --reload` → http://localhost:8000
- Lint: `ruff check . && ruff format --check .`
- Test: `pytest -v`

## Working rules
- Run lint and tests before every commit; CI runs the same checks.
- Every new feature gets a test. Never make real API calls in tests.
- Keep secrets in `.env` (git-ignored); add new settings to `.env.example` too.
- Small, focused commits, one roadmap step at a time; tick the step in `ROADMAP.md` when done.
- Keep code beginner-readable: short functions, docstrings explaining *why*.
