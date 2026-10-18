"""TGOS adoption rules 2 (only the neighbourhood differs) and 4 (query has no street), synthetic base."""

import csv
import json
import math

import pyarrow as pa
import pyarrow.parquet as pq
from lvr_pipeline.addresses.identity import building_key
from lvr_pipeline.results.reconcile import EARTH_RADIUS_M, haversine_m
from lvr_pipeline.results.review import TGOS_REJECTION_CODES
from lvr_pipeline.tgos.adopt import NEIGHBORHOOD_OUTSIDE_REASON, BaseGroups
from lvr_pipeline.tgos.validate import apply_response
from lvr_pipeline.transactions.normalize import normalize_address

ORIGIN = (120.2, 23.5)
ROAD = "彰化縣彰化市南瑤里{}鄰天祥路30巷1號"
WRITES_NO_NEIGHBORHOOD = "彰化縣彰化市南瑤里天祥路30巷1號"
DIFFERS = "TGOS response address differs from submitted address"
MULTI = "TGOS neighbourhood differs and the base group has several locations"
VILLAGE_DOOR_MULTI = "TGOS village and door have several locations in the base"
HAMLET = "嘉義縣大埔鄉大埔村{}"


def north(metres: float, base=ORIGIN):
    """A point ``metres`` north of ``base`` (WGS84 sphere of the repository)."""
    return base[0], base[1] + math.degrees(metres / EARTH_RADIUS_M)


def exactly_30_m():
    """The northward offset whose rounded haversine distance is exactly 30.0 m."""
    step = math.degrees(30.0 / EARTH_RADIUS_M)
    for k in range(-4000, 4000):
        point = (ORIGIN[0], ORIGIN[1] + step * (1 + k * 1e-9))
        if haversine_m(ORIGIN, point) == 30.0:
            return point
    raise AssertionError("no point at exactly 30.0 m")


def key_of(address):
    return building_key(normalize_address(address))


def write_index(tmp_path, entries):
    """An offline-index folder with the rows (address, point, validity); a None address is a null key."""
    folder = tmp_path / "index"
    folder.mkdir()
    keys = [key_of(a) if a else None for a, _, _ in entries]
    counties = [json.loads(k[3:])["county"] if k else "" for k in keys]
    table = pa.table(
        {
            "building_key": keys,
            "county_code": counties,
            "lng": [p[0] for _, p, _ in entries],
            "lat": [p[1] for _, p, _ in entries],
            "validity": [v for _, _, v in entries],
        }
    )
    pq.write_table(table, folder / "offline_rows.parquet")
    return folder


def run(tmp_path, query, answer, entries):
    """apply_response for one query; returns the result row and the evidence rows."""
    index = write_index(tmp_path, [(a, p, v[0] if v else "valid") for a, p, *v in entries])
    path = tmp_path / "reply.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["Address", "Response_Address", "Response_X", "Response_Y"])
        writer.writeheader()
        writer.writerow({"Address": query, "Response_Address": answer, "Response_X": "120.31", "Response_Y": "23.61"})
    key = key_of(query)
    queries = [
        {
            "address": query, "batch_id": "b", "query_kind": "building_key", "building_key": key,
            "query_fingerprint": "fp", "county_code": json.loads(key[3:])["county"], "status": "submitted",
            "result_id": None,
        }
    ]
    batches = [{"batch_id": "b", "status": "submitted", "address_count": 1, "response_sha256": None}]
    results, evidence = [], []
    base = BaseGroups(index)
    apply_response(batches, queries, results, evidence, [], "b", path, base=base)
    return results[0], evidence, queries[0]


def code_of(result):
    assert result["status"] == "rejected"
    return TGOS_REJECTION_CODES[result["reason"]]


def test_single_location_group_adopts_and_keeps_the_query_key(tmp_path):
    entries = [(ROAD.format(15), ORIGIN), (ROAD.format(17), ORIGIN)]
    result, evidence, query = run(tmp_path, ROAD.format(15), ROAD.format(16), entries)
    assert result["status"] == "succeeded" and result["reason"] == NEIGHBORHOOD_OUTSIDE_REASON
    assert (result["lng"], result["lat"]) == (120.31, 23.61)
    assert evidence[0]["building_key"] == key_of(ROAD.format(15)) == query["building_key"]


def test_response_neighbourhood_inside_the_group_has_no_reason(tmp_path):
    entries = [(ROAD.format(15), ORIGIN), (ROAD.format(16), north(10))]
    result, _, _ = run(tmp_path, ROAD.format(15), ROAD.format(16), entries)
    assert result["status"] == "succeeded" and result["reason"] is None


def test_response_without_neighbourhood_is_outside_the_group(tmp_path):
    result, _, _ = run(tmp_path, ROAD.format(15), WRITES_NO_NEIGHBORHOOD, [(ROAD.format(15), ORIGIN)])
    assert result["status"] == "succeeded" and result["reason"] == NEIGHBORHOOD_OUTSIDE_REASON


def test_far_apart_neighbourhoods_are_a_multi_location_group(tmp_path):
    entries = [(ROAD.format(15), ORIGIN), (ROAD.format(16), north(100))]
    result, evidence, query = run(tmp_path, ROAD.format(15), ROAD.format(16), entries)
    assert result["reason"] == MULTI and code_of(result) == "tgos_neighborhood_multi_location"
    assert not evidence and query["status"] == "rejected"


def test_thirty_metres_is_within_and_just_over_is_not(tmp_path):
    exact = exactly_30_m()
    assert haversine_m(ORIGIN, exact) == 30.0
    result, _, _ = run(tmp_path, ROAD.format(15), ROAD.format(16), [(ROAD.format(15), ORIGIN), (ROAD.format(16), exact)])
    assert result["status"] == "succeeded"
    over = north(30.1)
    assert haversine_m(ORIGIN, over) > 30.0
    other = tmp_path / "over"
    other.mkdir()
    result, _, _ = run(other, ROAD.format(15), ROAD.format(16), [(ROAD.format(15), ORIGIN), (ROAD.format(16), over)])
    assert code_of(result) == "tgos_neighborhood_multi_location"


def test_neighbourhood_with_several_points_is_represented_by_its_medoid(tmp_path):
    # Neighbourhood 15 has three points; the medoid is 5 m north, 10 m from neighbourhood 16.
    entries = [
        (ROAD.format(15), ORIGIN), (ROAD.format(15), north(5)), (ROAD.format(15), north(200)),
        (ROAD.format(16), north(15)),
    ]
    result, _, _ = run(tmp_path, ROAD.format(15), ROAD.format(16), entries)
    assert result["status"] == "succeeded" and result["reason"] is None


def test_group_not_in_the_base_is_isolated(tmp_path):
    entries = [("彰化縣彰化市南瑤里15鄰天祥路30巷2號", ORIGIN)]
    result, _, _ = run(tmp_path, ROAD.format(15), ROAD.format(16), entries)
    assert result["reason"] == DIFFERS and code_of(result) == "tgos_isolated"


def test_other_village_and_invalid_rows_do_not_count(tmp_path):
    entries = [
        (ROAD.format(15), ORIGIN),
        ("彰化縣彰化市延平里16鄰天祥路30巷1號", north(500)),
        (ROAD.format(16), north(500), "invalid"),
        (None, north(500)),
    ]
    result, _, _ = run(tmp_path, ROAD.format(15), ROAD.format(16), entries)
    assert result["status"] == "succeeded" and result["reason"] == NEIGHBORHOOD_OUTSIDE_REASON


def test_village_proven_by_code_and_only_the_neighbourhood_differs(tmp_path):
    result, _, _ = run(
        tmp_path, ROAD.format(15), "彰化縣彰化市南瑶里16鄰天祥路30巷1號", [(ROAD.format(15), ORIGIN)]
    )
    assert result["status"] == "succeeded" and result["reason"] == NEIGHBORHOOD_OUTSIDE_REASON


def test_another_door_in_the_response_is_not_rule_2(tmp_path):
    result, _, _ = run(tmp_path, ROAD.format(15), "彰化縣彰化市南瑤里16鄰天祥路30巷2號", [(ROAD.format(15), ORIGIN)])
    assert result["reason"] == DIFFERS


def test_query_without_a_neighbourhood_never_uses_rule_2(tmp_path):
    result, _, _ = run(tmp_path, WRITES_NO_NEIGHBORHOOD, ROAD.format(16), [(ROAD.format(15), ORIGIN)])
    assert result["status"] == "succeeded" and result["reason"] is None


# Rule 4: the query has a village and a door but no street.
QUERY_4 = HAMLET.format("278號")


def test_village_and_door_single_location_adopts(tmp_path):
    entries = [(HAMLET.format("13鄰大埔278號"), ORIGIN), (HAMLET.format("12鄰中山路278號"), north(20))]
    result, evidence, query = run(tmp_path, QUERY_4, HAMLET.format("13鄰大埔278號"), entries)
    assert result["status"] == "succeeded" and result["reason"] is None
    assert evidence[0]["building_key"] == key_of(QUERY_4)


def test_village_and_door_door_notation_applies(tmp_path):
    query = HAMLET.format("278之1號")
    result, _, _ = run(tmp_path, query, HAMLET.format("13鄰大埔278號之1"), [(HAMLET.format("13鄰大埔278號之1"), ORIGIN)])
    assert result["status"] == "succeeded"


def test_village_and_door_multi_location_is_rejected(tmp_path):
    entries = [(HAMLET.format("13鄰大埔278號"), ORIGIN), (HAMLET.format("12鄰中山路278號"), north(100))]
    result, _, _ = run(tmp_path, QUERY_4, HAMLET.format("13鄰大埔278號"), entries)
    assert result["reason"] == VILLAGE_DOOR_MULTI and code_of(result) == "tgos_village_door_multi_location"


def test_village_and_door_not_in_the_base_is_isolated(tmp_path):
    entries = [(HAMLET.format("13鄰大埔279號"), ORIGIN)]
    result, _, _ = run(tmp_path, QUERY_4, HAMLET.format("13鄰大埔278號"), entries)
    assert result["reason"] == DIFFERS and code_of(result) == "tgos_isolated"


def test_village_and_door_with_another_door_in_the_response_is_isolated(tmp_path):
    entries = [(HAMLET.format("13鄰大埔278號"), ORIGIN)]
    result, _, _ = run(tmp_path, QUERY_4, HAMLET.format("13鄰大埔279號"), entries)
    assert result["reason"] == DIFFERS


def test_village_and_door_with_another_village_in_the_response_is_isolated(tmp_path):
    entries = [(HAMLET.format("13鄰大埔278號"), ORIGIN)]
    result, _, _ = run(tmp_path, QUERY_4, "嘉義縣大埔鄉曾文村13鄰大埔278號", entries)
    assert result["reason"] == DIFFERS


def test_village_and_door_with_a_different_written_neighbourhood_is_isolated(tmp_path):
    query = HAMLET.format("15鄰278號")
    entries = [(HAMLET.format("13鄰大埔278號"), ORIGIN)]
    result, _, _ = run(tmp_path, query, HAMLET.format("13鄰大埔278號"), entries)
    assert result["reason"] == DIFFERS


def test_the_base_is_read_once_for_the_towns_it_needs(tmp_path):
    index = write_index(tmp_path, [(ROAD.format(15), ORIGIN, "valid")])
    base = BaseGroups(index)
    towns = {("10007", "彰化市")}
    base.load(towns)
    seconds = base.seconds
    base.load(towns)
    assert base.seconds == seconds and base.loaded == towns
    assert base.neighborhood_group(key_of(ROAD.format(15))).present
    assert not base.neighborhood_group(key_of("彰化縣彰化市南瑤里15鄰天祥路30巷9號")).present


def test_review_codes_are_registered():
    from lvr_pipeline.contracts.schemas import EXCEPTION_REASON_CODES

    assert {"tgos_neighborhood_multi_location", "tgos_village_door_multi_location"} <= set(EXCEPTION_REASON_CODES)
