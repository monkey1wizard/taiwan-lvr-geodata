"""R07-7: build, land and park detail files linked to main rows by 編號 inside one file group. Synthetic data only."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lvr_pipeline.converted import export_converted  # noqa: E402
from lvr_pipeline.pipeline import run_full  # noqa: E402
from lvr_pipeline.storage.runs import load_snapshot  # noqa: E402
from lvr_pipeline.transactions.inventory import inventory_json, write_raw_manifest  # noqa: E402
from lvr_pipeline.transactions.normalize import normalize  # noqa: E402
from lvr_pipeline.transactions.read import ingest  # noqa: E402
from test_p1_conversion import CODE, add_members, dataset, make_source, no_char_fixes, rules, verified_source  # noqa: E402
from test_p2_offline import source, use_synthetic_admin_towns  # noqa: E402

HEADER = ["編號", "棟別", "note"]
ENGLISH = ["Serial", "Kind", "Remark"]
KEYS = ["serial_missing", "serial_duplicated", "detail_unmatched", "detail_ambiguous", "detail_member_missing",
        "detail_serial_column_missing", "detail_row_failed", "linked_rows"]
ZERO = dict.fromkeys(KEYS, 0)


def detail(rows, header=HEADER, english=ENGLISH):
    return (header, rows, english)


def stages(tmp_path, raw, manifest, batches=("115q1",)):
    work = tmp_path / "work"
    ingested = ingest(raw, manifest, list(batches), work, batch_rows=2, code_commit=CODE)
    normalized = normalize(ingested, work, cutoff=202610, rules_path=rules(tmp_path), char_fixes=no_char_fixes(), batch_rows=2, code_commit=CODE)
    return ingested, normalized, export_converted(normalized, work, code_commit=CODE)


def build(tmp_path, records, members, *, categories=("sales",)):
    raw, _ = make_source(tmp_path, records, categories=categories)
    return stages(tmp_path, raw, add_members(tmp_path, members))


def entry(report, kind, *, letter="a", category="sales", batch="115q1"):
    found = [row for row in report["detail_link"]
             if (row["batch"], row["county_letter"], row["category"], row["detail_kind"]) == (batch, letter, category, kind)]
    assert len(found) == 1
    return found[0]


def counters(report, kind, letter="a"):
    row = entry(report, kind, letter=letter)
    return {key: row[key] for key in KEYS}


def all_details(prefix="a_lvr_land_a"):
    return {f"{prefix}_build.csv": detail([["S1", "b1", ""], ["S1", "b2", ""], ["S1", "b3", ""]]),
            f"{prefix}_land.csv": detail([["S1", "l1", ""], ["S1", "l2", ""]]),
            f"{prefix}_park.csv": detail([["S1", "p1", ""]])}


def empty_details(**override):
    members = {"a_lvr_land_a_build.csv": detail([]), "a_lvr_land_a_land.csv": detail([]), "a_lvr_land_a_park.csv": detail([])}
    members.update(override)
    return members


def test_three_build_two_land_one_park_link_in_row_order_and_main_without_details_gets_none(tmp_path):
    ingested, _, converted = build(tmp_path, [{"serial": "S1"}, {"serial": "S2"}], all_details())
    observations = dataset(converted, "observation")
    first, second = observations[0]["raw_record_id"], observations[1]["raw_record_id"]
    linked = dataset(converted, "detail")
    assert len(linked) == 6 and {row["raw_record_id"] for row in linked} == {first}
    assert second not in {row["raw_record_id"] for row in linked}
    for kind, count in [("build", 3), ("land", 2), ("park", 1)]:
        rows = [row for row in linked if row["detail_kind"] == kind]
        assert [row["source_row_number"] for row in rows] == list(range(3, 3 + count))
        assert [json.loads(row["raw_fields_json"])["棟別"] for row in rows] == [f"{kind[0]}{n}" for n in range(1, count + 1)]
        assert all(row["member_path"] == f"a_lvr_land_a_{kind}.csv" for row in rows)
    report = load_snapshot(converted, "converted")[1]
    assert report["detail_rows"] == 6 and report["dataset_counts"]["detail"] == 6
    assert counters(report, "build") == {**ZERO, "linked_rows": 3}
    assert counters(report, "land") == {**ZERO, "linked_rows": 2}
    assert counters(report, "park") == {**ZERO, "linked_rows": 1}
    assert entry(report, None)["serial_missing"] == 0 and entry(report, None)["serial_duplicated"] == 0
    ingest_report = load_snapshot(ingested, "ingest")[1]
    assert ingest_report["detail_input_rows"] == 6 and len(ingest_report["detail_members"]) == 3
    member = next(m for m in ingest_report["detail_members"] if m["detail_kind"] == "build")
    assert (member["input_rows"], member["english_rows"], member["header"]) == (3, 1, HEADER)
    assert (member["path"], member["category"], member["batch"]) == ("a_lvr_land_a_build.csv", "sales", "115q1")
    assert ingest_report["input_rows"] == 2  # main counts keep their meaning
    ids = [row["detail_record_id"] for row in dataset(ingested, "ingest-detail")]
    assert len(ids) == len(set(ids)) == 6


def test_empty_main_serial_gets_no_details_and_is_counted(tmp_path):
    _, _, converted = build(tmp_path, [{"serial": ""}, {"serial": "S1"}], all_details())
    report = load_snapshot(converted, "converted")[1]
    assert entry(report, None)["serial_missing"] == 1 and report["detail_rows"] == 6
    assert {row["raw_record_id"] for row in dataset(converted, "detail")} == {dataset(converted, "observation")[1]["raw_record_id"]}


def test_duplicated_main_serial_hangs_nothing_and_detail_is_ambiguous(tmp_path):
    _, _, converted = build(tmp_path, [{"serial": "S1"}, {"serial": "S1"}],
                            empty_details(**{"a_lvr_land_a_build.csv": detail([["S1", "b1", ""]])}))
    report = load_snapshot(converted, "converted")[1]
    assert entry(report, None)["serial_duplicated"] == 2
    assert counters(report, "build") == {**ZERO, "detail_ambiguous": 1}
    assert report["detail_rows"] == 0 and dataset(converted, "detail") == []


def test_unknown_or_empty_detail_serial_is_unmatched(tmp_path):
    members = {**all_details(), "a_lvr_land_a_build.csv": detail([["S1", "b1", ""], ["NOPE", "b2", ""], ["", "b3", ""]])}
    _, _, converted = build(tmp_path, [{"serial": "S1"}], members)
    report = load_snapshot(converted, "converted")[1]
    assert counters(report, "build") == {**ZERO, "detail_unmatched": 2, "linked_rows": 1}
    assert report["detail_rows"] == 1 + 2 + 1


def test_serial_is_matched_after_stripping_only(tmp_path):
    members = empty_details(**{"a_lvr_land_a_build.csv": detail([["S1 ", "b1", ""], ["s1", "b2", ""]])})
    _, _, converted = build(tmp_path, [{"serial": " S1 "}], members)
    assert counters(load_snapshot(converted, "converted")[1], "build") == {**ZERO, "detail_unmatched": 1, "linked_rows": 1}


def test_detail_member_without_serial_column_is_not_linked(tmp_path):
    members = {**all_details(), "a_lvr_land_a_build.csv": (["棟別", "note"], [["b1", ""], ["b2", ""]], ["Kind", "Remark"])}
    _, _, converted = build(tmp_path, [{"serial": "S1"}], members)
    report = load_snapshot(converted, "converted")[1]
    assert counters(report, "build") == {**ZERO, "detail_serial_column_missing": 1}
    assert report["detail_rows"] == 3 and {row["detail_kind"] for row in dataset(converted, "detail")} == {"land", "park"}


def test_detail_row_with_wrong_column_count_is_failed_not_output(tmp_path):
    members = {**all_details(), "a_lvr_land_a_build.csv": detail([["S1", "b1", ""], ["S1", "b2"]])}
    ingested, _, converted = build(tmp_path, [{"serial": "S1"}], members)
    report = load_snapshot(converted, "converted")[1]
    assert counters(report, "build") == {**ZERO, "detail_row_failed": 1, "linked_rows": 1}
    assert report["detail_rows"] == 1 + 2 + 1
    failed = [row for row in dataset(ingested, "ingest-detail") if row["row_status"] == "failed"]
    assert len(failed) == 1 and failed[0]["raw_fields_json"] == "{}" and failed[0]["serial"] is None
    assert load_snapshot(ingested, "ingest")[1]["detail_input_rows"] == 5


def test_missing_park_member_is_counted_and_not_treated_as_no_details(tmp_path):
    members = all_details()
    del members["a_lvr_land_a_park.csv"]
    _, _, converted = build(tmp_path, [{"serial": "S1"}], members)
    report = load_snapshot(converted, "converted")[1]
    assert counters(report, "park") == {**ZERO, "detail_member_missing": 1}
    assert counters(report, "build")["detail_member_missing"] == 0 and report["detail_rows"] == 5


def test_source_without_detail_members_counts_every_kind_missing(tmp_path):
    raw, manifest = make_source(tmp_path, [{"serial": "S1"}], categories=("sales", "rent"))
    report = load_snapshot(stages(tmp_path, raw, manifest)[2], "converted")[1]
    assert report["detail_rows"] == 0
    for category in ["sales", "rent"]:
        for kind in ["build", "land", "park"]:
            assert entry(report, kind, category=category)["detail_member_missing"] == 1


def test_multi_door_main_row_gets_details_once_per_observation(tmp_path):
    _, _, converted = build(tmp_path, [{"serial": "S1", "address": "臺北市中正區測試路10、12號"}], all_details())
    assert len(dataset(converted, "address-component")) == 2
    linked = dataset(converted, "detail")
    assert len(linked) == 6 and len({row["raw_record_id"] for row in linked}) == 1
    assert len({row["detail_record_id"] for row in linked}) == 6


def test_same_serial_in_other_county_letter_is_never_cross_linked(tmp_path):
    raw, _ = make_source(tmp_path, [{"serial": "S1"}], categories=("sales",))
    header = ["土地位置建物門牌", "交易標的", "交易年月日", "總價元", "建物移轉總面積平方公尺", "編號"]
    row = ["臺北市中正區測試路10號", "房地(土地+建物)", "1150102", "100", "1.5", "S1"]
    manifest = add_members(tmp_path, {
        "b_lvr_land_a.csv": (header, [row]),
        "a_lvr_land_a_build.csv": detail([["S1", "a-build", ""]]),
        "b_lvr_land_a_build.csv": detail([["S1", "b-build", ""], ["ONLYA", "x", ""]])})
    _, _, converted = stages(tmp_path, raw, manifest)
    by_member = {row["member_path"]: row["raw_record_id"] for row in dataset(converted, "observation")}
    linked = {json.loads(row["raw_fields_json"])["棟別"]: row["raw_record_id"] for row in dataset(converted, "detail")}
    assert linked == {"a-build": by_member["a_lvr_land_a.csv"], "b-build": by_member["b_lvr_land_a.csv"]}
    report = load_snapshot(converted, "converted")[1]
    assert counters(report, "build", "a") == {**ZERO, "linked_rows": 1}
    assert counters(report, "build", "b") == {**ZERO, "detail_unmatched": 1, "linked_rows": 1}
    assert counters(report, "land", "b")["detail_member_missing"] == 1


def test_same_serial_in_two_batches_is_never_cross_linked(tmp_path):
    raw, _ = make_source(tmp_path, [{"serial": "S1"}], batch="115q1", categories=("sales",))
    make_source(tmp_path, [{"serial": "S1"}], batch="115q2", categories=("sales",))
    first = add_members(tmp_path, {"a_lvr_land_a_build.csv": detail([["S1", "q1", ""]])}, batch="115q1")["inputs"][0]
    second = add_members(tmp_path, {"a_lvr_land_a_build.csv": detail([["S1", "q2a", ""], ["S1", "q2b", ""]])}, batch="115q2")["inputs"][0]
    _, _, converted = stages(tmp_path, raw, {"inputs": [first, second]}, ("115q1", "115q2"))
    batch_of = {row["raw_record_id"]: row["src_batch"] for row in dataset(converted, "observation")}
    linked = [(batch_of[row["raw_record_id"]], json.loads(row["raw_fields_json"])["棟別"]) for row in dataset(converted, "detail")]
    assert sorted(linked) == [("115q1", "q1"), ("115q2", "q2a"), ("115q2", "q2b")]
    report = load_snapshot(converted, "converted")[1]
    assert entry(report, "build", batch="115q1")["linked_rows"] == 1 and entry(report, "build", batch="115q2")["linked_rows"] == 2


def test_excluded_main_row_still_links_its_details(tmp_path):
    _, _, converted = build(tmp_path, [{"serial": "S1", "target": "土地"}], all_details())
    disposition = dataset(converted, "disposition")
    assert disposition[0]["outcome"] == "excluded" and dataset(converted, "observation") == []
    linked = dataset(converted, "detail")
    assert len(linked) == 6 and {row["raw_record_id"] for row in linked} == {disposition[0]["raw_record_id"]}


def test_detail_without_english_row_keeps_a_digit_first_row(tmp_path):
    members = empty_details(**{"a_lvr_land_a_build.csv": (HEADER, [["1", "b1", ""]])})
    _, _, converted = build(tmp_path, [{"serial": "1"}], members)
    rows = dataset(converted, "detail")
    assert len(rows) == 1 and rows[0]["source_row_number"] == 2


@pytest.mark.parametrize("first_row", [["S1", "b1", ""], ["S1", "Kind", "Remark"], ["S1", "建物", "x"]])
def test_first_data_row_with_text_serial_is_never_dropped_as_label(tmp_path, first_row):
    """A real row on line 2 holds a digit or Chinese text, so the English-label rule never drops it."""
    members = empty_details(**{"a_lvr_land_a_build.csv": (HEADER, [first_row])})
    _, _, converted = build(tmp_path, [{"serial": "S1"}], members)
    rows = dataset(converted, "detail")
    assert len(rows) == 1 and rows[0]["source_row_number"] == 2


def test_run_full_report_has_detail_link_and_detail_rows(tmp_path):
    raw, _ = make_source(tmp_path, [{"serial": "S1"}, {"serial": "S2"}], batch="115q1", categories=("sales",))
    add_members(tmp_path, {**all_details(), "a_lvr_land_a_land.csv": detail([["S1", "l1", ""], ["ZZ", "l2", ""]])})
    manifest = tmp_path / "raw_manifest.json"
    manifest.write_text(inventory_json(verified_source(write_raw_manifest(raw, manifest))) + "\n", encoding="utf-8")
    address_dir, descriptor = source(tmp_path)
    use_synthetic_admin_towns(tmp_path, address_dir, descriptor)
    descriptor_path = tmp_path / "address_source.json"
    descriptor_path.write_text(json.dumps(verified_source(descriptor), ensure_ascii=False), encoding="utf-8")
    result = run_full(run_id="detail1", cutoff=202610, address_dir=address_dir, runs_root=tmp_path / "runs",
                      raw_dir=raw, manifest_path=manifest, address_source=descriptor_path, rules_path=rules(tmp_path),
                      batch_rows=2)
    assert result["completed"] is True, result["failures"]
    report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
    assert report["rows"]["detail_rows"] == 3 + 1 + 1
    assert entry(report, "build")["linked_rows"] == 3
    assert counters(report, "land") == {**ZERO, "detail_unmatched": 1, "linked_rows": 1}
    assert counters(report, "park") == {**ZERO, "linked_rows": 1}
