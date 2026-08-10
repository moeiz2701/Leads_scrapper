"""Distance between two listings (§10.1).

§10.1's fuzzy dedupe tier is "token-set ratio ≥ 88 **AND** haversine distance
< 150 m". The name half is cheap; the distance half is what stops "Paragon Salon"
in Gulberg merging with "Paragon Salon" in DHA, which is a real chain with real
separate branches and two numbers the operator wants both of.

The grid helper exists so the fuzzy tier does not have to compare every business
against every other one. Blocking is only safe if a cell is wider than the match
radius in *both* axes — see ``CELL_DEGREES``.
"""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_008.8

# §10.1's fuzzy-match radius.
NEAR_DUPLICATE_METRES = 150.0

# Cell size for the blocking grid, in degrees, applied to latitude and longitude
# alike. Correctness condition: a cell must be at least NEAR_DUPLICATE_METRES
# across in both axes, or two businesses 150 m apart could land two cells apart
# and never be compared. Latitude is a flat 111.32 km/deg. Longitude shrinks with
# latitude, and its worst case in scope is the northernmost §3.1 city
# (Abbottabad, ~34.2°N; allow to 37°N for headroom):
#
#     0.002° × 111_320 × cos(37°) = 178 m  >  150 m  ✓
#
# So 0.002° with a 3×3 neighbourhood scan is exhaustive for this threshold.
CELL_DEGREES = 0.002


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, a)))


def grid_cell(lat: float, lng: float) -> tuple[int, int]:
    """The blocking cell a point falls in."""
    return (math.floor(lat / CELL_DEGREES), math.floor(lng / CELL_DEGREES))


def neighbouring_cells(cell: tuple[int, int]) -> list[tuple[int, int]]:
    """The cell and its 8 neighbours — the complete search space for a match."""
    row, col = cell
    return [(row + dr, col + dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1)]


def is_near(
    lat1: float | None,
    lng1: float | None,
    lat2: float | None,
    lng2: float | None,
    radius_m: float = NEAR_DUPLICATE_METRES,
) -> bool:
    """§10.1's distance test, with missing coordinates answering ``False``.

    Missing stays missing: a business with no coordinates has not been shown to
    be near anything, and guessing "probably the same place" is how a merge
    destroys a lead.
    """
    if lat1 is None or lng1 is None or lat2 is None or lng2 is None:
        return False
    return haversine_m(lat1, lng1, lat2, lng2) < radius_m
