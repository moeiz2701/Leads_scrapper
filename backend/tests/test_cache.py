"""§7 — content-addressed cache.

``DiskArchive`` is tested standalone (no DB). ``FetchCache``'s TTL behaviour is
covered in the integration tests, which need Postgres.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from leadscraper.core.cache import DiskArchive, normalise_url, url_hash


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://example.com/x", "https://example.com/x/"),
        ("https://example.com/x", "https://www.example.com/x"),
        ("https://example.com/x", "HTTPS://Example.COM/x"),
        ("https://example.com/x", "https://example.com:443/x"),
        ("http://example.com/x", "http://example.com:80/x"),
        ("https://example.com/x?a=1&b=2", "https://example.com/x?b=2&a=1"),
        ("https://example.com/x", "https://example.com/x?utm_source=fb&fbclid=abc"),
    ],
)
def test_equivalent_urls_share_a_cache_key(a: str, b: str) -> None:
    """Every one of these is the same page. If they hash differently the 60–80%
    saving §7 depends on quietly erodes as tracking params leak into URLs."""
    assert url_hash(a) == url_hash(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://example.com/x", "https://example.com/y"),
        ("https://example.com/x?page=1", "https://example.com/x?page=2"),
        ("https://example.com/x", "https://other.com/x"),
    ],
)
def test_distinct_urls_do_not_collide(a: str, b: str) -> None:
    assert url_hash(a) != url_hash(b)


def test_normalise_url_keeps_meaningful_query_params() -> None:
    assert "page=3" in normalise_url("https://businesslist.pk/category/salon?page=3&utm_medium=x")
    assert "utm_medium" not in normalise_url(
        "https://businesslist.pk/category/salon?page=3&utm_medium=x"
    )


def test_archive_round_trip(tmp_path: Path) -> None:
    archive = DiskArchive(tmp_path)
    key = url_hash("https://example.com/contact")
    body = b"<html><a href='https://wa.me/923001234567'>chat</a></html>"

    assert archive.read(key) is None
    path = archive.write(key, body)
    assert archive.exists(key)
    assert archive.read(key) == body
    # Stored compressed — a run's worth of HTML is mostly whitespace and markup.
    assert path.suffix == ".gz"
    assert gzip.decompress(path.read_bytes()) == body


def test_archive_shards_by_hash_prefix(tmp_path: Path) -> None:
    """Thousands of files in one directory degrades badly on Windows."""
    archive = DiskArchive(tmp_path)
    key = url_hash("https://example.com/a")
    path = archive.path_for(key)
    assert path.parent.name == key[2:4]
    assert path.parent.parent.name == key[:2]


def test_archive_overwrite_replaces_body(tmp_path: Path) -> None:
    archive = DiskArchive(tmp_path)
    key = url_hash("https://example.com/a")
    archive.write(key, b"old")
    archive.write(key, b"new")
    assert archive.read(key) == b"new"


def test_archive_leaves_no_temp_files(tmp_path: Path) -> None:
    """Write-then-rename: a crash mid-write must never leave a truncated body
    that a later run parses as real content."""
    archive = DiskArchive(tmp_path)
    archive.write(url_hash("https://example.com/a"), b"body")
    assert list(tmp_path.rglob("*.tmp")) == []


def test_archive_read_of_corrupt_file_returns_none(tmp_path: Path) -> None:
    archive = DiskArchive(tmp_path)
    key = url_hash("https://example.com/a")
    path = archive.path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not gzip at all")
    assert archive.read(key) is None


def test_archive_delete(tmp_path: Path) -> None:
    archive = DiskArchive(tmp_path)
    key = url_hash("https://example.com/a")
    archive.write(key, b"body")
    assert archive.delete(key) is True
    assert archive.delete(key) is False
    assert archive.read(key) is None
