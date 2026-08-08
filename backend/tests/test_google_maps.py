"""§5.1 Maps source — the parts that do not need a browser."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from leadscraper.config import Settings
from leadscraper.core.maps_payload import MapsResult, parse_search_results
from leadscraper.core.proxy import ProxyNotConfiguredError
from leadscraper.sources.google_maps import (
    GoogleMapsSource,
    merge_results,
    search_url,
)
from leadscraper.taxonomy import build_query_plan

FIXTURES = Path(__file__).parent / "fixtures"


def _result(place_id: str, **overrides) -> MapsResult:
    base = dict(
        name="X", place_id=place_id, address=None, lat=None, lng=None,
        phone_raw=None, website=None, rating=None, review_count=None,
        price_range=None, category=None,
    )
    return MapsResult(**{**base, **overrides})


def test_search_url_encodes_the_query() -> None:
    url = search_url("salon in Gulberg, Lahore")
    assert url.startswith("https://www.google.com/maps/search/")
    assert " " not in url
    assert "Gulberg" in url


# --------------------------------------------------------------------------- #
# Merging payloads for one query
# --------------------------------------------------------------------------- #


def test_richer_record_wins_when_a_place_appears_twice() -> None:
    """§5.1: one navigation can emit several search responses of differing
    richness. A thin one must never displace a fat one."""
    thin = _result("ChIJa", phone_raw=None, rating=None)
    fat = _result("ChIJa", phone_raw="0300-1234567", rating=4.5, address="Somewhere")
    merged = merge_results([[thin], [fat]])
    assert len(merged) == 1
    assert merged[0].phone_raw == "0300-1234567"


def test_thin_record_does_not_overwrite_a_fat_one() -> None:
    thin = _result("ChIJa")
    fat = _result("ChIJa", phone_raw="0300-1234567", rating=4.5)
    merged = merge_results([[fat], [thin]])
    assert merged[0].phone_raw == "0300-1234567"


def test_merge_preserves_first_seen_order() -> None:
    merged = merge_results([[_result("ChIJa"), _result("ChIJb")], [_result("ChIJc")]])
    assert [r.place_id for r in merged] == ["ChIJa", "ChIJb", "ChIJc"]


def test_merge_falls_back_to_name_and_address_without_a_place_id() -> None:
    a = _result(None, name="Salon A", address="Street 1")
    b = _result(None, name="Salon A", address="Street 1")
    c = _result(None, name="Salon B", address="Street 2")
    assert len(merge_results([[a, b, c]])) == 2


def test_merge_of_nothing_is_empty() -> None:
    assert merge_results([]) == []
    assert merge_results([[], []]) == []


def test_merge_across_real_payloads_dedupes_by_place_id() -> None:
    raw = gzip.decompress((FIXTURES / "maps_search_salon.json.gz").read_bytes())
    salon = parse_search_results(raw)
    merged = merge_results([salon, salon, salon])
    assert len(merged) == len(salon)
    assert len({r.place_id for r in merged}) == len(merged)


# --------------------------------------------------------------------------- #
# Construction / §7.1 gate
# --------------------------------------------------------------------------- #


def _settings(**overrides) -> Settings:
    base = {
        "proxy_mode": "direct",
        "proxy_url": "",
        "proxy_required_sources": "google_maps",
        "browser_workers": 2,
        "delay_min_seconds": 1.0,
        "delay_max_seconds": 2.0,
    }
    return Settings(**{**base, **overrides})


def test_constructing_without_pk_egress_refuses() -> None:
    """§7.1 — Maps geo-ranks, so a non-PK IP is a correctness problem. The
    module refuses at construction rather than producing a plausible wrong run."""
    with pytest.raises(ProxyNotConfiguredError):
        GoogleMapsSource(settings=_settings())


def test_opting_out_is_a_deliberate_env_change() -> None:
    """Removing google_maps from PROXY_REQUIRED_SOURCES is the documented,
    conscious way to run direct during development."""
    source = GoogleMapsSource(settings=_settings(proxy_required_sources=""))
    assert source.proxy.is_direct


def test_pacing_and_breaker_come_from_settings() -> None:
    source = GoogleMapsSource(
        settings=_settings(proxy_required_sources="", browser_workers=4)
    )
    assert source.policy.concurrency == 4
    assert source.policy.delay_min == 1.0
    assert source.breaker.source == "google_maps"


# --------------------------------------------------------------------------- #
# Query planning (§5.1)
# --------------------------------------------------------------------------- #


def test_query_plan_carries_the_tile_for_area_attribution() -> None:
    """§12.1 column 5 is `area`, and the tile is the only place it can come
    from — a Maps address does not reliably name the commercial district."""
    plan = build_query_plan("Lahore", "salon", synonym_limit=2)
    assert len(plan) == 24
    assert plan[0].tile == "DHA"
    assert plan[0].city == "Lahore"
    assert plan[0].query == "salon in DHA, Lahore"
    assert len({spec.query for spec in plan}) == len(plan)
