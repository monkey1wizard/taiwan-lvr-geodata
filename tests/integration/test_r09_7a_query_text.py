"""R09-7a: TGOS query text is door level, built from parsed identity fields, and a queried door is not queried again."""

import pytest

from lvr_pipeline.addresses.identity import building_key
from lvr_pipeline.addresses.pool import district_query
from lvr_pipeline.storage.parquet import rows
from lvr_pipeline.storage.runs import digest
from lvr_pipeline.tgos import _artifact_paths, _write_state, load_state, prepare_tgos, transition_batch
from lvr_pipeline.tgos import batches
from lvr_pipeline.tgos.batches import _carry_ledger, district_query_text, key_query_text
from test_p1_conversion import ADDRESS
from test_p2_offline import index_of, run

FLOOR = "臺北市中正區測試路18之2號3樓"
VILLAGE = "臺北市中正區大安里3鄰測試路5號2樓(後棟)"
ATTACHED = "臺北市中正區測試路6號附9"
PLAIN = "臺北市中正區測試路21號"
DISTRICT_2F = "臺北市測試路99號2樓"
DISTRICT_3F = "臺北市測試路99號3樓"
TEXTS = {
    FLOOR: "臺北市中正區測試路18之2號",
    VILLAGE: "臺北市中正區大安里3鄰測試路5號",
    ATTACHED: "臺北市中正區測試路6號附9",
    PLAIN: PLAIN,
}


def _build(tmp_path):
    *_, state = run(
        tmp_path,
        coordinates=[
            (ADDRESS, "63", "6300100", 121.5, 25.0),
            ("臺北市文山區測試路11號", "63", "6300800", 121.55, 25.02),
        ],
        records=[
            {"address": ADDRESS, "date": "1141102"},
            *({"address": a, "date": "1150102"} for a in (FLOOR, VILLAGE, ATTACHED, PLAIN, DISTRICT_2F, DISTRICT_3F)),
        ],
    )
    prepared, _ = prepare_tgos(state, tmp_path / "work", tmp_path / "exchange", index=index_of(state))
    return state, prepared


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    state, prepared = _build(tmp_path_factory.mktemp("r09_7a"))
    return {"state": state, "prepared": prepared, "queries": list(rows(prepared / "tgos_queries.parquet"))}


def test_key_query_text_is_door_level_and_round_trips():
    cases = {
        "彰化縣彰化市南瑤里天祥路30巷1號5樓": "彰化縣彰化市南瑤里天祥路30巷1號",
        FLOOR: TEXTS[FLOOR],
        VILLAGE: TEXTS[VILLAGE],
        ATTACHED: TEXTS[ATTACHED],
        "臺北市中正區測試路10號之1": "臺北市中正區測試路10號之1",
    }
    for address, expected in cases.items():
        key = building_key(address)
        text = key_query_text(key)
        assert text == expected and building_key(text) == key
    assert key_query_text('v2:{"county":"10007","door":"1","locality_road":"天祥路30巷","town":"彰化市","village":"南瑤里"}') == (
        "彰化縣彰化市南瑤里天祥路30巷1號"
    )


def test_prepared_batch_sends_only_door_level_text(built):
    key_rows = {r["address"]: r for r in built["queries"] if r["query_kind"] == "building_key"}
    assert set(key_rows) == set(TEXTS.values())
    for row in key_rows.values():
        assert building_key(row["address"]) == row["building_key"]
        assert row["query_fingerprint"] == digest([row["building_key"], row["address"]])
    village = key_rows[TEXTS[VILLAGE]]["building_key"]
    assert '"village":"大安里"' in village and '"neighborhood":"3"' in village
    assert not any(c in r["address"] for r in built["queries"] for c in "樓室()")


def test_district_floors_merge_into_one_text_without_town(built):
    district = [r for r in built["queries"] if r["query_kind"] == "district_missing"]
    assert [r["address"] for r in district] == ["臺北市測試路99號"]
    assert district[0]["query_fingerprint"] == digest(["district_missing", "63", "臺北市測試路99號"])
    report = load_state(built["prepared"])[1]
    assert report["tgos_district_text_merged"] == 1 and report["tgos_key_text_merged"] == 0
    assert report["tgos_key_already_queried"] == 0 and report["tgos_district_already_queried"] == 0
    assert district_query_text("63", district_query(DISTRICT_2F)) == district_query_text("63", district_query(DISTRICT_3F))


def _old_style_ledger(prepared, work, status, *, only_sent=False):
    """Batch 1 as it was sent: the floor key and the district door carry their original address text."""
    paths = _artifact_paths(prepared, load_state(prepared)[0])
    queries = list(rows(paths["tgos-query"]))
    for row in queries:
        if row["address"] == TEXTS[FLOOR]:
            row["address"] = FLOOR
            row["query_fingerprint"] = digest([row["building_key"], FLOOR])
            row["status"] = status
        elif row["query_kind"] == "district_missing":
            row["address"] = DISTRICT_2F
            row["query_fingerprint"] = digest(["district_missing", "63", DISTRICT_2F])
            row["status"] = status
    if only_sent:
        queries = [r for r in queries if r["address"] in {FLOOR, DISTRICT_2F}]
    batch_rows = list(rows(paths["tgos-batch"]))
    batch_rows[0]["address_count"] = len(queries)
    ledger = _write_state(
        prepared, work, action="old-text-ledger", batches=batch_rows, queries=queries,
        results=[], aliases=[], alias_events=[],
    )
    return ledger, batch_rows[0]["batch_id"]


def test_carry_rederives_keys_of_old_and_new_style_text(tmp_path):
    _, prepared = _build(tmp_path)
    ledger, _ = _old_style_ledger(prepared, tmp_path / "old", "submitted")
    _, carried, rekeyed = _carry_ledger(ledger)
    by_text = {r["address"]: r for r in carried if r["query_kind"] == "building_key"}
    assert FLOOR in by_text and TEXTS[VILLAGE] in by_text
    for text, row in by_text.items():
        assert row["building_key"] == building_key(text)
    assert by_text[FLOOR]["building_key"] == building_key(TEXTS[FLOOR])
    assert rekeyed == 0


@pytest.mark.parametrize("status", ["submitted", "succeeded", "failed"])
def test_key_and_door_queried_under_old_text_are_not_selected_again(tmp_path, status):
    state, prepared = _build(tmp_path)
    ledger, old_batch = _old_style_ledger(prepared, tmp_path / "old", status, only_sent=True)
    seeded, _ = prepare_tgos(
        state, tmp_path / "new", tmp_path / "exchange", ledger=ledger, index=index_of(state)
    )
    report = load_state(seeded)[1]
    assert report["tgos_key_already_queried"] == 1 and report["tgos_district_already_queried"] == 1
    new = [r for r in rows(seeded / "tgos_queries.parquet") if r["batch_id"] != old_batch]
    assert {r["address"] for r in new} == set(TEXTS.values()) - {TEXTS[FLOOR]}


def test_cancelled_old_batch_makes_its_keys_selectable_again(tmp_path):
    state, prepared = _build(tmp_path)
    ledger, old_batch = _old_style_ledger(prepared, tmp_path / "old", "submitted")
    cancelled = transition_batch(ledger, tmp_path / "old", old_batch, "cancelled", reason="x")
    seeded, _ = prepare_tgos(
        state, tmp_path / "new", tmp_path / "exchange", ledger=cancelled, index=index_of(state)
    )
    new = {r["address"] for r in rows(seeded / "tgos_queries.parquet") if r["batch_id"] != old_batch}
    assert new == {TEXTS[FLOOR], "臺北市測試路99號"}
    assert load_state(seeded)[1]["tgos_key_already_queried"] == 0


def test_all_sent_keys_leave_nothing_to_select(tmp_path):
    state, prepared = _build(tmp_path)
    ledger, _ = _old_style_ledger(prepared, tmp_path / "old", "submitted")
    with pytest.raises(ValueError, match="No eligible TGOS candidates remain"):
        prepare_tgos(state, tmp_path / "new", tmp_path / "exchange", ledger=ledger, index=index_of(state))


def test_text_that_does_not_give_back_its_key_is_never_built(monkeypatch):
    key = building_key(FLOOR)
    real = batches.building_key
    monkeypatch.setattr(batches, "building_key", lambda text: real(text).replace('"door":"', '"door":"9'))
    with pytest.raises(ValueError, match="18之2"):
        key_query_text(key)
    monkeypatch.undo()
    parsed = district_query(DISTRICT_2F)
    real_district = batches.district_query
    monkeypatch.setattr(
        batches, "district_query", lambda text: (*real_district(text)[:3], "100", None, None)
    )
    with pytest.raises(ValueError, match="99號"):
        district_query_text("63", parsed)


def test_key_without_a_round_trip_text_is_counted_not_sent(monkeypatch):
    good, bad = building_key(PLAIN), building_key(FLOOR)
    real = batches.key_query_text

    def text(key):
        if key == bad:
            raise ValueError("TGOS query text does not give back its building key")
        return real(key)

    monkeypatch.setattr(batches, "key_query_text", text)
    counts = {"already_queried": 0, "text_not_round_trip": 0}
    row = {"status": "unmatched", "county_code": "63", "canonical_address": "", "address_family": "f"}
    selected = batches.select_unmatched_candidates(
        [{**row, "building_key": good}, {**row, "building_key": bad}],
        retry_fingerprints=set(), prior=set(), sent_text=set(), suspended_codes=set(), counts=counts,
    )
    assert [r["building_key"] for r in selected] == [good]
    assert counts["text_not_round_trip"] == 1
