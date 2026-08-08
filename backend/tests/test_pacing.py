"""§7 — pacing and circuit breaking."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from leadscraper.core.pacing import (
    SOCIAL_PACING,
    BreakerRegistry,
    BudgetExceededError,
    CircuitBreaker,
    PacingPolicy,
)
from leadscraper.enums import SourceStatus


def test_delays_fall_inside_the_documented_window() -> None:
    """§7: randomised 3–10s, jittered. Fixed delays are the bursty pattern that
    trips naive limiters, so the value must actually vary."""
    policy = PacingPolicy(delay_min=3.0, delay_max=10.0)
    rng = random.Random(0)
    samples = [policy.next_delay(rng) for _ in range(200)]
    assert all(3.0 <= s <= 10.0 for s in samples)
    assert len(set(samples)) > 100


def test_social_pacing_is_much_slower(  ) -> None:
    """§6.6: 8–20s and concurrency 1, far below the core-source budget."""
    assert SOCIAL_PACING.delay_min == 8.0
    assert SOCIAL_PACING.delay_max == 20.0
    assert SOCIAL_PACING.concurrency == 1


@pytest.mark.parametrize(
    "kwargs",
    [{"delay_min": -1}, {"delay_min": 10, "delay_max": 3}, {"concurrency": 0}],
)
def test_invalid_policies_are_rejected(kwargs) -> None:
    with pytest.raises(ValueError):
        PacingPolicy(**kwargs)


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #


def test_starts_healthy() -> None:
    breaker = CircuitBreaker(source="google_maps")
    assert breaker.status() is SourceStatus.OK
    assert breaker.allows_request()


def test_three_consecutive_failures_trip_it() -> None:
    """§7: 3 consecutive failures → pause that source for 30 minutes."""
    breaker = CircuitBreaker(source="google_maps", failure_threshold=3)
    breaker.record_failure("timeout")
    breaker.record_failure("timeout")
    assert breaker.status() is SourceStatus.THROTTLED
    breaker.record_failure("timeout")
    assert breaker.status() is SourceStatus.CIRCUIT_OPEN
    assert breaker.tripped_by == "failure_streak"
    assert not breaker.allows_request()


def test_a_success_clears_the_failure_streak() -> None:
    breaker = CircuitBreaker(source="google_maps", failure_threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert breaker.status() is SourceStatus.THROTTLED
    assert not breaker.is_open()


def test_any_captcha_trips_immediately() -> None:
    """§7 singles CAPTCHA out: 'any CAPTCHA on a source'. Grinding against a
    challenge is the fastest route to a hard block."""
    breaker = CircuitBreaker(source="google_maps")
    breaker.record_captcha()
    assert breaker.is_open()
    assert breaker.tripped_by == "captcha"


@pytest.mark.parametrize("status_code", [429, 503])
def test_throttling_responses_trip_it(status_code: int) -> None:
    breaker = CircuitBreaker(source="businesslist_pk")
    breaker.record_blocked(status_code)
    assert breaker.is_open()
    assert breaker.last_error == f"http_{status_code}"


def test_five_empty_successes_trip_it() -> None:
    """§5.5's most important guard, and the one easiest to leave out: five
    consecutive 200s that yielded nothing means the markup moved. Without this
    you 'harvest 1,500 blank rows and not notice'."""
    breaker = CircuitBreaker(source="zameen", empty_threshold=5)
    for _ in range(4):
        breaker.record_success(produced=False)
    assert not breaker.is_open()
    breaker.record_success(produced=False)
    assert breaker.is_open()
    assert breaker.tripped_by == "empty_streak"


def test_a_productive_success_clears_the_empty_streak() -> None:
    breaker = CircuitBreaker(source="zameen", empty_threshold=3)
    breaker.record_success(produced=False)
    breaker.record_success(produced=False)
    breaker.record_success(produced=True)
    breaker.record_success(produced=False)
    assert not breaker.is_open()


def test_the_pause_expires() -> None:
    breaker = CircuitBreaker(source="google_maps", pause_minutes=30)
    now = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    breaker.record_captcha(now=now)
    assert breaker.is_open(now + timedelta(minutes=29))
    assert not breaker.is_open(now + timedelta(minutes=31))


def test_daily_budget_is_a_hard_ceiling() -> None:
    """§7: 'Per-source daily request budget — hard ceiling, enforced in code.'"""
    breaker = CircuitBreaker(source="google_maps", daily_request_budget=2)
    breaker.record_request()
    breaker.record_request()
    assert breaker.budget_exhausted()
    assert not breaker.allows_request()
    with pytest.raises(BudgetExceededError, match="daily budget"):
        breaker.record_request()


def test_reset_restores_health() -> None:
    breaker = CircuitBreaker(source="google_maps")
    breaker.record_captcha()
    breaker.reset()
    assert breaker.status() is SourceStatus.OK


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_one_blocked_source_does_not_pause_the_others() -> None:
    """§7: 'Continue the run with remaining sources.' A partial run beats none."""
    registry = BreakerRegistry()
    registry.get("google_maps").record_captcha()
    active = registry.active_sources(["google_maps", "business_website", "pakplay"])
    assert active == ["business_website", "pakplay"]


def test_registry_reports_statuses_for_the_ui() -> None:
    registry = BreakerRegistry()
    registry.get("google_maps").record_failure()
    registry.get("pakplay").record_success()
    assert registry.statuses() == {
        "google_maps": SourceStatus.THROTTLED,
        "pakplay": SourceStatus.OK,
    }


def test_registry_returns_the_same_breaker_per_source() -> None:
    registry = BreakerRegistry()
    assert registry.get("pakplay") is registry.get("pakplay")
