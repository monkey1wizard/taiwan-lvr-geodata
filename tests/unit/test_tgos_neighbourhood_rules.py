"""TGOS adoption rules 2 (only the neighbourhood differs), 4 (query has no street) and 5 (village is a place name), synthetic base."""

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


# Rule 5: the query's village is a place name (locality) in the response.
PLACE = "宜蘭縣羅東鎮{}"
PLACE_QUERY = PLACE.format("鍾山新村1號")
PLACE_ANSWER = PLACE.format("公正里18鄰鍾山新村1號")


def test_village_as_place_single_location_adopts_and_keeps_the_query_key(tmp_path):
    entries = [(PLACE_ANSWER, ORIGIN), (PLACE.format("公正里19鄰鍾山新村1號"), north(10))]
    result, evidence, query = run(tmp_path, PLACE_QUERY, PLACE_ANSWER, entries)
    assert result["status"] == "succeeded" and result["reason"] is None
    assert evidence[0]["building_key"] == key_of(PLACE_QUERY) == query["building_key"]


def test_village_as_place_another_door_is_isolated(tmp_path):
    result, _, _ = run(tmp_path, PLACE_QUERY, PLACE.format("公正里18鄰鍾山新村2號"), [(PLACE_ANSWER, ORIGIN)])
    assert result["reason"] == DIFFERS and code_of(result) == "tgos_isolated"


def test_village_as_place_another_locality_is_isolated(tmp_path):
    result, _, _ = run(tmp_path, PLACE_QUERY, PLACE.format("公正里18鄰枕山一村1號"), [(PLACE_ANSWER, ORIGIN)])
    assert result["reason"] == DIFFERS and code_of(result) == "tgos_isolated"


def test_village_as_place_multi_location_is_rejected(tmp_path):
    entries = [(PLACE_ANSWER, ORIGIN), (PLACE.format("公正里19鄰鍾山新村1號"), north(100))]
    result, _, _ = run(tmp_path, PLACE_QUERY, PLACE_ANSWER, entries)
    assert result["reason"] == VILLAGE_DOOR_MULTI and code_of(result) == "tgos_village_door_multi_location"


def test_village_as_place_not_in_the_base_is_isolated(tmp_path):
    entries = [(PLACE.format("公正里18鄰鍾山新村2號"), ORIGIN)]
    result, _, _ = run(tmp_path, PLACE_QUERY, PLACE_ANSWER, entries)
    assert result["reason"] == DIFFERS and code_of(result) == "tgos_isolated"


def test_village_as_place_door_notation_applies(tmp_path):
    query, answer = PLACE.format("聖湖新村10之2號"), PLACE.format("公正里18鄰聖湖新村10號之2")
    result, _, _ = run(tmp_path, query, answer, [(answer, ORIGIN)])
    assert result["status"] == "succeeded" and result["reason"] is None


def test_village_as_place_with_a_different_written_neighbourhood_is_isolated(tmp_path):
    query = PLACE.format("鍾山新村15鄰1號")
    result, _, _ = run(tmp_path, query, PLACE_ANSWER, [(PLACE_ANSWER, ORIGIN)])
    assert result["reason"] == DIFFERS and code_of(result) == "tgos_isolated"


def test_village_as_place_does_not_touch_a_query_with_a_street(tmp_path):
    query = PLACE.format("公正里天祥路1號")
    result, _, _ = run(tmp_path, query, PLACE_ANSWER, [(PLACE_ANSWER, ORIGIN)])
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


# c42: the bopomofo letter ㄧ is read as 一 in the group keys and lookups.
BOPOMOFO_ROAD = "彰化縣彰化市南瑤里{}鄰ㄧ街30號"
NUMERAL_ROAD = "彰化縣彰化市南瑤里{}鄰一街30號"


def test_bopomofo_in_the_road_reaches_the_neighbourhood_group(tmp_path):
    entries = [(NUMERAL_ROAD.format(15), ORIGIN), (NUMERAL_ROAD.format(17), ORIGIN)]
    result, _, query = run(tmp_path, BOPOMOFO_ROAD.format(15), NUMERAL_ROAD.format(16), entries)
    assert result["status"] == "succeeded" and result["reason"] == NEIGHBORHOOD_OUTSIDE_REASON
    assert query["building_key"] == key_of(BOPOMOFO_ROAD.format(15)) != key_of(NUMERAL_ROAD.format(15))


def test_bopomofo_in_the_village_text_reaches_rule_4(tmp_path):
    result, _, _ = run(tmp_path, "嘉義縣大埔鄉ㄧ村278號", "嘉義縣大埔鄉一村13鄰大埔278號", [("嘉義縣大埔鄉一村13鄰大埔278號", ORIGIN)])
    assert result["status"] == "succeeded", result["reason"]


def test_bopomofo_in_the_village_text_reaches_rule_5(tmp_path):
    answer = PLACE.format("公正里18鄰鍾山一村1號")
    result, _, _ = run(tmp_path, PLACE.format("鍾山ㄧ村1號"), answer, [(answer, ORIGIN)])
    assert result["status"] == "succeeded", result["reason"]


C41_QUERY = "苗栗縣頭份市尖山里14鄰尖山141之25號"


def test_response_without_the_final_hao_is_adopted(tmp_path):
    result, _, _ = run(tmp_path, C41_QUERY, "苗栗縣頭份市尖山里14鄰尖山141之25", [])
    assert result["status"] == "succeeded", result["reason"]
    assert result["response_address"] == "苗栗縣頭份市尖山里14鄰尖山141之25"


def test_truncated_door_without_hao_differs(tmp_path):
    result, _, _ = run(tmp_path, "新竹縣寶山鄉寶新路147巷45號", "新竹縣寶山鄉雙新村4鄰寶新路1143", [])
    assert result["reason"] == DIFFERS


def test_response_without_a_door_is_not_a_complete_address(tmp_path):
    result, _, _ = run(tmp_path, C41_QUERY, "苗栗縣頭份市尖山里14鄰尖山", [])
    assert result["reason"] == "TGOS response is not a complete address"


DAPU = "嘉義縣大埔鄉{}"


def test_place_written_as_village_in_the_response_multi_location_is_rejected(tmp_path):
    entries = [(DAPU.format("一村13鄰大埔278號"), ORIGIN), (DAPU.format("一村14鄰茄苳278號"), north(100))]
    result, _, _ = run(tmp_path, DAPU.format("一村278號"), DAPU.format("一村13鄰大埔278號"), entries)
    assert result["reason"] == VILLAGE_DOOR_MULTI and code_of(result) == "tgos_village_door_multi_location"


def test_place_written_as_village_in_the_response_another_door_is_isolated(tmp_path):
    entries = [(DAPU.format("一村13鄰大埔279號"), ORIGIN)]
    result, _, _ = run(tmp_path, DAPU.format("一村278號"), DAPU.format("一村13鄰大埔279號"), entries)
    assert result["reason"] == DIFFERS and code_of(result) == "tgos_isolated"


def test_place_group_counts_base_rows_written_either_way(tmp_path):
    entries = [(DAPU.format("一村278號"), ORIGIN), (DAPU.format("一村13鄰大埔278號"), north(10))]
    result, _, _ = run(tmp_path, DAPU.format("一村278號"), DAPU.format("一村13鄰大埔278號"), entries)
    assert result["status"] == "succeeded", result["reason"]


def test_place_group_is_several_locations_when_the_two_writings_are_apart(tmp_path):
    entries = [(DAPU.format("一村278號"), ORIGIN), (DAPU.format("一村13鄰大埔278號"), north(100))]
    result, _, _ = run(tmp_path, DAPU.format("一村278號"), DAPU.format("一村13鄰大埔278號"), entries)
    assert result["reason"] == VILLAGE_DOOR_MULTI


def test_a_listed_village_before_the_door_is_not_a_place(tmp_path):
    query = "雲林縣虎尾鎮興南里548號"
    answer = "雲林縣虎尾鎮興南里5鄰興南548號"
    result, _, _ = run(tmp_path, query, answer, [(answer, ORIGIN)])
    assert json.loads(key_of(query)[3:])["village"] == "興南里"
    assert result["status"] == "succeeded", result["reason"]


def test_a_road_that_only_ends_in_cun_is_not_a_place_from_the_village(tmp_path):
    query = "臺中市東勢區東崎路五段雪山一村4號"
    answer = "臺中市東勢區泰昌里20鄰東崎路五段雪山一村4號"
    result, _, _ = run(tmp_path, query, answer, [])
    assert result["status"] == "succeeded", result["reason"]


SECTION = "臺北市大安區{}"
SECTION_QUERY = SECTION.format("安和路149號")
SECTION_ANSWER = SECTION.format("全安里14鄰安和路二段149號")
SECTION_MULTI = "TGOS road section added and the base has several locations"


def test_rule_9_single_location_adopts_the_added_section(tmp_path):
    result, evidence, query = run(tmp_path, SECTION_QUERY, SECTION_ANSWER, [(SECTION_ANSWER, ORIGIN)])
    assert result["status"] == "succeeded" and result["reason"] is None
    assert evidence[0]["building_key"] == key_of(SECTION_QUERY) == query["building_key"]


def test_rule_9_two_sections_with_the_same_door_far_apart_are_rejected(tmp_path):
    entries = [(SECTION.format("安和路一段149號"), ORIGIN), (SECTION_ANSWER, north(100))]
    result, _, _ = run(tmp_path, SECTION_QUERY, SECTION_ANSWER, entries)
    assert result["reason"] == SECTION_MULTI and code_of(result) == "tgos_road_section_multi_location"


def test_rule_9_base_rows_within_30_m_are_one_location(tmp_path):
    entries = [(SECTION_ANSWER, ORIGIN), (SECTION.format("全安里15鄰安和路二段149號"), north(10)), (SECTION_QUERY, north(20))]
    result, _, _ = run(tmp_path, SECTION_QUERY, SECTION_ANSWER, entries)
    assert result["status"] == "succeeded", result["reason"]


def test_rule_9_not_in_the_base_is_isolated(tmp_path):
    result, _, _ = run(tmp_path, SECTION_QUERY, SECTION_ANSWER, [(SECTION.format("安和路二段151號"), ORIGIN)])
    assert result["reason"] == DIFFERS and code_of(result) == "tgos_isolated"


def test_rule_9_base_with_only_another_section_is_isolated(tmp_path):
    result, _, _ = run(tmp_path, SECTION_QUERY, SECTION_ANSWER, [(SECTION.format("安和路一段149號"), ORIGIN)])
    assert result["reason"] == DIFFERS and code_of(result) == "tgos_isolated"


def test_rule_9_does_not_apply_to_a_query_that_writes_a_section(tmp_path):
    query = SECTION.format("安和路一段149號")
    result, _, _ = run(tmp_path, query, SECTION_ANSWER, [(SECTION_ANSWER, ORIGIN)])
    assert result["reason"] == DIFFERS


def test_rule_9_another_door_or_road_is_rejected(tmp_path):
    for number, answer in enumerate((SECTION.format("全安里14鄰安和路二段151號"), SECTION.format("全安里14鄰安和街二段149號"))):
        folder = tmp_path / str(number)
        folder.mkdir()
        result, _, _ = run(folder, SECTION_QUERY, answer, [(answer, ORIGIN)])
        assert result["reason"] == DIFFERS, answer


def test_rule_9_a_written_neighbourhood_must_match(tmp_path):
    query = SECTION.format("全安里13鄰安和路149號")
    result, _, _ = run(tmp_path, query, SECTION_ANSWER, [(SECTION_ANSWER, ORIGIN)])
    assert result["reason"] == DIFFERS
