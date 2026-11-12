# Repo Guide

An AI agent that explores any public GitHub repository and answers questions about it, citing the files it read. Built with the Claude API and developed using Claude Code.

> 🚧 Work in progress. See [ROADMAP.md](ROADMAP.md) for the build plan and current status.

## Run locally

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"      # Windows (use .venv/bin/pip on Mac/Linux)
uvicorn app.main:app --reload
```

Then open http://localhost:8000/docs to try the API.

### Setup: your API key
Copy `.env.example` to `.env` and paste your key from https://console.anthropic.com/settings/keys
after `ANTHROPIC_API_KEY=`. `.env` is git-ignored, so your key never reaches GitHub.

### Chat from the terminal
```bash
python -m app.cli https://github.com/pallets/flask
```

### API
| Method | Path | What it does |
|---|---|---|
| `POST` | `/api/sessions` | `{"repo_url": "..."}` → starts a chat and returns an automatic overview |
| `POST` | `/api/sessions/{id}/messages` | `{"message": "..."}` → asks a follow-up question |
| `GET` | `/health` | Liveness check |

## Checks

```bash
ruff check . && ruff format --check .
pytest -v
```
