"""Persistence layer. Schema mirrors implementation.md §11."""

from leadscraper.db.base import Base
from leadscraper.db.session import get_session, session_scope

__all__ = ["Base", "get_session", "session_scope"]
