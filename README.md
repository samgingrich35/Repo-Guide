# Repo Guide

An AI agent that explores any public GitHub repository and answers questions about it, citing the files it read. Built with the Claude API and developed using Claude Code.

> 🚧 Work in progress. See [ROADMAP.md](ROADMAP.md) for the build plan and current status.

## Run locally

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"      # Windows (use .venv/bin/pip on Mac/Linux)
uvicorn app.main:app --reload
```

Then open http://localhost:8000/health.

## Checks

```bash
ruff check . && ruff format --check .
pytest -v
```
