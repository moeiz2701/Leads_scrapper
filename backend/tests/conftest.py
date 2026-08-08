"""Shared fixtures.

DB-backed tests run against the Compose Postgres and are skipped (not failed)
when it is not up, so the pure-logic suite — which is most of it — stays runnable
with nothing but `uv sync`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from leadscraper.config import get_settings
from leadscraper.core.cache import DiskArchive, FetchCache
from leadscraper.db.session import get_engine


def _database_reachable() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("select 1"))
        return True
    except SQLAlchemyError:
        return False


requires_db = pytest.mark.skipif(
    not _database_reachable(),
    reason="Postgres not reachable — run `docker compose up -d` from the repo root",
)


@pytest.fixture
def db_session() -> Iterator[Session]:
    """A session inside a transaction that is always rolled back.

    Tests share one database and never leave rows behind, so they can run in any
    order and repeatedly without a reset step.
    """
    connection = get_engine().connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        # A test that deliberately triggers an IntegrityError leaves the
        # transaction already deassociated; rolling it back again is noise.
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture
def fetch_cache(db_session: Session, tmp_path: Path) -> FetchCache:
    """Cache wired to a throwaway archive dir so tests never touch data/raw."""
    return FetchCache(
        session=db_session,
        settings=get_settings(),
        archive=DiskArchive(tmp_path),
    )
