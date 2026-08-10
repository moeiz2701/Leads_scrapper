"""Runtime settings, loaded from environment / ``.env``.

Everything tunable in §7 (pacing, budgets, cache TTL) and §13 (Settings screen)
resolves through here. Nothing else in the codebase reads ``os.environ``.
"""

from __future__ import annotations

import functools
from pathlib import Path

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------------------------------------------------------- database
    postgres_user: str = "leads"
    postgres_password: str = "leads"
    postgres_db: str = "leads"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    redis_host: str = "localhost"
    redis_port: int = 6379

    # ---------------------------------------------------------------- storage
    raw_archive_dir: Path = Path("./data/raw")

    # ---------------------------------------------------------------- proxy (§7.1)
    proxy_mode: str = "direct"
    proxy_url: str = ""
    proxy_required_sources: str = "google_maps"

    # ---------------------------------------------------------------- optional APIs
    serp_api_key: str = ""
    serp_provider: str = "serper"
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_access_token: str = ""

    # ---------------------------------------------------------------- pacing (§7)
    concurrency: int = Field(default=3, ge=1, le=8)
    browser_workers: int = Field(default=3, ge=1, le=8)
    delay_min_seconds: float = 3.0
    delay_max_seconds: float = 10.0
    cache_ttl_listing_days: int = 7
    cache_ttl_detail_days: int = 30
    dedupe_fuzzy_threshold: int = Field(default=88, ge=50, le=100)
    # §10.1's phone and domain tiers, demoted to corroboration — a shared number
    # or a shared domain lowers the name bar but never waives the 150 m test.
    # Measured floor: among businesses within 150 m that share a number, the
    # highest name similarity is 54.5 and none of them are duplicates, so this
    # must stay comfortably above that. See the §10.1 note and test_dedupe.py.
    dedupe_corroborated_threshold: int = Field(default=75, ge=50, le=100)

    # §6.6 — hard caps for the social module.
    social_requests_per_business: int = 1
    social_cache_days: int = 30
    social_delay_min_seconds: float = 8.0
    social_delay_max_seconds: float = 20.0
    operator_queue_cap: int = 20

    # §7 circuit breaker.
    circuit_break_failures: int = 3
    circuit_break_minutes: int = 30
    # §5.5 — 5 consecutive empty reveals means the selector drifted, not that the
    # data is missing. Break rather than harvest blank rows unnoticed.
    empty_reveal_break_threshold: int = 5

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def raw_archive_path(self) -> Path:
        path = self.raw_archive_dir
        if not path.is_absolute():
            path = BACKEND_ROOT / path
        return path

    @property
    def proxy_required_source_set(self) -> frozenset[str]:
        return frozenset(s.strip() for s in self.proxy_required_sources.split(",") if s.strip())


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
