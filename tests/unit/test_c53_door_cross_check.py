"""c53: the door cross-check counts only coordinates that carry a door (docs/地址規則.md). Synthetic data only."""

import json
import math

import pytest

from lvr_pipeline.addresses.identity import building_key
from lvr_pipeline.contracts.validate import validate_dataset_rows
from lvr_pipeline.offline.index import EARTH_RADIUS_M
from lvr_pipeline.results.reconcile import (
    DOOR_CROSS_CHECK,
    DOOR_RULE,
    EXCEEDS_TOLERANCE,
    WITHIN_TOLERANCE,
    is_door_cross_check,
    resolution_row,
)
from lvr_pipeline.tgos import _rebuild_resolution

T = "臺北市中正區"
P = (121.5, 25.0)
K = building_key(T + "測試路17號")
LAT_DEGREE_PER_M = 180 / (math.pi * EARTH_RADIUS_M)


def north(point, metres):
    return point[0], point[1] + metres * LAT_DEGREE_PER_M


def point(eid, doors):
    return {"evidence_count": max(1, len(doors)), "evidence_id": eid, "doors": doors}


def test_rule_name_is_bumped():
    assert DOOR_RULE == "village_neighborhood_subid_v2"


@pytest.mark.parametrize(
    ("door_lists", "cross"),
    [
        # Two doors on one coordinate and a TGOS coordinate without a door: not a cross-check.
        ([[("甲里", "9"), ("乙里", "5")], []], False),
        # Two doors on two coordinates: a cross-check, with or without a TGOS coordinate.
        ([[("甲里", "1")], [("乙里", "2")]], True),
        ([[("甲里", "1")], [("乙里", "2")], []], True),
        # One door on two coordinates: the tolerance rule.
        ([[("甲里", "1")], [("甲里", "1")], []], False),
        # Two doors on one coordinate and one of them again elsewhere: a cross-check.
        ([[("甲里", "1"), ("甲里", "2")], [("甲里", "1")]], True),
    ],
)
def test_is_door_cross_check(door_lists, cross):
    assert is_door_cross_check(door_lists) is cross


def test_tgos_point_near_a_shared_coordinate_is_located():
    # e17 case: two neighbourhoods share one coordinate; TGOS answers 0.13 m away with six decimals.
    offline, tgos = (120.691420831, 24.17922817), (120.691421, 24.179227)
    row = resolution_row("v2", K, {offline: point("a", [("松和里", "9"), ("松安里", "5")]),
                                        tgos: point("t", [])}, 30.0)
    assert row["status"] == "located" and row["resolution_basis"] == WITHIN_TOLERANCE
    assert (row["lng"], row["lat"]) == offline and row["evidence_id"] == "a"
    validate_dataset_rows([row], "coordinate-resolution")


def test_tgos_point_far_from_a_shared_coordinate_stays_a_conflict():
    row = resolution_row("v2", K, {P: point("a", [("甲里", "1"), ("甲里", "2")]),
                                        north(P, 40): point("t", [])}, 30.0)
    assert row["status"] == "conflict" and row["resolution_basis"] == EXCEEDS_TOLERANCE
    validate_dataset_rows([row], "coordinate-resolution")


def test_two_doors_on_two_coordinates_stay_a_cross_check():
    row = resolution_row("v2", K, {P: point("a", [("甲里", "1")]), north(P, 10): point("b", [("甲里", "2")]),
                                        north(P, 5): point("t", [])}, 30.0)
    assert row["status"] == "conflict" and row["resolution_basis"] == DOOR_CROSS_CHECK
    validate_dataset_rows([row], "coordinate-resolution")


def test_validator_rejects_the_old_cross_check_on_a_shared_coordinate():
    row = resolution_row("v2", K, {P: point("a", [("甲里", "1"), ("甲里", "2")]),
                                        north(P, 1): point("t", [])}, 30.0)
    old = {**row, "status": "conflict", "resolution_basis": DOOR_CROSS_CHECK, "lng": None, "lat": None,
           "evidence_id": None}
    with pytest.raises(ValueError, match="adoption rule"):
        validate_dataset_rows([old], "coordinate-resolution")
    assert json.loads(row["coordinates_json"])[1]["doors"] == []


def test_rebuild_with_tgos_evidence_locates_the_shared_door():
    k = building_key(T + "測試路17號")
    evidence = [
        {"evidence_id": "a", "building_key": k, "lng": P[0], "lat": P[1], "validity": "valid",
         "source_kind": "legacy_base", "normalized_address": T + "甲里9鄰測試路17號"},
        {"evidence_id": "b", "building_key": k, "lng": P[0], "lat": P[1], "validity": "valid",
         "source_kind": "legacy_base", "normalized_address": T + "乙里5鄰測試路17號"},
        {"evidence_id": "t", "building_key": k, "lng": round(P[0] + 1e-6, 6), "lat": P[1], "validity": "valid",
         "source_kind": "tgos_result", "normalized_address": T + "測試路17號"},
    ]
    pool = [{"key_version": "v2", "building_key": k, "canonical_address": T + "測試路17號", "county_code": "63",
             "town_code": "6300100", "address_family": "f"}]
    address_rows, _, statuses, resolutions = _rebuild_resolution(pool, evidence, "c", 30.0, DOOR_RULE)
    assert address_rows[0]["status"] == "located" and statuses["located"] == 1
    assert resolutions[0]["resolution_basis"] == WITHIN_TOLERANCE
