"""§7.1 — egress routing.

The behaviour under test is the one that costs money if it is wrong: Maps must
refuse to run without PK egress rather than return confidently wrong results.
"""

from __future__ import annotations

import pytest

from leadscraper.config import Settings
from leadscraper.core.proxy import (
    ProxyMode,
    ProxyNotConfiguredError,
    proxy_available,
    resolve_proxy,
)


def _settings(**overrides) -> Settings:
    base = {
        "proxy_mode": "direct",
        "proxy_url": "",
        "proxy_required_sources": "google_maps",
    }
    return Settings(**{**base, **overrides})


def test_direct_mode_for_geo_neutral_sources() -> None:
    for source in ("business_website", "pakplay", "zameen", "businesslist_pk"):
        assert resolve_proxy(source, _settings()).is_direct


def test_maps_refuses_to_run_without_pk_egress() -> None:
    """§7.1: 'a non-PK IP returns the wrong businesses entirely. This is a
    correctness issue, not an evasion one.' Silently falling back to direct
    would produce a full run of plausible, wrong data."""
    with pytest.raises(ProxyNotConfiguredError, match="geo-ranked"):
        resolve_proxy("google_maps", _settings())


def test_maps_runs_once_a_proxy_is_configured() -> None:
    cfg = resolve_proxy(
        "google_maps",
        _settings(proxy_mode="residential", proxy_url="http://user:pass@pk.proxy.example:8000"),
    )
    assert cfg.mode is ProxyMode.RESIDENTIAL
    assert not cfg.is_direct


def test_residential_mode_without_a_url_still_refuses() -> None:
    with pytest.raises(ProxyNotConfiguredError):
        resolve_proxy("google_maps", _settings(proxy_mode="residential", proxy_url=""))


def test_httpx_and_playwright_dialects() -> None:
    """Playwright wants credentials split out of the URL; httpx wants them in it."""
    settings = _settings(
        proxy_mode="residential", proxy_url="http://user:pass@pk.proxy.example:8000"
    )
    cfg = resolve_proxy("business_website", settings)
    assert cfg.httpx_proxy() == "http://user:pass@pk.proxy.example:8000"
    assert cfg.playwright_proxy() == {
        "server": "http://pk.proxy.example:8000",
        "username": "user",
        "password": "pass",
    }


def test_direct_dialects_are_none() -> None:
    cfg = resolve_proxy("business_website", _settings())
    assert cfg.httpx_proxy() is None
    assert cfg.playwright_proxy() is None


def test_proxy_available_gates_the_ui_toggle() -> None:
    assert not proxy_available(_settings())
    assert proxy_available(_settings(proxy_mode="residential", proxy_url="http://p:1"))


def test_required_sources_are_configurable() -> None:
    settings = _settings(proxy_required_sources="google_maps, foodpanda")
    assert settings.proxy_required_source_set == {"google_maps", "foodpanda"}
    with pytest.raises(ProxyNotConfiguredError):
        resolve_proxy("foodpanda", settings)
