"""Tests for the web API and chat sessions, with fake Claude + fake GitHub (offline, free)."""

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from app.agent import RepoAgent
from app.config import Settings
from app.main import app, get_store
from app.sessions import SessionBusyError, SessionStore
from app.tools import RepoTools
from tests.test_agent import FakeClaude, reply, text, tool_use
from tests.test_tools import fake_github

REPO_URL = "https://github.com/octo/demo"


def make_store(claude: FakeClaude, **kwargs) -> SessionStore:
    def factory(owner, repo):
        return RepoAgent(
            client=claude,
            tools=RepoTools(owner, repo, http=fake_github()),
            model="claude-haiku-5-5",
        )

    return SessionStore(factory, **kwargs)


@pytest.fixture
def api():
    """Returns a function that builds a test client around a scripted Claude."""

    def build(*replies, **store_kwargs):
        claude = FakeClaude(*replies)
        store = make_store(claude, **store_kwargs)
        app.dependency_overrides[get_store] = lambda: store
        return TestClient(app), claude, store

    yield build
    app.dependency_overrides.clear()


# ---------- starting a session ----------


def test_start_session_returns_an_overview_with_steps_and_source_links(api):
    client, claude, _ = api(
        reply(tool_use("read_file", {"path": "app/main.py"}), stop="tool_use"),
        reply(text("## What it is\nA demo app.")),
    )
    resp = client.post("/api/sessions", json={"repo_url": REPO_URL})

    assert resp.status_code == 201
    body = resp.json()
    assert body["repo"] == "octo/demo"
    assert body["session_id"]
    assert body["overview"]["answer"].startswith("## What it is")
    assert body["overview"]["steps"] == [
        {"tool": "read_file", "input": {"path": "app/main.py"}, "is_error": False}
    ]
    assert body["overview"]["files_read"] == [
        {"path": "app/main.py", "url": "https://github.com/octo/demo/blob/main/app/main.py"}
    ]
    assert body["overview"]["cost_usd"] > 0
    # The agent was asked for an overview with the expected sections.
    assert "## How it's organized" in claude.requests[0]["messages"][0]["content"][0]["text"]


@pytest.mark.parametrize("url", ["https://gitlab.com/x/y", "https://github.com/octo/missing"])
def test_bad_or_missing_repo_is_a_404(api, url):
    client, claude, _ = api()
    resp = client.post("/api/sessions", json={"repo_url": url})
    assert resp.status_code == 404
    assert claude.requests == []  # never paid for a Claude call


# ---------- chatting ----------


def test_follow_up_question_continues_the_same_conversation(api):
    client, claude, _ = api(reply(text("Overview.")), reply(text("It uses Flask.")))
    session_id = client.post("/api/sessions", json={"repo_url": REPO_URL}).json()["session_id"]

    resp = client.post(f"/api/sessions/{session_id}/messages", json={"message": "What web lib?"})

    assert resp.status_code == 200
    assert resp.json()["answer"] == "It uses Flask."
    assert len(claude.requests[1]["messages"]) == 3  # overview Q, overview A, new Q


def test_unknown_session_is_a_404(api):
    client, _, _ = api()
    resp = client.post("/api/sessions/nope/messages", json={"message": "hi"})
    assert resp.status_code == 404
    assert "expired" in resp.json()["detail"]


@pytest.mark.parametrize("message", ["", "x" * 2001])
def test_empty_or_huge_messages_are_rejected(api, message):
    client, _, _ = api(reply(text("Overview.")))
    session_id = client.post("/api/sessions", json={"repo_url": REPO_URL}).json()["session_id"]
    resp = client.post(f"/api/sessions/{session_id}/messages", json={"message": message})
    assert resp.status_code == 422


def test_question_limit_per_chat(api):
    client, _, _ = api(reply(text("Overview.")), max_turns=1)
    session_id = client.post("/api/sessions", json={"repo_url": REPO_URL}).json()["session_id"]
    resp = client.post(f"/api/sessions/{session_id}/messages", json={"message": "more?"})
    assert resp.status_code == 429
    assert "Start a new one" in resp.json()["detail"]


# ---------- failures talking to Claude ----------


def _api_error(cls, status):
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://api.anthropic.com"))
    return cls("boom", response=response, body=None)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (_api_error(anthropic.AuthenticationError, 401), 503),
        (_api_error(anthropic.RateLimitError, 429), 429),
        (_api_error(anthropic.InternalServerError, 500), 502),
    ],
)
def test_claude_errors_become_clear_http_errors(api, error, status):
    client, claude, _ = api()

    def explode(**kwargs):
        raise error

    claude.create = explode
    resp = client.post("/api/sessions", json={"repo_url": REPO_URL})
    assert resp.status_code == status


def test_missing_api_key_is_explained(monkeypatch):
    monkeypatch.setattr(
        "app.main.get_settings", lambda: Settings(anthropic_api_key="", _env_file=None)
    )
    get_store.cache_clear()
    resp = TestClient(app).post("/api/sessions", json={"repo_url": REPO_URL})
    get_store.cache_clear()
    assert resp.status_code == 503
    assert "ANTHROPIC_API_KEY" in resp.json()["detail"]


# ---------- the session store itself ----------


def test_store_forgets_the_oldest_session_when_full():
    store = make_store(FakeClaude(), max_sessions=2)
    first = store.create("octo", "demo")
    store.create("octo", "demo")
    store.create("octo", "demo")
    with pytest.raises(Exception, match=first.id):
        store.get(first.id)


def test_store_refuses_a_second_question_while_one_is_running():
    store = make_store(FakeClaude())
    session = store.create("octo", "demo")
    session.lock.acquire()  # pretend a question is in progress
    with pytest.raises(SessionBusyError):
        store.ask(session, "another one")
