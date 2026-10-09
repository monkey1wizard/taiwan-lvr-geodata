"""R05-10: the offline index rejects coordinates outside the WGS84 Taiwan bounds."""

import json
import math

import pytest

from lvr_pipeline import tgos
from lvr_pipeline.contracts.validate import (
    TAIWAN_BOUNDS,
    TAIWAN_BOUNDS_RULE,
    within_taiwan_bounds,
)
from lvr_pipeline.offline import index as offline_index
from lvr_pipeline.offline.index import build_index
from lvr_pipeline.storage.parquet import rows
from test_p2_offline import source

WEST, EAST, SOUTH, NORTH = 118.0, 123.5, 21.5, 26.5


def test_bounds_are_shared_with_tgos_and_unchanged():
    assert TAIWAN_BOUNDS == (WEST, EAST, SOUTH, NORTH)
    assert tgos.TAIWAN_BOUNDS is TAIWAN_BOUNDS


@pytest.mark.parametrize(
    "lng, lat",
    [(121.5, 25.0), (WEST, 23.0), (EAST, 23.0), (121.0, SOUTH), (121.0, NORTH), (WEST, SOUTH), (EAST, NORTH)],
)
def test_inside_and_boundary_values_are_valid(lng, lat):
    assert within_taiwan_bounds(lng, lat)
    assert tgos._tgos_coordinate(str(lng), str(lat)) == (lng, lat)


@pytest.mark.parametrize(
    "lng, lat",
    [
        (118.75456607061, 0.0),
        (0.0, 25.0),
        (0.0, 0.0),
        (WEST - 1e-9, 23.0),
        (EAST + 1e-9, 23.0),
        (121.0, SOUTH - 1e-9),
        (121.0, NORTH + 1e-9),
        (-158.596091324892, 62.2492665105295),
        (math.nan, 25.0),
        (121.5, math.inf),
    ],
)
def test_outside_values_are_invalid(lng, lat):
    assert not within_taiwan_bounds(lng, lat)
    with pytest.raises(ValueError, match="outside declared WGS84 Taiwan bounds"):
        tgos._tgos_coordinate(str(lng), str(lat))


def test_index_marks_outside_rows_invalid_coordinate(tmp_path):
    coordinates = [
        ("臺北市中正區測試路1號", "63", "6300100", 118.75456607061, 0.0),
        ("臺北市中正區測試路2號", "63", "6300100", 0.0, 25.0),
        ("臺北市中正區測試路3號", "63", "6300100", WEST, SOUTH),
        ("臺北市中正區測試路4號", "63", "6300100", EAST, NORTH),
        ("臺北市中正區測試路5號", "63", "6300100", 121.5, 25.0),
        ("臺北市中正區測試路6號", "63", "6300100", 121.5, NORTH + 0.001),
    ]
    root, descriptor = source(tmp_path, coordinates)
    index = build_index(root, descriptor, tmp_path / "work")
    values = sorted(rows(index / "offline_rows.parquet"), key=lambda r: r["source_row_number"])
    assert [r["validity"] for r in values] == [
        "invalid_coordinate",
        "invalid_coordinate",
        "valid",
        "valid",
        "valid",
        "invalid_coordinate",
    ]
    for row, (*_, lng, lat) in zip(values, coordinates):
        if row["validity"] == "valid":
            assert (row["lng"], row["lat"]) == (lng, lat)
        else:
            # Coordinate fields stay empty; the source row is kept by reference.
            assert row["lng"] is None and row["lat"] is None
            assert row["building_key"] is not None and row["source_ref"] == "roads/63-測試路.csv"
    quality = json.loads((index / "quality.json").read_text(encoding="utf-8"))
    assert quality["valid_rows"] == 3 and quality["invalid_rows"] == 3
    parameters = quality["producer_config"]["parameters"]
    assert parameters["coordinate_bounds_rule"] == TAIWAN_BOUNDS_RULE == "taiwan_bounds_v1"
    assert parameters["coordinate_bounds"] == [WEST, EAST, SOUTH, NORTH]


def test_bounds_rule_is_bound_into_index(tmp_path, monkeypatch):
    root, descriptor = source(tmp_path)
    work = tmp_path / "work"
    first = build_index(root, descriptor, work)
    assert build_index(root, descriptor, work) == first
    monkeypatch.setattr(offline_index, "TAIWAN_BOUNDS_RULE", "taiwan_bounds_test")
    assert build_index(root, descriptor, work) != first
