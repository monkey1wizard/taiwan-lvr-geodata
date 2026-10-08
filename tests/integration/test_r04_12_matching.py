"""R04-12: matching transactions to doors with village and neighbourhood sub-identifiers.

Owner decisions of 2026-10-09: rows that differ only in village or neighbourhood are different
doors. A transaction that writes a village or neighbourhood only matches doors with the same
value. Without them, candidates with one coordinate are located; candidates from several doors
with different coordinates are not located and go to the manual cross-check list. The 30 m rule
applies only within one door. Synthetic data only.
"""

import csv
import json
import math

import pytest

from lvr_pipeline.addresses.identity import building_key_v2
from lvr_pipeline.contracts.validate import validate_dataset_rows
from lvr_pipeline.offline.index import EARTH_RADIUS_M
from lvr_pipeline.results.reconcile import (
    DOOR_CROSS_CHECK,
    DOOR_RULE,
    WITHIN_TOLERANCE,
    load_p2,
    projected_evidence_id,
    resolution_row,
)
from lvr_pipeline.results.review import CROSS_CHECK_CODES, R04_12_CODES, build_review
from lvr_pipeline.storage.parquet import rows
from lvr_pipeline.tgos import _rebuild_resolution, import_tgos, prepare_tgos, transition_batch
from test_p2_offline import run

T = "臺北市中正區"
LAT_DEGREE_PER_M = 180 / (math.pi * EARTH_RADIUS_M)
P1, P2, P3, P4 = (121.50, 25.00), (121.52, 25.02), (121.53, 25.03), (121.54, 25.04)


def north(point, metres):
    return point[0], point[1] + metres * LAT_DEGREE_PER_M


SOURCE = [
    # Door 20: two villages far apart.
    (T + "甲里1鄰測試路20號", P1),
    (T + "乙里2鄰測試路20號二樓", P2),
    # Door 21: one village, two neighbourhoods, the same coordinate.
    (T + "甲里1鄰測試路21號", P3),
    (T + "甲里3鄰測試路21號", P3),
    # Door 22: one door, two coordinates 10 m apart.
    (T + "甲里1鄰測試路22號", P4),
    (T + "甲里1鄰測試路22號三樓", north(P4, 10)),
    # Door 23: two neighbourhoods 10 m apart; 30 m is not applied between doors.
    (T + "甲里1鄰測試路23號", P1),
    (T + "甲里2鄰測試路23號", north(P1, 10)),
]
TRADES = {
    "cross": T + "測試路20號",
    "village_a": T + "甲里測試路20號",
    "full_b": T + "乙里2鄰測試路20號",
    "other_village": T + "丙里測試路20號",
    "other_lin": T + "丙里3鄰測試路20號",
    "same_point": T + "測試路21號",
    "one_door": T + "測試路22號",
    "near_doors": T + "測試路23號",
    "lin_only": T + "2鄰測試路23號",
    "annex_before": T + "測試路30附1號",
    "annex_after": T + "測試路30號附1",
    "lane_cjk": T + "測試路十二巷5號",
    "lane_arabic": T + "測試路12巷5號",
    "lane_named": T + "測試路豐二巷52號",
    "lane_plain": T + "測試路52號",
    "note": T + "測試路40號(店面)",
    "garbled": T + "測" + chr(0xE001) + "路21號",  # U+E001 has no character rule
}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("r04-12")
    coordinates = [(address, "63", "6300100", *point) for address, point in SOURCE]
    records = [{"address": value} for value in TRADES.values()]
    converted, root, descriptor, index, pool, state = run(
        tmp_path, coordinates, records, categories=("sales",)
    )
    return {"tmp": tmp_path, "converted": converted, "root": root, "descriptor": descriptor,
            "index": index, "pool": pool, "state": state}


def results(state):
    return {row["canonical_address"]: row for row in rows(state / "address_index.parquet")}


def key(name):
    return building_key_v2(TRADES[name])


def by_key(state):
    return {row["building_key"]: row for row in rows(state / "address_index.parquet")}


def resolutions(state):
    return {row["building_key"]: row for row in rows(state / "coordinate_resolutions.parquet")}


def test_state_verifies_and_records_the_door_rule(built):
    _, report = load_p2(built["state"], "offline-state")
    assert report["door_rule"] == DOOR_RULE
    assert report["door_cross_check_keys"] == 2


def test_village_and_neighbourhood_filter_candidates(built):
    state = by_key(built["state"])
    assert (state[key("village_a")]["status"], state[key("village_a")]["lng"]) == ("located", P1[0])
    assert (state[key("full_b")]["status"], state[key("full_b")]["lat"]) == ("located", P2[1])
    assert state[key("other_village")]["status"] == "unmatched"
    assert state[key("lin_only")]["status"] == "located"
    assert (state[key("lin_only")]["lng"], state[key("lin_only")]["lat"]) == north(P1, 10)


def test_several_doors_with_one_coordinate_are_located(built):
    row = by_key(built["state"])[key("same_point")]
    assert row["status"] == "located" and (row["lng"], row["lat"]) == P3
    assert row["coordinate_count"] == 1 and row["evidence_count"] == 2


def test_thirty_metre_rule_applies_within_one_door(built):
    row = by_key(built["state"])[key("one_door")]
    detail = resolutions(built["state"])[key("one_door")]
    assert row["status"] == "located" and detail["resolution_basis"] == WITHIN_TOLERANCE


@pytest.mark.parametrize("name", ["cross", "near_doors"])
def test_several_doors_with_different_coordinates_need_cross_check(built, name):
    row = by_key(built["state"])[key(name)]
    detail = resolutions(built["state"])[key(name)]
    assert row["status"] == "conflict" and row["lng"] is None and row["evidence_id"] is None
    assert detail["resolution_basis"] == DOOR_CROSS_CHECK
    doors = [door for item in json.loads(detail["coordinates_json"]) for door in item["doors"]]
    assert len({(d["village"], d["neighborhood"]) for d in doors}) == 2
    if name == "near_doors":
        # The two doors are within 30 m, but the tolerance is not applied between doors.
        assert detail["max_distance_m"] < 30


def test_projected_evidence_keeps_ids_unique_and_traceable(built):
    index_rows = {(r["source_ref"], r["source_row_number"]): r for r in rows(built["index"] / "offline_rows.parquet")}
    evidence = list(rows(built["state"] / "address_observations.parquet"))
    assert len({row["evidence_id"] for row in evidence}) == len(evidence)
    # The 甲里1鄰 door-20 row supports three pool keys.
    source = index_rows[("roads/63-測試路.csv", 2)]
    projected = [row for row in evidence if (row["source_ref"], row["source_row_number"]) == ("roads/63-測試路.csv", 2)]
    assert {row["building_key"] for row in projected} == {key("cross"), key("village_a")}
    for row in projected:
        assert row["evidence_id"] == projected_evidence_id(source["evidence_id"], row["building_key"])


def test_resolution_validator_recomputes_door_cross_check(built):
    detail = resolutions(built["state"])[key("near_doors")]
    validate_dataset_rows([detail], "coordinate-resolution")
    listed = json.loads(detail["coordinates_json"])
    for item in listed:
        item.pop("doors")
    with pytest.raises(ValueError):
        validate_dataset_rows([{**detail, "coordinates_json": json.dumps(listed)}], "coordinate-resolution")
    one_door = json.loads(detail["coordinates_json"])
    for item in one_door:
        item["doors"] = [{"village": "甲里", "neighborhood": "1"}]
    with pytest.raises(ValueError, match="adoption rule"):
        validate_dataset_rows([{**detail, "coordinates_json": json.dumps(one_door)}], "coordinate-resolution")


def test_resolution_row_without_doors_keeps_r05_4_rule():
    a, b = (121.5, 25.0), north((121.5, 25.0), 10)
    plain = resolution_row("v2", "v2:k", {a: {"evidence_count": 1, "evidence_id": "x"},
                                          b: {"evidence_count": 1, "evidence_id": "y"}}, 30.0)
    assert plain["status"] == "located" and "doors" not in plain["coordinates_json"]
    doors = resolution_row("v2", "v2:k", {a: {"evidence_count": 1, "evidence_id": "x", "doors": [("甲里", "1")]},
                                          b: {"evidence_count": 1, "evidence_id": "y", "doors": [("甲里", "2")]}}, 30.0)
    assert doors["status"] == "conflict" and doors["resolution_basis"] == DOOR_CROSS_CHECK


def test_garbled_probe_uses_door_matching(built):
    applied = [row for row in rows(built["pool"] / "address_occurrences.parquet")
               if row["reason"] == "coordinate_confirmed_candidate"]
    assert [row["building_key"] for row in applied] == [key("same_point")]


def test_rebuild_resolution_door_rule_and_legacy_states():
    k = building_key_v2(T + "測試路23號")
    evidence = [
        {"evidence_id": "a", "building_key": k, "lng": P1[0], "lat": P1[1], "validity": "valid",
         "source_kind": "legacy_base", "normalized_address": T + "甲里1鄰測試路23號"},
        {"evidence_id": "b", "building_key": k, "lng": north(P1, 10)[0], "lat": north(P1, 10)[1],
         "validity": "valid", "source_kind": "legacy_base", "normalized_address": T + "甲里2鄰測試路23號"},
    ]
    pool = [{"key_version": "v2", "building_key": k, "canonical_address": T + "測試路23號", "county_code": "63",
             "town_code": "6300100", "address_family": "f"}]
    old = _rebuild_resolution(pool, evidence, "c", 30.0)
    assert old[0][0]["status"] == "located"
    new = _rebuild_resolution(pool, evidence, "c", 30.0, DOOR_RULE)
    assert new[0][0]["status"] == "conflict" and new[3][0]["resolution_basis"] == DOOR_CROSS_CHECK


@pytest.fixture(scope="module")
def reviewed(built):
    tmp_path = built["tmp"]
    prepared, _ = prepare_tgos(built["state"], tmp_path / "work", tmp_path / "exchange", limit=100,
                               exchange_date="2026-10-09")
    queries = list(rows(prepared / "tgos_queries.parquet"))
    batch_id = queries[0]["batch_id"]
    submitted = transition_batch(prepared, tmp_path / "work", batch_id, "submitted")
    answers = {
        # The response adds the neighbourhood: the same door.
        TRADES["other_village"]: (T + "丙里5鄰測試路20號", "121.55", "25.05"),
        # The response names another neighbourhood: not the queried door.
        TRADES["other_lin"]: (T + "丙里4鄰測試路20號", "121.56", "25.06"),
    }
    response = tmp_path / "response.csv"
    with response.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["Address", "Response_Address", "Response_X", "Response_Y"])
        writer.writeheader()
        for query in queries:
            answer, x, y = answers.get(query["address"], ("", "", ""))
            writer.writerow({"Address": query["address"], "Response_Address": answer,
                             "Response_X": x, "Response_Y": y})
    tgos_state = import_tgos(submitted, tmp_path / "work", batch_id, response)
    review = build_review(built["converted"], built["state"], tmp_path / "review-work",
                          descriptor=built["descriptor"], tgos_state=tgos_state, run_id="r04-12")
    return {"queries": queries, "tgos": tgos_state, "review": review,
            "rows": list(rows(review / "exception_addresses.parquet")),
            "cross": list(rows(review / "cross_check_addresses.parquet"))}


def test_tgos_accepts_added_neighbourhood_only_for_the_same_door(built, reviewed):
    state = by_key(reviewed["tgos"])
    assert state[key("other_village")]["status"] == "located"
    assert state[key("other_lin")]["status"] == "unmatched"
    result = [row for row in rows(reviewed["tgos"] / "tgos_imports.parquet")
              if row["address"] == TRADES["other_lin"]]
    assert [row["reason"] for row in result] == ["TGOS response address differs from submitted address"]
    _, report = load_p2(reviewed["tgos"], "tgos-state")
    assert report["door_rule"] == DOOR_RULE and report["door_cross_check_keys"] == 2
    tgos_resolutions = resolutions(reviewed["tgos"])
    assert tgos_resolutions[key("cross")]["resolution_basis"] == DOOR_CROSS_CHECK


def test_cross_check_list_is_a_separate_file(reviewed):
    assert {row["reason_code"] for row in reviewed["cross"]} == set(CROSS_CHECK_CODES)
    assert {row["raw_address"] for row in reviewed["cross"]} == {TRADES["cross"], TRADES["near_doors"]}
    assert not any(row["reason_code"] in CROSS_CHECK_CODES for row in reviewed["rows"])
    conflicts = {row["raw_address"] for row in reviewed["rows"] if row["reason_code"] == "coordinate_conflict"}
    assert conflicts.isdisjoint({TRADES["cross"], TRADES["near_doors"]})
    related = json.loads(reviewed["cross"][0]["related_json"])
    assert all("doors" in item for item in related["coordinates"])
    csv_path = reviewed["review"] / "cross_check_addresses.csv"
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    _, report = load_p2(reviewed["review"], "review")
    assert report["cross_check_rows"] == len(reviewed["cross"]) == 2
    assert report["dataset_counts"]["exception-address"] == len(reviewed["rows"]) + len(reviewed["cross"])


@pytest.mark.parametrize("code,pair", [
    ("annex_variant_pair", ("annex_before", "annex_after")),
    ("lane_numeral_variant_pair", ("lane_cjk", "lane_arabic")),
    ("named_lane_variant_pair", ("lane_named", "lane_plain")),
])
def test_variant_pairs_point_at_each_other(reviewed, code, pair):
    found = [row for row in reviewed["rows"] if row["reason_code"] == code]
    addresses = {row["raw_address"]: json.loads(row["related_json"])["pair_address"] for row in found}
    a, b = (TRADES[name] for name in pair)
    assert addresses == {a: b, b: a}
    assert not any(row["reason_code"] == "subdoor_variant_pair" for row in reviewed["rows"]
                   if row["raw_address"] in (a, b))


def test_bracket_note_is_listed_and_ignored_by_the_key(reviewed):
    found = [row for row in reviewed["rows"] if row["reason_code"] == "bracket_note"]
    assert [row["raw_address"] for row in found] == [TRADES["note"]]
    assert json.loads(found[0]["related_json"]) == {"notes": ["(店面)"]}
    assert found[0]["building_key"] == building_key_v2(T + "測試路40號")


def test_every_r04_12_code_is_produced(reviewed):
    found = {row["reason_code"] for row in reviewed["rows"] + reviewed["cross"]}
    assert set(R04_12_CODES) | set(CROSS_CHECK_CODES) <= found
