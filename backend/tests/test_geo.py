"""§10.1's distance test and the blocking grid that makes it cheap."""

from __future__ import annotations

import math

import pytest

from leadscraper.core.geo import (
    CELL_DEGREES,
    NEAR_DUPLICATE_METRES,
    grid_cell,
    haversine_m,
    is_near,
    neighbouring_cells,
)

# Two real Islamabad salons from the live run, 171 m apart — House of Salons'
# F-7 Female Studio and its F-7 men's branch. They are the closest pair in the
# whole run that shares a phone number, and they are two different businesses.
F7_FEMALE = (33.7167, 73.0552)
F7_MENS = (33.7182, 73.0554)


def test_haversine_matches_a_known_distance() -> None:
    # Islamabad to Lahore, ~268 km.
    metres = haversine_m(33.6844, 73.0479, 31.5204, 74.3587)
    assert 265_000 < metres < 272_000


def test_haversine_is_zero_for_a_point_against_itself() -> None:
    assert haversine_m(*F7_FEMALE, *F7_FEMALE) == pytest.approx(0.0, abs=1e-6)


def test_haversine_is_symmetric() -> None:
    assert haversine_m(*F7_FEMALE, *F7_MENS) == pytest.approx(
        haversine_m(*F7_MENS, *F7_FEMALE)
    )


def test_missing_coordinates_are_never_near() -> None:
    """Missing stays missing: a business with no coordinates has not been shown
    to be near anything, and §10.1's fuzzy tier requires the distance test."""
    assert not is_near(None, None, 33.7, 73.0)
    assert not is_near(33.7, 73.0, None, None)
    assert not is_near(None, None, None, None)


def test_the_default_radius_is_the_one_10_1_specifies() -> None:
    assert NEAR_DUPLICATE_METRES == 150.0


# --------------------------------------------------------------------------- #
# Grid blocking must not lose a pair
# --------------------------------------------------------------------------- #


def test_a_cell_is_wider_than_the_match_radius_in_both_axes() -> None:
    """The correctness condition for blocking. If a cell were narrower than the
    match radius, two businesses 150 m apart could land two cells apart and never
    be compared — a silent miss, which is the §5.5 failure mode.

    Longitude is the tight axis because degrees of longitude shrink with
    latitude; 37°N is beyond the northernmost §3.1 city.
    """
    metres_per_degree_lat = 111_320
    assert CELL_DEGREES * metres_per_degree_lat > NEAR_DUPLICATE_METRES
    worst_case_lng = CELL_DEGREES * metres_per_degree_lat * math.cos(math.radians(37))
    assert worst_case_lng > NEAR_DUPLICATE_METRES


def test_neighbouring_cells_is_the_full_three_by_three() -> None:
    cells = neighbouring_cells((10, 20))
    assert len(cells) == 9
    assert (10, 20) in cells
    assert (9, 19) in cells and (11, 21) in cells


@pytest.mark.parametrize("bearing_degrees", range(0, 360, 15))
def test_any_pair_inside_the_radius_shares_a_neighbourhood(bearing_degrees: int) -> None:
    """Walk 149 m out from a fixed point in every direction and check the grid
    still brings the pair together."""
    lat, lng = F7_FEMALE
    distance = NEAR_DUPLICATE_METRES - 1
    bearing = math.radians(bearing_degrees)
    d_lat = (distance * math.cos(bearing)) / 111_320
    d_lng = (distance * math.sin(bearing)) / (111_320 * math.cos(math.radians(lat)))
    other = (lat + d_lat, lng + d_lng)

    assert is_near(lat, lng, *other)
    assert grid_cell(*other) in neighbouring_cells(grid_cell(lat, lng))


def test_the_closest_shared_phone_pair_in_the_live_run_is_still_too_far() -> None:
    """House of Salons F-7 (Female Studio) and F-7 (Men's Salon) publish the same
    seven numbers and sit 171 m apart. They are separate branches with separate
    addresses, and §10.1's radius is what keeps them separate rows."""
    assert haversine_m(*F7_FEMALE, *F7_MENS) > NEAR_DUPLICATE_METRES
    assert not is_near(*F7_FEMALE, *F7_MENS)
