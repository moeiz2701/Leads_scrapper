"""Parser for Google Maps' internal search payload (§5.1).

What the recon spike established
--------------------------------
§5.1 says "the results list does not show phone numbers. You must open each
place panel", and §14 budgets 700 detail-panel interactions on that basis. That
is true of the rendered DOM. It is **not** true of the network response §5.1
itself tells us to read: the search payload carries the phone number for every
result, along with name, place_id, address, lat/lng, rating and category.

Measured over two live queries (40 results, Lahore salon + restaurant):

    name       40/40   100%      phone      40/40   100%
    place_id   40/40   100%      rating     40/40   100%
    address    40/40   100%      category   40/40   100%
    lat / lng  40/40   100%      website    34/40    85%
    reviews    20/40    50%   <- see below

So Stage 2 for Maps does not need one interaction per business. Reproduce with
``scripts/spike_maps_payload.py``.

Why this parser is written defensively
--------------------------------------
The payload is a positional array — no keys, just nesting, where index 178 means
"phone" only by convention Google never promised to keep. Two things follow:

1. Every access goes through ``_get``, which returns ``None`` rather than raising
   on a shape change. One reshuffled index must not kill a whole run.
2. Payload richness varies *between responses for the same endpoint*. The lighter
   194 KB response in the spike omitted review counts for all 20 results while
   the 828 KB one had them for all 20. Treat a missing field as missing, never as
   zero — §10.2 feeds ``business_signal`` from these, and a fabricated 0 reviews
   would push good leads down the ranking.

``test_maps_payload.py`` pins the field map against a captured fixture, so drift
shows up as a failing test rather than as §5.5's "1,500 blank rows and nobody
notices".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# Google prefixes its JSON payloads with an anti-hijacking guard.
_JSON_GUARD_RE = re.compile(r"^\)\]\}'\s*")
_REVIEW_COUNT_RE = re.compile(r"([\d,]+)\s+review", re.IGNORECASE)

# Index of the results array within the top-level payload.
RESULTS_INDEX = 64

# Field map, relative to one business record. Verified against live payloads —
# see the module docstring for fill rates.
FIELD_PATHS: dict[str, tuple[int | str, ...]] = {
    "name": (11,),
    "place_id": (78,),
    "address": (39,),
    "lat": (9, 2),
    "lng": (9, 3),
    "phone": (178, 0, 0),
    "website": (7, 0),
    "rating": (4, 7),
    "review_text": (4, 3, 1),
    "price_range": (4, 2),
    "category": (13, 0),
}


@dataclass(frozen=True, slots=True)
class MapsResult:
    """One business as it appears in the search payload."""

    name: str | None
    place_id: str | None
    address: str | None
    lat: float | None
    lng: float | None
    phone_raw: str | None
    website: str | None
    rating: float | None
    review_count: int | None
    price_range: str | None
    category: str | None

    @property
    def maps_url(self) -> str | None:
        if not self.place_id:
            return None
        return f"https://www.google.com/maps/place/?q=place_id:{self.place_id}"


def strip_guard(text: str) -> str:
    return _JSON_GUARD_RE.sub("", text).strip()


def parse_payload(raw: bytes | str) -> Any | None:
    """Decode a captured response body, or ``None`` if it is not the JSON payload."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    try:
        return json.loads(strip_guard(text))
    except json.JSONDecodeError:
        return None


def _get(node: Any, path: tuple[int | str, ...]) -> Any:
    """Positional access that yields ``None`` instead of raising on drift."""
    for key in path:
        if node is None:
            return None
        try:
            node = node[key]
        except (IndexError, KeyError, TypeError):
            return None
    return node


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _parse_review_count(value: Any) -> int | None:
    """'1,435 reviews' -> 1435. Missing stays missing, never 0 (§10.2)."""
    if not isinstance(value, str):
        return None
    match = _REVIEW_COUNT_RE.search(value)
    return int(match.group(1).replace(",", "")) if match else None


def parse_result(record: Any) -> MapsResult | None:
    """Build a ``MapsResult`` from one business record, or ``None`` if unusable."""
    if not isinstance(record, list):
        return None
    name = _get(record, FIELD_PATHS["name"])
    place_id = _get(record, FIELD_PATHS["place_id"])
    if not name and not place_id:
        return None

    return MapsResult(
        name=name if isinstance(name, str) else None,
        place_id=place_id if isinstance(place_id, str) else None,
        address=_get(record, FIELD_PATHS["address"]),
        lat=_as_float(_get(record, FIELD_PATHS["lat"])),
        lng=_as_float(_get(record, FIELD_PATHS["lng"])),
        phone_raw=_get(record, FIELD_PATHS["phone"]),
        website=_get(record, FIELD_PATHS["website"]),
        rating=_as_float(_get(record, FIELD_PATHS["rating"])),
        review_count=_parse_review_count(_get(record, FIELD_PATHS["review_text"])),
        price_range=_get(record, FIELD_PATHS["price_range"]),
        category=_get(record, FIELD_PATHS["category"]),
    )


def parse_search_results(raw: bytes | str) -> list[MapsResult]:
    """Every business in one captured Maps search response."""
    payload = parse_payload(raw)
    if payload is None:
        return []
    wrappers = _get(payload, (RESULTS_INDEX,))
    if not isinstance(wrappers, list):
        return []

    results: list[MapsResult] = []
    for wrapper in wrappers:
        result = parse_result(_get(wrapper, (1,)))
        if result is not None:
            results.append(result)
    return results


def looks_like_search_payload(url: str) -> bool:
    return "tbm=map" in url
