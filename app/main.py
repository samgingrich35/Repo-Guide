"""The web server: routes that start a repo session and chat with the agent.

Run locally with:  uvicorn app.main:app --reload
API docs (auto-generated): http://localhost:8000/docs
"""

import logging
from functools import lru_cache
from typing import Annotated

import anthropic
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app import __doc__ as app_description
from app.agent import AgentResult, RepoAgent
from app.config import get_settings
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


# ---------- routes ----------


@app.get("/health")
def health() -> dict[str, str]:
    """Used by hosting platforms (and us) to check the server is up."""
    return {"status": "ok"}


@app.post("/api/sessions", status_code=201)
def start_session(req: StartRequest, store: StoreDep) -> SessionOut:
    """Paste a repo URL: start a chat about it and get an automatic overview."""
    try:
        owner, repo = parse_repo_url(req.repo_url)
        session = store.create(owner, repo)
    except RepoNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ToolError as e:
        raise HTTPException(502, str(e)) from e
    overview = _ask(store, session, OVERVIEW_PROMPT)
    return SessionOut(session_id=session.id, repo=session.repo, overview=overview)


@app.post("/api/sessions/{session_id}/messages")
def chat(session_id: str, req: ChatRequest, store: StoreDep) -> AnswerOut:
    """Ask a follow-up question in an existing chat."""
    try:
        session = store.get(session_id)
    except SessionNotFoundError as e:
        raise HTTPException(404, "Chat not found or expired. Paste the repo URL again.") from e
    return _ask(store, session, req.message)


def _ask(store: SessionStore, session: Session, question: str) -> AnswerOut:
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
