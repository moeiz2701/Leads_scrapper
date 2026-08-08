"""Synchronous SQLAlchemy sessions.

Deliberately sync, not async. RQ workers are synchronous processes, and mixing
an async ORM into them buys nothing at this scale while costing a lot of
complexity. FastAPI route handlers that touch the DB are declared ``def`` (not
``async def``) so Starlette runs them in its threadpool. The genuinely async work
— httpx fan-out and Playwright — happens inside a stage job via ``asyncio.run``,
where it is isolated and easy to reason about.
"""

from __future__ import annotations

import functools
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from leadscraper.config import get_settings


@functools.lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=max(5, settings.concurrency * 2),
        max_overflow=10,
        future=True,
    )


@functools.lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


def get_session() -> Session:
    return get_sessionmaker()()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any exception."""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
