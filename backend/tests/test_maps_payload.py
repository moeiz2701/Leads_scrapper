"""§5.1 — Maps search payload parsing, pinned against captured live responses.

The fixtures are real Google Maps search responses (Lahore salon and Lahore
restaurant), trimmed to four results each and gzipped. They exist because the
payload is a positional array: index 178 means "phone" only by a convention
Google never promised to keep. A golden file is the only thing that turns a
silent reshuffle into a failing test instead of §5.5's "1,500 blank rows and
nobody notices".

If these fail after a Google change, re-run scripts/spike_maps_payload.py,
diff the indices, and update FIELD_PATHS — do not delete the test.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from leadscraper.core.maps_payload import (
    MapsResult,
    parse_payload,
    parse_result,
    parse_search_results,
    strip_guard,
)
from leadscraper.core.phone import normalise
from leadscraper.enums import LineType

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> bytes:
    return gzip.decompress((FIXTURES / name).read_bytes())


@pytest.fixture(scope="module")
def salon_results() -> list[MapsResult]:
    return parse_search_results(_load("maps_search_salon.json.gz"))


@pytest.fixture(scope="module")
def food_results() -> list[MapsResult]:
    return parse_search_results(_load("maps_search_food.json.gz"))


# --------------------------------------------------------------------------- #
# The finding this whole spike existed to establish
# --------------------------------------------------------------------------- #


def test_search_payload_carries_phone_numbers(salon_results, food_results) -> None:
    """The costing question behind §14.

    §5.1 says you must open a place panel per business to get a phone, and §14
    budgets 700 interactions / 28 min on that. The search payload has them
    already — so Stage 2 parses ~60 responses instead of interacting 700 times.
    """
    for results in (salon_results, food_results):
        assert results
        assert all(r.phone_raw for r in results)


def test_payload_phones_normalise_to_pk_mobiles_and_landlines(salon_results) -> None:
    """Payload phones must survive §9.1/§9.2 — a phone we cannot classify is not
    a lead, and this is where the two halves of the system first meet."""
    for result in salon_results:
        parsed = normalise(result.phone_raw or "")
        assert parsed is not None, result.phone_raw
        assert parsed.e164.startswith("+92")
        assert parsed.line_type in {LineType.MOBILE, LineType.LANDLINE, LineType.UAN}


def test_identity_fields_are_fully_populated(salon_results, food_results) -> None:
    """name / place_id / address / lat / lng were 100% in the live sample."""
    for results in (salon_results, food_results):
        for r in results:
            assert r.name
            assert r.place_id and r.place_id.startswith("ChIJ")
            assert r.address
            assert r.lat is not None and r.lng is not None


def test_coordinates_land_inside_pakistan(salon_results, food_results) -> None:
    """A cheap guard against lat/lng swapping indices — the classic positional
    parser bug, and one that would silently poison §10.1's 150m dedupe."""
    for results in (salon_results, food_results):
        for r in results:
            assert 23.0 < (r.lat or 0) < 37.5, r.name
            assert 60.0 < (r.lng or 0) < 78.0, r.name


def test_rating_is_present_and_plausible(salon_results, food_results) -> None:
    for results in (salon_results, food_results):
        for r in results:
            assert r.rating is not None
            assert 0.0 <= r.rating <= 5.0


def test_review_count_missing_stays_none_never_zero(salon_results, food_results) -> None:
    """Payload richness varies between responses for the same endpoint: the
    lighter salon response omitted review counts entirely, the food one had them
    for every result. §10.2 scores business_signal from this, so a fabricated
    0 would push good leads down the ranking. Missing must stay missing."""
    assert all(r.review_count is None for r in salon_results)
    assert all(isinstance(r.review_count, int) and r.review_count > 0 for r in food_results)


def test_known_business_parses_exactly(salon_results) -> None:
    """One fully-pinned record. If Google reshuffles indices, this fails first
    and names the field that moved."""
    paragon = next(r for r in salon_results if r.name and "Paragon" in r.name)
    assert paragon.place_id == "ChIJt107G8EFGTkRnh2as4GxD3Y"
    assert normalise(paragon.phone_raw).e164 == "+924232294007"
    assert paragon.category == "Hair salon"
    assert paragon.website == "https://www.instagram.com/paragonsalonlhr/"
    assert paragon.maps_url == (
        "https://www.google.com/maps/place/?q=place_id:ChIJt107G8EFGTkRnh2as4GxD3Y"
    )


def test_website_may_be_a_social_profile(salon_results) -> None:
    """85% of results carried a website, and for PK SMBs it is often an Instagram
    profile rather than a domain — which is a §6.4 bio-link lead, not a §5.2
    website-module lead. Stage 2 must route on the URL, not assume a domain."""
    websites = [r.website for r in salon_results if r.website]
    assert any("instagram.com" in w for w in websites)


# --------------------------------------------------------------------------- #
# Defensive parsing — the payload is positional and unversioned
# --------------------------------------------------------------------------- #


def test_paginated_envelope_format_parses() -> None:
    """The lazy-loaded pages Maps sends as you scroll are wrapped differently
    from the first page: ``{"c":0,"d":")]}'\\n[...]"}/*""*/``, with the payload
    JSON-encoded inside ``d`` and a comment trailer.

    A plain json.loads raises "Extra data" on that, which the parser used to
    swallow — so four of every five captured payloads silently produced zero
    results while the run reported success. Measured cost: 20 unique businesses
    per query instead of 56, from responses already fetched and paid for.
    """
    results = parse_search_results(_load("maps_search_paginated.json.gz"))
    assert len(results) == 4
    assert all(r.phone_raw for r in results)
    assert all(r.place_id for r in results)


def test_envelope_and_bare_payloads_use_the_same_field_map() -> None:
    """Once unwrapped the inner array has the identical shape, so a single field
    map serves both. If that ever stops being true, this fails."""
    paginated = parse_search_results(_load("maps_search_paginated.json.gz"))
    bare = parse_search_results(_load("maps_search_salon.json.gz"))
    for results in (paginated, bare):
        assert all(r.name and r.place_id and r.lat is not None for r in results)


def test_multiple_documents_in_one_body_are_all_read() -> None:
    doc = ")]}'\n" + json.dumps([None] * 65)
    assert parse_search_results(doc + '/*""*/' + doc) == []


def test_unwrapping_is_depth_bounded() -> None:
    """A malformed envelope that nests into itself must not recurse forever."""
    nested = json.dumps({"d": json.dumps({"d": json.dumps({"d": "[]"})})})
    assert parse_search_results(nested) == []


def test_json_guard_is_stripped() -> None:
    assert strip_guard(")]}'\n[1,2,3]") == "[1,2,3]"
    assert parse_payload(")]}'\n[1,2,3]") == [1, 2, 3]


@pytest.mark.parametrize(
    "junk",
    [b"", b"not json at all", b")]}'\n{not json}", b"<html>blocked</html>"],
)
def test_unparseable_bodies_yield_no_results_rather_than_raising(junk: bytes) -> None:
    """A shape change must degrade to zero results for that response, not kill
    the run — the circuit breaker in §7 is what should notice, not a traceback."""
    assert parse_search_results(junk) == []


def test_missing_indices_degrade_field_by_field() -> None:
    """One reshuffled index should cost that field, not the whole record."""
    record = [None] * 100
    record[11] = "Some Salon"
    record[78] = "ChIJfake"
    result = parse_result(record)
    assert result is not None
    assert result.name == "Some Salon"
    assert result.phone_raw is None
    assert result.rating is None


def test_record_without_name_or_place_id_is_dropped() -> None:
    assert parse_result([None] * 200) is None
    assert parse_result("not a record") is None
    assert parse_result(None) is None
