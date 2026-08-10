"""Shared fixtures.

DB-backed tests run against a **separate** ``*_test`` database, created on
demand. That separation is not fastidiousness: real runs commit into the main
database, and tests that query globally (``select(Contact)``) would otherwise
see production rows and fail — or worse, pass for the wrong reason.

Tests are skipped, not failed, when Postgres is unreachable, so the pure-logic
suite stays runnable with nothing but ``uv sync``.
"""

from __future__ import annotations

import functools
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from leadscraper.config import get_settings
from leadscraper.core.cache import DiskArchive, FetchCache
from leadscraper.db import models  # noqa: F401  — registers tables on Base.metadata
from leadscraper.db.base import Base

TEST_DB_SUFFIX = "_test"


def _test_database_url() -> str:
    settings = get_settings()
    return settings.database_url.rsplit("/", 1)[0] + f"/{settings.postgres_db}{TEST_DB_SUFFIX}"


@functools.lru_cache(maxsize=1)
def _test_engine() -> Engine | None:
    """Create the test database and schema, or ``None`` if Postgres is down."""
    settings = get_settings()
    admin_url = settings.database_url.rsplit("/", 1)[0] + "/postgres"
    db_name = f"{settings.postgres_db}{TEST_DB_SUFFIX}"

    try:
        admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            exists = conn.execute(
                text("select 1 from pg_database where datname = :n"), {"n": db_name}
            ).scalar()
            if not exists:
                conn.execute(text(f'create database "{db_name}"'))
        admin.dispose()
    except SQLAlchemyError:
        return None

    engine = create_engine(_test_database_url(), future=True)
    try:
        # Dropped first, then recreated. ``create_all`` alone only ever *adds*
        # tables, so a column added to a model since the last run would be
        # missing from a test database created before it — the suite then fails
        # with an UndefinedColumn that looks like a code bug and is not one.
        # This database exists only for the suite and every test rolls back, so
        # there is nothing here worth keeping between sessions.
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
    except SQLAlchemyError:
        return None
    return engine


requires_db = pytest.mark.skipif(
    _test_engine() is None,
    reason="Postgres not reachable — run `docker compose up -d` from the repo root",
)


@pytest.fixture
def db_session() -> Iterator[Session]:
    """A session inside a transaction that is always rolled back.

    Tests therefore leave no rows behind and can run in any order, repeatedly,
    without a reset step.
    """
    engine = _test_engine()
    assert engine is not None
    connection = engine.connect()
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
