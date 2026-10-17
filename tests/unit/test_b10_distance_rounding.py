"""B-10: every distance and distance sum is rounded to 0.001 m (half away from zero).

The rule covers the tolerance comparison and the medoid choice in Python
(``results.reconcile``) and the SQL haversine (``offline.index._haversine_sql``).
"""

import math
import random

import duckdb
import pytest

from lvr_pipeline.offline.index import EARTH_RADIUS_M, _haversine_sql
from lvr_pipeline.results.reconcile import (
    EXCEEDS_TOLERANCE,
    WITHIN_TOLERANCE,
    adopt_coordinates,
    haversine_m,
    round_distance_m,
)

BASE = (121.5, 25.0)


def raw_haversine_m(a, b):
    """The unrounded distance, same formula as ``haversine_m``."""
    lng_a, lat_a = a
    lng_b, lat_b = b
    h = math.sin(math.radians(lat_b - lat_a) / 2) ** 2 + math.cos(
        math.radians(lat_a)
    ) * math.cos(math.radians(lat_b)) * math.sin(math.radians(lng_b - lng_a) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))


def point_north_of_base(distance_m: float) -> tuple[float, float]:
    """A point at the given raw distance, moved in latitude only at lng 121.5."""
    delta_lat = distance_m / (EARTH_RADIUS_M * math.pi / 180)
    return (BASE[0], BASE[1] + delta_lat)


def assert_millimetre_multiple(value: float) -> None:
    assert abs(round(value * 1000) - value * 1000) <= 1e-9


def test_round_distance_m_half_away_from_zero():
    assert round_distance_m(30.0004) == 30.0
    assert round_distance_m(30.0006) == 30.001
    assert round_distance_m(0.0005) == 0.001
    assert round_distance_m(0.0) == 0.0


@pytest.mark.parametrize("raw", [30.0002, 30.0004])
def test_band_up_to_half_millimetre_above_tolerance_is_located(raw):
    point = point_north_of_base(raw)
    assert 30.0000 < raw_haversine_m(BASE, point) < 30.0005
    result = adopt_coordinates([BASE, point], tolerance_m=30.0)
    assert result["status"] == "located"
    assert result["resolution_basis"] == WITHIN_TOLERANCE
    assert result["max_distance_m"] == 30.0


@pytest.mark.parametrize("raw", [30.0006, 30.0009])
def test_band_above_half_millimetre_is_conflict(raw):
    point = point_north_of_base(raw)
    assert 30.0005 < raw_haversine_m(BASE, point) < 30.001
    result = adopt_coordinates([BASE, point], tolerance_m=30.0)
    assert result["status"] == "conflict"
    assert result["resolution_basis"] == EXCEEDS_TOLERANCE
    assert result["max_distance_m"] == 30.001


def test_haversine_m_is_rounded_to_millimetre():
    point = point_north_of_base(12.3456)
    value = haversine_m(BASE, point)
    assert value == round_distance_m(raw_haversine_m(BASE, point))
    assert_millimetre_multiple(value)


def test_haversine_sql_matches_python_on_random_pairs():
    rng = random.Random(20261018)
    pairs = []
    for pair_id in range(50):
        a = (BASE[0] + rng.uniform(-0.0004, 0.0004), BASE[1] + rng.uniform(-0.0004, 0.0004))
        b = (BASE[0] + rng.uniform(-0.0004, 0.0004), BASE[1] + rng.uniform(-0.0004, 0.0004))
        pairs.append((pair_id, a, b))

    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE left_pts (pair_id INTEGER, lng DOUBLE, lat DOUBLE)")
        con.execute("CREATE TABLE right_pts (pair_id INTEGER, lng DOUBLE, lat DOUBLE)")
        con.executemany(
            "INSERT INTO left_pts VALUES (?, ?, ?)",
            [(pid, a[0], a[1]) for pid, a, _ in pairs],
        )
        con.executemany(
            "INSERT INTO right_pts VALUES (?, ?, ?)",
            [(pid, b[0], b[1]) for pid, _, b in pairs],
        )
        rows = con.execute(
            f"SELECT a.pair_id, {_haversine_sql('a', 'b')} AS d "
            "FROM left_pts a JOIN right_pts b USING (pair_id) ORDER BY a.pair_id"
        ).fetchall()
    finally:
        con.close()

    assert len(rows) == 50
    for pid, sql_value in rows:
        _, a, b = pairs[pid]
        assert sql_value == haversine_m(a, b)
        assert_millimetre_multiple(sql_value)


def test_distance_sums_and_max_are_millimetre_multiples():
    points = [BASE, point_north_of_base(10.0004), point_north_of_base(17.0007), point_north_of_base(40.0003)]
    result = adopt_coordinates(points, tolerance_m=100.0)
    assert_millimetre_multiple(result["max_distance_m"])
    assert len(result["distance_sums"]) == 4
    for _, total in result["distance_sums"]:
        assert_millimetre_multiple(total)
