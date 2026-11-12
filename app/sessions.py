"""Chat sessions: one agent (with its memory) per repo a visitor is exploring.

Sessions live in memory. That's fine for a demo on one server; a bigger
deployment would store them in a database like Redis instead.
"""

import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from app.agent import AgentResult, RepoAgent

OVERVIEW_PROMPT = """\
Give me an overview of this repository for a developer seeing it for the first time.

Explore it first (repo info, the root folder, the README, and the main source folder), \
then answer with these sections:

## What it is
One short paragraph: what the project does and who it's for.

## Tech stack
Main languages, frameworks and notable libraries (check the dependency files).

## How it's organized
The important top-level folders and files, one line each on what they hold.

## Key files to read first
3-6 files that are the best starting points, and why.

## How to run it
Only if the repo says how (README, scripts, config). Otherwise skip this section."""


class SessionNotFoundError(Exception):
    pass


class SessionBusyError(Exception):
    """The agent is still answering the previous question in this session."""


class SessionLimitError(Exception):
    """This session has used up its question allowance."""


@dataclass
class Session:
    id: str
    repo: str  # "owner/name"
    agent: RepoAgent
    created: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    turns: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class SessionStore:
    """Keeps recent sessions in memory, forgetting the oldest and idle ones."""

    def __init__(
        self,
        agent_factory: Callable[[str, str], RepoAgent],
        max_sessions: int = 200,
        idle_timeout_s: int = 60 * 60,
        max_turns: int = 30,
    ):
        self._factory = agent_factory
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self._guard = threading.Lock()
        self.max_sessions = max_sessions
        self.idle_timeout_s = idle_timeout_s
        self.max_turns = max_turns

    def create(self, owner: str, repo: str) -> Session:
        """Start a session for a repo. Raises RepoNotFoundError for bad repos."""
        agent = self._factory(owner, repo)
        agent.tools.get_repo_info()  # fail fast if the repo doesn't exist
        session = Session(id=secrets.token_urlsafe(16), repo=f"{owner}/{repo}", agent=agent)
        with self._guard:
            self._evict_stale()
            self._sessions[session.id] = session
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)  # drop the oldest
        return session

    def get(self, session_id: str) -> Session:
        with self._guard:
            self._evict_stale()
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(session_id)
            self._sessions.move_to_end(session_id)
            return session

    def ask(self, session: Session, question: str) -> AgentResult:
        """Ask a question in a session, one at a time (the agent's memory isn't shared-safe)."""
        if session.turns >= self.max_turns:
            raise SessionLimitError(
                f"This chat reached {self.max_turns} questions. Start a new one to keep going."
            )
        if not session.lock.acquire(blocking=False):
            raise SessionBusyError("Still working on your previous question. Please wait.")
        try:
            result = session.agent.ask(question)
            session.turns += 1
            session.last_used = time.time()
            return result
        finally:
            session.lock.release()

    def _evict_stale(self) -> None:
        cutoff = time.time() - self.idle_timeout_s
        for sid in [s.id for s in self._sessions.values() if s.last_used < cutoff]:
            del self._sessions[sid]
