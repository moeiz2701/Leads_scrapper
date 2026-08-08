"""Content-addressed raw-fetch cache (§7).

§7 calls this "the biggest single win" — it cuts request volume 60–80% across
runs, which is what keeps the crawler under rate limits by design rather than by
apology. §2 adds the other half of the rationale: keeping every raw body means a
selector break is a re-parse, not a re-scrape.

Split in two on purpose:

* ``DiskArchive`` — pure filesystem, no database. Independently testable and
  usable from a throwaway recon script.
* ``FetchCache`` — ``DiskArchive`` plus the ``raw_fetches`` manifest, which is
  what enforces TTL and lets you ask "what did we fetch, and when".
"""

from __future__ import annotations

import gzip
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leadscraper.config import Settings, get_settings
from leadscraper.db.models import RawFetch


class FetchKind(StrEnum):
    """Drives TTL. Listings churn; detail pages rarely change."""

    LISTING = "listing"
    DETAIL = "detail"


def url_hash(url: str) -> str:
    """Stable content-addressing key. Normalised so trivial URL variants share."""
    return hashlib.sha256(normalise_url(url).encode("utf-8")).hexdigest()


def normalise_url(url: str) -> str:
    """Strip the noise that makes identical pages look like distinct URLs.

    Without this, a tracking parameter on an inbound link turns a cache hit into
    a fresh fetch, and the 60–80% saving quietly erodes.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if (scheme == "https" and netloc.endswith(":443")) or (
        scheme == "http" and netloc.endswith(":80")
    ):
        netloc = netloc.rsplit(":", 1)[0]

    drop_prefixes = ("utm_", "fbclid", "gclid", "mc_", "_ga")
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith(drop_prefixes)
        )
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, query, ""))


@dataclass(frozen=True, slots=True)
class CachedBody:
    url: str
    status: int
    body: bytes
    content_type: str | None
    fetched_at: datetime

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, errors="replace")


class DiskArchive:
    """Gzipped bodies on disk, sharded two levels deep by hash prefix.

    Sharding matters: a single run produces thousands of files, and Windows
    directory listing degrades badly past a few tens of thousands in one folder.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, key: str) -> Path:
        return self.root / key[:2] / key[2:4] / f"{key}.gz"

    def write(self, key: str, body: bytes) -> Path:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a crash mid-write never leaves a truncated body
        # that a later run would happily parse as real content.
        tmp = path.with_suffix(".gz.tmp")
        tmp.write_bytes(gzip.compress(body))
        tmp.replace(path)
        return path

    def read(self, key: str) -> bytes | None:
        path = self.path_for(key)
        if not path.exists():
            return None
        try:
            return gzip.decompress(path.read_bytes())
        except (OSError, EOFError, gzip.BadGzipFile):
            return None

    def exists(self, key: str) -> bool:
        return self.path_for(key).exists()

    def delete(self, key: str) -> bool:
        path = self.path_for(key)
        if path.exists():
            path.unlink()
            return True
        return False


class FetchCache:
    """Disk archive + ``raw_fetches`` manifest, with TTL enforcement."""

    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        archive: DiskArchive | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.archive = archive or DiskArchive(self.settings.raw_archive_path)

    def ttl_for(self, kind: FetchKind) -> timedelta:
        days = (
            self.settings.cache_ttl_listing_days
            if kind is FetchKind.LISTING
            else self.settings.cache_ttl_detail_days
        )
        return timedelta(days=days)

    def get(self, url: str, *, ignore_ttl: bool = False) -> CachedBody | None:
        """Return a cached body, or ``None`` on miss or expiry.

        ``ignore_ttl=True`` is the re-parse path from §2: when a selector breaks
        you want every body you ever fetched, however stale.
        """
        key = url_hash(url)
        row = self.session.get(RawFetch, key)
        if row is None:
            return None
        if not ignore_ttl and row.expires_at and row.expires_at <= datetime.now(UTC):
            return None
        body = self.archive.read(key)
        if body is None:
            return None
        return CachedBody(
            url=row.url,
            status=row.status or 0,
            body=body,
            content_type=row.content_type,
            fetched_at=row.fetched_at or datetime.now(UTC),
        )

    def put(
        self,
        url: str,
        body: bytes,
        *,
        status: int = 200,
        content_type: str | None = None,
        source: str | None = None,
        kind: FetchKind = FetchKind.DETAIL,
    ) -> str:
        key = url_hash(url)
        path = self.archive.write(key, body)
        now = datetime.now(UTC)

        row = self.session.get(RawFetch, key)
        if row is None:
            row = RawFetch(url_hash=key)
            self.session.add(row)

        row.url = normalise_url(url)
        row.status = status
        row.content_type = content_type
        row.body_path = str(path)
        row.body_bytes = len(body)
        row.source = source
        row.kind = kind.value
        row.fetched_at = now
        row.expires_at = now + self.ttl_for(kind)
        self.session.flush()
        return key

    def is_fresh(self, url: str, kind: FetchKind = FetchKind.DETAIL) -> bool:
        row = self.session.get(RawFetch, url_hash(url))
        if row is None or row.expires_at is None:
            return False
        return row.expires_at > datetime.now(UTC)

    def stats(self) -> dict[str, int]:
        """Manifest counters for the §13 Screen 2 progress panel."""
        entries, total_bytes = self.session.execute(
            select(func.count(RawFetch.url_hash), func.coalesce(func.sum(RawFetch.body_bytes), 0))
        ).one()
        return {"entries": int(entries), "bytes": int(total_bytes)}
