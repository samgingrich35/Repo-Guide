"""Abuse and cost protection for the public demo.

Two guards:
- RateLimiter: each visitor (by IP address) gets N requests per time window.
- DailyBudget: once the estimated Claude spend for today passes a cap,
  new questions are refused until tomorrow (UTC). This keeps a public
  link from draining the API credit, whatever traffic it gets.

Both live in memory, which suits a single server. Several servers would
share them through something like Redis instead.
"""

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import UTC, datetime


class RateLimitedError(Exception):
    def __init__(self, retry_after_s: int):
        super().__init__(f"Too many requests. Try again in {retry_after_s} seconds.")
        self.retry_after_s = retry_after_s


class BudgetExceededError(Exception):
    pass


class RateLimiter:
    """Sliding window: at most `limit` hits per `window_s` seconds for each key."""

    def __init__(self, limit: int, window_s: int, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> None:
        """Count one request for `key`, or raise RateLimitedError if over the limit."""
        now = self._clock()
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= now - self.window_s:
                hits.popleft()  # forget hits that fell out of the window
            if len(hits) >= self.limit:
                raise RateLimitedError(retry_after_s=int(hits[0] + self.window_s - now) + 1)
            hits.append(now)


class DailyBudget:
    """Tracks estimated spend per UTC day and refuses work past the cap."""

    def __init__(self, cap_usd: float, today: Callable[[], str] | None = None):
        self.cap_usd = cap_usd
        self._today = today or (lambda: datetime.now(UTC).date().isoformat())
        self._day = self._today()
        self._spent = 0.0
        self._lock = threading.Lock()

    @property
    def spent_usd(self) -> float:
        with self._lock:
            self._roll_over()
            return self._spent

    def check(self) -> None:
        """Raise BudgetExceededError if today's cap is used up."""
        if self.cap_usd > 0 and self.spent_usd >= self.cap_usd:
            raise BudgetExceededError(
                "The demo has used its Claude budget for today. Please come back tomorrow."
            )

    def record(self, cost_usd: float | None) -> None:
        with self._lock:
            self._roll_over()
            self._spent += cost_usd or 0.0

    def _roll_over(self) -> None:
        today = self._today()
        if today != self._day:
            self._day, self._spent = today, 0.0
