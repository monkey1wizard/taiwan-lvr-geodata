"""R09-6: district_missing addresses are sent to TGOS and adopted only inside their candidate towns."""

import json

import pytest

from lvr_pipeline.contracts.validate import validate_dataset_rows
from lvr_pipeline.results.review import TGOS_REJECTION_CODES, build_review
from lvr_pipeline.storage.parquet import rows
from lvr_pipeline.storage.runs import digest
from lvr_pipeline.tgos import import_tgos, load_state, merge_ledger, prepare_tgos, transition_batch
from lvr_pipeline.tgos.batches import select_district_candidates
from test_p1_conversion import ADDRESS
from test_p2_offline import index_of, run
from test_p3_tgos import response

PLAIN = "臺北市測試路99號"
VILLAGE = "臺北市大安里測試路98號"
NO_ROAD = "臺北市無此路7號"
KEY = "臺北市中正區測試路21號"
GOOD = ("121.51", "25.01")
NOT_CANDIDATE = "TGOS response district is not a candidate district"
DIFFERS = "TGOS response address differs from submitted address"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("r09_6")
    converted, _, descriptor, index, _, state = run(
        tmp_path,
        coordinates=[
            (ADDRESS, "63", "6300100", 121.5, 25.0),
            ("臺北市文山區測試路11號", "63", "6300800", 121.55, 25.02),
        ],
        records=[
            {"address": ADDRESS, "date": "1141102"},
            {"address": KEY, "date": "1150102"},
            {"address": PLAIN, "date": "1150103"},
            {"address": VILLAGE, "date": "1150104"},
            {"address": NO_ROAD, "date": "1150105"},
        ],
    )
    work = tmp_path / "work"
    prepared, _ = prepare_tgos(state, work, tmp_path / "exchange", index=index_of(state))
    batch_id = list(rows(prepared / "tgos_batches.parquet"))[0]["batch_id"]
    submitted = transition_batch(prepared, work, batch_id, "submitted")
    return {
        "tmp": tmp_path, "converted": converted, "descriptor": descriptor, "state": state, "index": index,
        "prepared": prepared, "submitted": submitted, "batch_id": batch_id, "work": work,
        "queries": {r["address"]: r for r in rows(prepared / "tgos_queries.parquet")},
    }


def _import(built, name, answers):
    """Import a response; ``answers`` maps submitted address to (Response_Address, (x, y))."""
    path = built["tmp"] / f"{name}.csv"
    response(
        path,
        [
            {"Address": a, "Response_Address": answers.get(a, ("", ("", "")))[0],
             "Response_X": answers.get(a, ("", ("", "")))[1][0], "Response_Y": answers.get(a, ("", ("", "")))[1][1]}
            for a in built["queries"]
        ],
    )
    work = built["tmp"] / f"work-{name}"
    imported = import_tgos(built["submitted"], work, built["batch_id"], path, index=index_of(built["submitted"]))
    return imported, work, {r["address"]: r for r in rows(imported / "tgos_imports.parquet")}


def test_district_queries_are_selected_with_fingerprint_kind_and_towns(built):
    queries = built["queries"]
    assert set(queries) == {KEY, PLAIN, VILLAGE}
    for address in (PLAIN, VILLAGE):
        row = queries[address]
        assert row["query_kind"] == "district_missing" and row["building_key"] is None
        assert row["query_fingerprint"] == digest(["district_missing", "63", address])
        assert json.loads(row["candidate_towns"]) == ["中正區", "文山區"]
        assert row["address_family"] == digest(["63", "", "測試路"])
    key = queries[KEY]
    assert key["query_kind"] == "building_key" and key["building_key"] and key["candidate_towns"] is None
    ordered = sorted(queries.values(), key=lambda r: r["ordinal"])
    assert [r["address"] for r in ordered] == [KEY, VILLAGE, PLAIN]


def test_road_in_no_town_is_not_sent_and_is_counted(built):
    assert NO_ROAD not in built["queries"]
    _, report, _ = load_state(built["prepared"])
    assert report["tgos_district_missing_without_candidates"] == 1


def test_suspended_county_is_not_selected(built):
    occurrences = built["state"] / "address_occurrences.parquet"
    offline_rows = built["index"] / "offline_rows.parquet"
    counted = {"63": 0}
    chosen, without = select_district_candidates(
        occurrences, offline_rows, suspended_codes={"63"}, suspended_excluded=counted
    )
    assert chosen == [] and without == 0 and counted == {"63": 3}
    chosen, without = select_district_candidates(occurrences, offline_rows, sent_text={PLAIN, VILLAGE})
    assert chosen == [] and without == 1


def test_candidate_fill_is_adopted_without_changing_the_address_result(built):
    imported, work, results = _import(
        built, "good",
        {PLAIN: ("臺北市文山區測試路99號", GOOD), VILLAGE: ("臺北市中正區大安里測試路98號", GOOD)},
    )
    fill = results[PLAIN]
    assert fill["status"] == "succeeded" and fill["reason"] == "tgos_district_fill"
    assert json.loads(fill["response_building_key"][3:])["town"] == "文山區"
    assert results[VILLAGE]["status"] == "succeeded"
    queries = {r["address"]: r for r in rows(imported / "tgos_queries.parquet")}
    assert queries[PLAIN]["status"] == queries[VILLAGE]["status"] == "succeeded"
    merged = merge_ledger(imported, work, built["tmp"] / "merged-good")
    for name in (
        "address_index.parquet", "unmatched_addresses.parquet", "coordinate_resolutions.parquet",
        "address_observations.parquet",
    ):
        # b14: the import snapshot holds the ledger only; the merged state equals the offline state.
        assert list(rows(merged / name)) == list(rows(built["state"] / name))


def test_key_query_in_the_same_batch_is_adopted_as_before(built):
    imported, work, results = _import(
        built, "key", {KEY: (KEY, GOOD), PLAIN: ("臺北市文山區測試路99號", GOOD)}
    )
    assert results[KEY]["status"] == "succeeded" and results[KEY]["reason"] is None
    assert results[KEY]["response_building_key"] is None
    before = list(rows(built["state"] / "address_observations.parquet"))
    merged = merge_ledger(imported, work, built["tmp"] / "merged-key")
    after = list(rows(merged / "address_observations.parquet"))
    added = [r for r in after if r["source_kind"] == "tgos_result"]
    assert len(after) == len(before) + 1 and [r["normalized_address"] for r in added] == [KEY]


@pytest.mark.parametrize(
    "name,address,answer,reason",
    [
        ("town", PLAIN, "臺北市大同區測試路99號", NOT_CANDIDATE),
        ("county", PLAIN, "新北市板橋區測試路99號", NOT_CANDIDATE),
        ("door", PLAIN, "臺北市文山區測試路100號", DIFFERS),
        ("road", PLAIN, "臺北市文山區別條路99號", DIFFERS),
        ("village", VILLAGE, "臺北市中正區復興里測試路98號", DIFFERS),
    ],
)
def test_fill_outside_the_rules_is_rejected(built, name, address, answer, reason):
    imported, _, results = _import(built, "bad-" + name, {address: (answer, GOOD)})
    result = results[address]
    assert result["status"] == "rejected" and result["response_building_key"] is None
    assert result["reason"] == reason and reason in TGOS_REJECTION_CODES
    assert {r["address"]: r for r in rows(imported / "tgos_queries.parquet")}[address]["status"] == "rejected"
    if reason == NOT_CANDIDATE:
        assert TGOS_REJECTION_CODES[reason] == "tgos_isolated"


def test_same_response_again_is_idempotent_and_review_skips_district_queries(built):
    imported, work, results = _import(
        built, "review", {PLAIN: ("臺北市大同區測試路99號", GOOD), KEY: (KEY, GOOD)}
    )
    path = built["tmp"] / "review.csv"
    assert import_tgos(imported, work, built["batch_id"], path, index=index_of(imported)) == imported
    review = build_review(
        built["converted"], built["state"], built["tmp"] / "work-review-out",
        descriptor=built["descriptor"], tgos_state=merge_ledger(imported, work, built["tmp"] / "merged-review"),
    )
    codes = {}
    for row in rows(review / "exception_addresses.parquet"):
        codes.setdefault(row["reason_code"], []).append(row["raw_address"])
    assert PLAIN in codes["district_missing"] and NO_ROAD in codes["district_missing"]
    assert not any(code.startswith("tgos_") for code in codes)


def test_carried_ledger_keeps_the_district_query_and_does_not_resend_it(built):
    tmp = built["tmp"]
    first, _ = prepare_tgos(
        built["state"], tmp / "carry-old", tmp / "carry-exchange", limit=2, index=built["index"]
    )
    old = {r["address"]: r for r in rows(first / "tgos_queries.parquet")}
    assert set(old) == {KEY, VILLAGE}
    seeded, _ = prepare_tgos(
        built["state"], tmp / "carry-new", tmp / "carry-exchange", ledger=first, index=built["index"]
    )
    queries = list(rows(seeded / "tgos_queries.parquet"))
    carried = {r["address"]: r for r in queries if r["address"] in old}
    assert len(queries) == 3 and len({r["query_fingerprint"] for r in queries}) == 3
    kept = carried[VILLAGE]
    assert kept["query_kind"] == "district_missing" and kept["building_key"] is None
    assert kept["query_fingerprint"] == old[VILLAGE]["query_fingerprint"] == digest(["district_missing", "63", VILLAGE])
    new_batch = [r for r in queries if r["address"] not in old]
    assert [r["address"] for r in new_batch] == [PLAIN]


def _query_row(**changes):
    row = {
        "batch_id": "b", "ordinal": 1, "query_fingerprint": "f", "key_version": "v2", "query_kind": "building_key",
        "building_key": None, "candidate_towns": None, "address": "a", "county_code": "63",
        "address_family": "x", "status": "prepared", "result_id": None,
    }
    return {**row, **changes}


def test_query_kind_and_building_key_must_agree():
    key = 'v2:{"county":"63","door":"1","locality_road":"測試路","town":"中正區"}'
    validate_dataset_rows([_query_row(building_key=key)], "tgos-query")
    validate_dataset_rows([_query_row(query_kind="district_missing", candidate_towns='["中正區"]')], "tgos-query")
    with pytest.raises(ValueError, match="kind and building key"):
        validate_dataset_rows([_query_row()], "tgos-query")
    with pytest.raises(ValueError, match="kind and building key"):
        validate_dataset_rows(
            [_query_row(query_kind="district_missing", building_key=key, candidate_towns='["中正區"]')], "tgos-query"
        )
    with pytest.raises(ValueError, match="candidate towns"):
        validate_dataset_rows([_query_row(query_kind="district_missing")], "tgos-query")
    # A carried query without a key under the current rules (contract rule 5) has no key and no towns.
    validate_dataset_rows([_query_row(query_kind="keyless")], "tgos-query")
    with pytest.raises(ValueError, match="kind and building key"):
        validate_dataset_rows([_query_row(query_kind="keyless", building_key=key)], "tgos-query")
    with pytest.raises(ValueError, match="query kind"):
        validate_dataset_rows([_query_row(query_kind="other", building_key=key)], "tgos-query")
