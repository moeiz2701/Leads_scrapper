"""Pacing and circuit breaking (§7).

§7 is explicit that these exist "for reliability and politeness, not
concealment". Nothing here disguises the crawler — it slows it down, spreads it
out, and stops it when a source says no.

The circuit breaker carries a second job that §5.5 asks for and which is easy to
overlook: counting *empty* successes. A source that returns HTTP 200 with no
phone number on 5 consecutive records has almost certainly changed its markup,
and the failure mode is harvesting 1,500 blank rows without noticing. An empty
streak trips the breaker exactly like an error streak does.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from leadscraper.enums import SourceStatus


@dataclass(frozen=True, slots=True)
class PacingPolicy:
    """§7: concurrency 2–4 never 20, randomised 3–10s jittered delays."""

    delay_min: float = 3.0
    delay_max: float = 10.0
    concurrency: int = 3
    daily_request_budget: int | None = None

    def __post_init__(self) -> None:
        if self.delay_min < 0 or self.delay_max < self.delay_min:
            raise ValueError("delay_max must be >= delay_min >= 0")
        if self.concurrency < 1:
            raise ValueError("concurrency must be >= 1")

    def next_delay(self, rng: random.Random | None = None) -> float:
        """Uniform jitter. Bursty fixed delays are what trip naive limiters."""
        return (rng or random).uniform(self.delay_min, self.delay_max)


# §6.6 caps the social module far harder than the core sources.
SOCIAL_PACING = PacingPolicy(delay_min=8.0, delay_max=20.0, concurrency=1)

# §5.2 websites are paced *per host*, which is what politeness actually means,
# and the §7 3–10s band is calibrated for the opposite situation: hitting one
# search engine or directory hundreds of times in a run. A business's own site
# sees at most 4 requests in a whole run (the §5.2 crawl budget), so a 1–3s gap
# between them is already gentler per host than the §7 default is per source —
# and it is the difference between §14's 8-minute website budget and 16 minutes
# of sleeping. Concurrency here is across *different* domains, never within one.
WEBSITE_PACING = PacingPolicy(delay_min=1.0, delay_max=3.0, concurrency=3)


class BudgetExceededError(RuntimeError):
    """Per-source daily request ceiling hit (§7, 'hard ceiling, enforced in code')."""


@dataclass
class CircuitBreaker:
    """Per-source breaker. §7: 3 consecutive failures or any CAPTCHA → pause 30 min.

    Deliberately not a global kill switch — §7 says to continue the run with the
    remaining sources, because a partial run is worth more than no run.
    """

    source: str
    failure_threshold: int = 3
    empty_threshold: int = 5
    pause_minutes: int = 30
    daily_request_budget: int | None = None

    consecutive_failures: int = 0
    consecutive_empty: int = 0
    requests_today: int = 0
    paused_until: datetime | None = None
    last_error: str | None = None
    _tripped_by: str | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ state

    def status(self, now: datetime | None = None) -> SourceStatus:
        now = now or datetime.now(UTC)
        if self.paused_until and self.paused_until > now:
            return SourceStatus.CIRCUIT_OPEN
        if self.consecutive_failures:
            return SourceStatus.THROTTLED
        return SourceStatus.OK

    def is_open(self, now: datetime | None = None) -> bool:
        return self.status(now) is SourceStatus.CIRCUIT_OPEN

    def allows_request(self, now: datetime | None = None) -> bool:
        if self.is_open(now):
            return False
        return not self.budget_exhausted()

    def budget_exhausted(self) -> bool:
        budget = self.daily_request_budget
        return budget is not None and self.requests_today >= budget

    # ----------------------------------------------------------------- events

    def record_request(self) -> None:
        if self.budget_exhausted():
            raise BudgetExceededError(
                f"Source {self.source!r} hit its daily budget of "
                f"{self.daily_request_budget} requests"
            )
        self.requests_today += 1

    def record_success(self, *, produced: bool = True) -> None:
        """A request that worked. ``produced=False`` means it returned nothing
        useful — a 200 with no phone, which §5.5 treats as suspicious."""
        self.consecutive_failures = 0
        self.last_error = None
        if produced:
            self.consecutive_empty = 0
            self._tripped_by = None
        else:
            self.consecutive_empty += 1
            if self.consecutive_empty >= self.empty_threshold:
                self._trip("empty_streak")

    def record_failure(self, error: str = "", now: datetime | None = None) -> None:
        self.consecutive_failures += 1
        self.last_error = error or None
        if self.consecutive_failures >= self.failure_threshold:
            self._trip("failure_streak", now)

    def record_captcha(self, now: datetime | None = None) -> None:
        """§7: *any* CAPTCHA trips immediately. Grinding against a challenge is
        both rude and the fastest way to a hard block."""
        self.last_error = "captcha"
        self._trip("captcha", now)

    def record_blocked(self, status_code: int, now: datetime | None = None) -> None:
        """429/503 — honour it rather than backing off into the same wall."""
        self.last_error = f"http_{status_code}"
        self._trip(f"http_{status_code}", now)

    def reset(self) -> None:
        self.consecutive_failures = 0
        self.consecutive_empty = 0
        self.paused_until = None
        self.last_error = None
        self._tripped_by = None

    # ---------------------------------------------------------------- private

    def _trip(self, reason: str, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self.paused_until = now + timedelta(minutes=self.pause_minutes)
        self._tripped_by = reason

    @property
    def tripped_by(self) -> str | None:
        return self._tripped_by


class BreakerRegistry:
    """One breaker per source, so a blocked directory never pauses Maps."""

    def __init__(
        self,
        failure_threshold: int = 3,
        empty_threshold: int = 5,
        pause_minutes: int = 30,
    ) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}
        self._failure_threshold = failure_threshold
        self._empty_threshold = empty_threshold
        self._pause_minutes = pause_minutes

    def get(self, source: str) -> CircuitBreaker:
        if source not in self._breakers:
            self._breakers[source] = CircuitBreaker(
                source=source,
                failure_threshold=self._failure_threshold,
                empty_threshold=self._empty_threshold,
                pause_minutes=self._pause_minutes,
            )
        return self._breakers[source]

    def statuses(self, now: datetime | None = None) -> dict[str, SourceStatus]:
        """Feeds the §13 Screen 2 per-source status pills."""
        return {name: b.status(now) for name, b in self._breakers.items()}

    def active_sources(self, candidates: list[str], now: datetime | None = None) -> list[str]:
        return [s for s in candidates if self.get(s).allows_request(now)]
