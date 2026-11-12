"""Tests for the rate limiter and daily budget, using fake clocks (no real waiting)."""

import pytest

from app.limits import BudgetExceededError, DailyBudget, RateLimitedError, RateLimiter


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_rate_limiter_allows_up_to_the_limit_then_blocks():
    limiter = RateLimiter(limit=3, window_s=60, clock=FakeClock())
    for _ in range(3):
        limiter.hit("1.2.3.4")
    with pytest.raises(RateLimitedError) as err:
        limiter.hit("1.2.3.4")
    assert 0 < err.value.retry_after_s <= 61


def test_rate_limiter_tracks_each_visitor_separately():
    limiter = RateLimiter(limit=1, window_s=60, clock=FakeClock())
    limiter.hit("visitor-a")
    limiter.hit("visitor-b")  # different visitor: allowed
    with pytest.raises(RateLimitedError):
        limiter.hit("visitor-a")


def test_rate_limiter_frees_up_as_the_window_slides():
    clock = FakeClock()
    limiter = RateLimiter(limit=2, window_s=60, clock=clock)
    limiter.hit("v")
    clock.now += 30
    limiter.hit("v")
    clock.now += 31  # the first hit is now older than 60s
    limiter.hit("v")
    with pytest.raises(RateLimitedError):
        limiter.hit("v")


def test_budget_blocks_once_the_cap_is_reached():
    budget = DailyBudget(cap_usd=0.01, today=lambda: "2026-11-12")
    budget.check()
    budget.record(0.006)
    budget.check()
    budget.record(0.005)
    with pytest.raises(BudgetExceededError):
        budget.check()


def test_budget_resets_on_a_new_day():
    day = {"value": "2026-11-12"}
    budget = DailyBudget(cap_usd=0.01, today=lambda: day["value"])
    budget.record(0.02)
    with pytest.raises(BudgetExceededError):
        budget.check()
    day["value"] = "2026-11-13"
    budget.check()
    assert budget.spent_usd == 0


def test_budget_cap_of_zero_means_no_cap():
    budget = DailyBudget(cap_usd=0)
    budget.record(1_000)
    budget.check()


def test_budget_ignores_unknown_costs():
    budget = DailyBudget(cap_usd=1)
    budget.record(None)
    assert budget.spent_usd == 0
