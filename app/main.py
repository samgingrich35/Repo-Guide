"""The web server: routes that start a repo session and chat with the agent.

Run locally with:  uvicorn app.main:app --reload
API docs (auto-generated): http://localhost:8000/docs
"""

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated

import anthropic
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import __doc__ as app_description
from app.agent import AgentResult, RepoAgent
from app.config import get_settings
from app.limits import BudgetExceededError, DailyBudget, RateLimitedError, RateLimiter
from app.sessions import (
    OVERVIEW_PROMPT,
    Session,
    SessionBusyError,
    SessionLimitError,
    SessionNotFoundError,
    SessionStore,
)
from app.tools import RepoNotFoundError, RepoTools, ToolError, parse_repo_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Repo Guide", description=app_description, version="0.1.0")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# The web page may only load scripts from us and the one CDN we use, so even if a
# repo's text slipped malicious HTML past the sanitizer, the browser wouldn't run it.
PAGE_CSP = (
    "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; style-src 'self'; "
    "img-src 'self' data: https:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path == "/":  # /docs needs inline scripts, so only the app page gets CSP
        response.headers["Content-Security-Policy"] = PAGE_CSP
    return response


# ---------- request / response shapes ----------


class StartRequest(BaseModel):
    repo_url: str = Field(max_length=300, examples=["https://github.com/pallets/flask"])


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class FileLink(BaseModel):
    path: str
    url: str


class StepOut(BaseModel):
    tool: str
    input: dict
    is_error: bool


class AnswerOut(BaseModel):
    answer: str
    steps: list[StepOut]
    files_read: list[FileLink]
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    stop: str


class SessionOut(BaseModel):
    session_id: str
    repo: str
    overview: AnswerOut


# ---------- wiring (swapped out for fakes in tests) ----------


@lru_cache
def get_store() -> SessionStore:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise HTTPException(
            503, "The server has no ANTHROPIC_API_KEY set. Add it to .env and restart."
        )
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def make_agent(owner: str, repo: str) -> RepoAgent:
        return RepoAgent(
            client=client,
            tools=RepoTools(owner, repo, token=settings.github_token),
            model=settings.claude_model,
            max_steps=settings.max_agent_steps,
            effort=settings.effort,
        )

    return SessionStore(make_agent)


StoreDep = Annotated[SessionStore, Depends(get_store)]


@dataclass
class Guards:
    """Per-visitor rate limits plus a daily spending cap for the whole server."""

    new_chats: RateLimiter
    messages: RateLimiter
    budget: DailyBudget


@lru_cache
def get_guards() -> Guards:
    settings = get_settings()
    hour = 60 * 60
    return Guards(
        new_chats=RateLimiter(settings.new_chats_per_hour, hour),
        messages=RateLimiter(settings.messages_per_hour, hour),
        budget=DailyBudget(settings.daily_budget_usd),
    )


GuardsDep = Annotated[Guards, Depends(get_guards)]


def _check_guards(limiter: RateLimiter, guards: Guards, request: Request) -> None:
    """Refuse the request early (before spending anything) if a limit is hit."""
    visitor = request.client.host if request.client else "unknown"
    try:
        limiter.hit(visitor)
        guards.budget.check()
    except RateLimitedError as e:
        raise HTTPException(429, str(e), headers={"Retry-After": str(e.retry_after_s)}) from e
    except BudgetExceededError as e:
        raise HTTPException(503, str(e)) from e


# ---------- routes ----------


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    """The web page: paste a repo URL, read the overview, then chat."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    """Used by hosting platforms (and us) to check the server is up, and which
    commit it's running (baked into the image at build time)."""
    return {"status": "ok", "version": os.environ.get("GIT_SHA", "dev")}


@app.post("/api/sessions", status_code=201)
def start_session(
    req: StartRequest, request: Request, store: StoreDep, guards: GuardsDep
) -> SessionOut:
    """Paste a repo URL: start a chat about it and get an automatic overview."""
    _check_guards(guards.new_chats, guards, request)
    try:
        owner, repo = parse_repo_url(req.repo_url)
        session = store.create(owner, repo)
    except RepoNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ToolError as e:
        raise HTTPException(502, str(e)) from e
    overview = _ask(store, guards, session, OVERVIEW_PROMPT)
    return SessionOut(session_id=session.id, repo=session.repo, overview=overview)


@app.post("/api/sessions/{session_id}/messages")
def chat(
    session_id: str, req: ChatRequest, request: Request, store: StoreDep, guards: GuardsDep
) -> AnswerOut:
    """Ask a follow-up question in an existing chat."""
    _check_guards(guards.messages, guards, request)
    try:
        session = store.get(session_id)
    except SessionNotFoundError as e:
        raise HTTPException(404, "Chat not found or expired. Paste the repo URL again.") from e
    return _ask(store, guards, session, req.message)


def _ask(store: SessionStore, guards: Guards, session: Session, question: str) -> AnswerOut:
    """Run the agent and translate failures into clear HTTP errors."""
    try:
        result = store.ask(session, question)
    except SessionBusyError as e:
        raise HTTPException(409, str(e)) from e
    except SessionLimitError as e:
        raise HTTPException(429, str(e)) from e
    except anthropic.AuthenticationError as e:
        raise HTTPException(503, "The server's Anthropic API key is invalid.") from e
    except anthropic.RateLimitError as e:
        raise HTTPException(429, "Claude is busy right now. Try again in a minute.") from e
    except anthropic.APIError as e:
        raise HTTPException(502, f"Claude API error: {e.__class__.__name__}") from e
    guards.budget.record(result.cost_usd)
    return _to_out(result, session.agent.tools)


def _to_out(result: AgentResult, tools: RepoTools) -> AnswerOut:
    return AnswerOut(
        answer=result.answer,
        steps=[StepOut(tool=s.tool, input=s.input, is_error=s.is_error) for s in result.steps],
        files_read=[FileLink(path=p, url=tools.file_url(p)) for p in result.files_read],
        input_tokens=(
            result.usage.input_tokens
            + result.usage.cache_read_tokens
            + result.usage.cache_write_tokens
        ),
        output_tokens=result.usage.output_tokens,
        cost_usd=result.cost_usd,
        stop=result.stop,
    )
