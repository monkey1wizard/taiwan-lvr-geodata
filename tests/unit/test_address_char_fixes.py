"""R07-6d: repair rare characters in transaction addresses with the pinned char_fixes.csv. Synthetic data only,
except one test that reads the real pinned file when C:/Code/taiwan-address-data exists."""
import hashlib
import json
from pathlib import Path

import pytest

from lvr_pipeline.addresses.address_fixes import (
    DIAGNOSTIC_CODE, apply_address_char_fixes, load_address_char_fixes, transaction_only)
from lvr_pipeline.pipeline import run_full
from lvr_pipeline.storage.runs import REBUILD_EPOCH, load_snapshot
from lvr_pipeline.transactions.normalize import normalize, normalize_record
from tests.integration.test_p1_conversion import CODE, dataset, ingest, make_source, no_char_fixes, rules, write_char_fixes

REAL_ADDRESS_DIR = Path("C:/Code/taiwan-address-data")
OWNER = "2026-10-13 擁有者確認"
OFFICIAL = "戶政司 115 年路名同鄉鎮唯一相符"
AREAS = {
    "area_1984.csv": "name,dgbas_id\n彰化縣彰化市,1000701\n彰化縣員林鎮,1000710\n桃園縣桃園市,6800100\n",
    "area_2014.csv": "name,dgbas_id\n員林市,1000710\n",
    "area_2015.csv": "name,dgbas_id\n桃園市桃園區,6800100\n",
}


def synthetic(tmp_path, rows):
    for name, text in AREAS.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    descriptor = {
        "administrative_files": [{"path": name, "sha256": hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()}
                                 for name in sorted(AREAS)],
        "char_fixes": write_char_fixes(tmp_path, rows),
    }
    return load_address_char_fixes(tmp_path, descriptor), descriptor


def fix(county, town, field, source, fixed, evidence=OFFICIAL):
    return [county, town, field, source, fixed, evidence, "2026-10-14"]


def test_same_string_in_another_town_is_not_replaced(tmp_path):
    fixes, _ = synthetic(tmp_path, [fix("10007", "1000701", "street", "?泉路", "温泉路")])
    assert apply_address_char_fixes("彰化縣彰化市?泉路10號", fixes)[0] == "彰化縣彰化市温泉路10號"
    assert apply_address_char_fixes("彰化縣員林市?泉路10號", fixes) == ("彰化縣員林市?泉路10號", [])
    # The same town name under another county is another town as well.
    assert apply_address_char_fixes("臺北市中正區?泉路10號", fixes)[1] == []


def test_black_square_fix_repairs_a_question_mark_address(tmp_path):
    fixes, _ = synthetic(tmp_path, [fix("10007", "1000701", "street", "福■路", "福北路")])
    for written in ["?", "■", "？"]:
        assert apply_address_char_fixes(f"彰化縣彰化市福{written}路1號", fixes)[0] == "彰化縣彰化市福北路1號"
    assert apply_address_char_fixes("彰化縣彰化市福南路1號", fixes)[1] == []


def test_old_county_address_is_resolved_before_matching(tmp_path):
    fixes, _ = synthetic(tmp_path, [fix("68", "6800100", "street", "中?路", "中興路")])
    address, applied = apply_address_char_fixes("桃園縣桃園市中?路5號", fixes)
    assert address == "桃園市桃園區中興路5號"
    assert applied[0]["before"] == "桃園市桃園區中?路5號" and applied[0]["after"] == "桃園市桃園區中興路5號"
    assert apply_address_char_fixes("桃園縣中壢市中?路5號", fixes)[1] == []  # another town of the same county


def test_town_code_with_two_names_matches_both(tmp_path):
    fixes, _ = synthetic(tmp_path, [fix("10007", "1000710", "lane", "田■巷", "田藔巷")])
    assert fixes.rules[0].town_names == {"員林鎮", "員林市"}
    assert apply_address_char_fixes("彰化縣員林鎮田?巷1號", fixes)[0] == "彰化縣員林鎮田藔巷1號"
    assert apply_address_char_fixes("彰化縣員林市田?巷1號", fixes)[0] == "彰化縣員林市田藔巷1號"


def test_only_street_lane_village_and_area_rows_are_used(tmp_path):
    fixes, _ = synthetic(tmp_path, [
        fix("10007", "1000701", field, f"{field}?", f"{field}字") for field in
        ["street", "lane", "village", "area", "number", "drop", "full_addr", "neighborhood", "plate_town"]])
    assert sorted(rule.field for rule in fixes.rules) == ["area", "lane", "street", "village"]
    assert fixes.counts == {"kept": 4, "excluded_by_field": 5, "excluded_transaction_only": 0,
                            "skipped_no_placeholder": 0, "unknown_town_code": 0}


def test_rows_citing_only_this_projects_transactions_are_excluded(tmp_path):
    own = "taiwan-lvr-geodata 交易資料同位置一致"
    assert transaction_only(own) and not transaction_only(own + "，" + OFFICIAL)
    for other in ["國土", "NLSC", "RIS", "2016", "擁有者"]:
        assert not transaction_only(own + other)
    assert not transaction_only("")
    fixes, _ = synthetic(tmp_path, [
        fix("10007", "1000701", "street", "甲?路", "甲乙路", own),
        fix("10007", "1000701", "street", "丙?路", "丙丁路", own + "，" + OWNER)])
    assert [rule.source_value for rule in fixes.rules] == ["丙?路"]
    assert fixes.counts["excluded_transaction_only"] == 1
    assert fixes.excluded_transaction_only == (("10007", "1000701", "street", "甲?路"),)


def test_row_without_placeholder_is_skipped_and_counted(tmp_path):
    fixes, _ = synthetic(tmp_path, [fix("10007", "1000701", "street", "中山路", "中正路"),
                                    fix("10007", "1000701", "street", "中\ue000路", "中正路")])
    assert fixes.counts["skipped_no_placeholder"] == 1 and fixes.counts["kept"] == 1
    assert apply_address_char_fixes("彰化縣彰化市中\ue000路1號", fixes)[0] == "彰化縣彰化市中正路1號"


def test_town_code_not_in_the_area_tables_is_counted_and_never_applied(tmp_path):
    fixes, _ = synthetic(tmp_path, [fix("10007", "1000799", "street", "中?路", "中正路")])
    assert fixes.counts["unknown_town_code"] == 1 and fixes.rules == ()


def test_longer_source_value_is_applied_first_and_every_match_is_replaced(tmp_path):
    fixes, _ = synthetic(tmp_path, [
        fix("10007", "1000701", "street", "?埤路", "短埤路"),
        fix("10007", "1000701", "street", "?埤路福山", "双埤路福山")])
    address, applied = apply_address_char_fixes("彰化縣彰化市?埤路福山?埤路1號", fixes)
    assert address == "彰化縣彰化市双埤路福山短埤路1號"
    assert [item["source_value"] for item in applied] == ["?埤路福山", "?埤路"]
    assert applied[0]["matches"] == 1


def test_fix_never_touches_county_or_town(tmp_path):
    fixes, _ = synthetic(tmp_path, [fix("10007", "1000701", "street", "彰?市", "X")])
    assert apply_address_char_fixes("彰化縣彰化市中山路1號", fixes)[1] == []


def test_arabic_section_in_a_fix_matches_the_normalized_address(tmp_path):
    fixes, _ = synthetic(tmp_path, [fix("10007", "1000701", "street", "三星?3段", "三星邨3段")])
    assert apply_address_char_fixes("彰化縣彰化市三星?三段1號", fixes)[0] == "彰化縣彰化市三星邨三段1號"


def test_hash_mismatch_raises(tmp_path):
    fixes, descriptor = synthetic(tmp_path, [fix("10007", "1000701", "street", "?路", "X路")])
    assert fixes.sha256 == descriptor["char_fixes"]["sha256"]
    with open(tmp_path / "char_fixes.csv", "ab") as stream:
        stream.write(b"\n")
    with pytest.raises(ValueError, match="char_fixes.csv hash mismatch"):
        load_address_char_fixes(tmp_path, descriptor)
    _, descriptor = synthetic(tmp_path, [])
    descriptor["administrative_files"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="Administrative source hash mismatch"):
        load_address_char_fixes(tmp_path, descriptor)
    with pytest.raises(ValueError, match="no char_fixes entry"):
        load_address_char_fixes(tmp_path, {k: v for k, v in descriptor.items() if k != "char_fixes"})


def raw_record(address):
    fields = {"土地位置建物門牌": address, "交易標的": "房地(土地+建物)", "交易年月日": "1150102",
              "總價元": "1000000", "建物移轉總面積平方公尺": "30.5", "編號": "S1"}
    return {"raw_record_id": "r1", "src_batch": "115q1", "member_path": "a_lvr_land_a.csv", "source_row_number": 3,
            "row_status": "parsed", "category": "sales", "input_sha256": "0" * 64, "raw_values_json": "[]",
            "raw_fields_json": json.dumps(fields, ensure_ascii=False)}


def test_applied_fix_is_recorded_as_a_diagnostic_with_its_evidence(tmp_path):
    fixes, _ = synthetic(tmp_path, [fix("10007", "1000701", "street", "福■路", "福北路", OFFICIAL)])
    output = normalize_record(raw_record("彰化縣彰化市福?路10號"), 202610, {}, [], fixes)
    assert [item["code"] for item in output["diagnostic"]] == [DIAGNOSTIC_CODE]
    assert json.loads(output["diagnostic"][0]["detail"]) == {
        "county": "10007", "town_code": "1000701", "field": "street", "source_value": "福■路",
        "fixed_value": "福北路", "evidence": OFFICIAL, "before": "彰化縣彰化市福?路10號",
        "after": "彰化縣彰化市福北路10號"}
    assert output["diagnostic"][0]["source_ref"] == "115q1/a_lvr_land_a.csv:3"
    # Evidence is not a review request: the row stays retained and the corrected address gets its door key.
    assert output["observation"][0]["parse_status"] == "retained"
    assert output["observation"][0]["raw_address"] == "彰化縣彰化市福?路10號"
    component = output["address-component"][0]
    assert component["normalized_address"] == "彰化縣彰化市福北路10號" and component["building_key"]
    assert output["disposition"][0]["reason"] == "normalized"


def test_no_diagnostic_when_nothing_applies(tmp_path):
    fixes, _ = synthetic(tmp_path, [fix("10007", "1000701", "street", "福■路", "福北路")])
    output = normalize_record(raw_record("彰化縣員林市福?路10號"), 202610, {}, [], fixes)
    assert not any(item["code"] == DIAGNOSTIC_CODE for item in output["diagnostic"])


def test_stage_is_rebuilt_when_the_fix_table_differs(tmp_path):
    raw, manifest = make_source(tmp_path, [{"address": "彰化縣彰化市福?路10號"}], categories=("sales",))
    work = tmp_path / "work"
    ingested = ingest(raw, manifest, ["115q1"], work, batch_rows=2, code_commit=CODE)
    fixes, _ = synthetic(tmp_path, [fix("10007", "1000701", "street", "福■路", "福北路")])
    first = normalize(ingested, work, cutoff=202610, rules_path=rules(tmp_path), char_fixes=fixes, batch_rows=2, code_commit=CODE)
    assert normalize(ingested, work, cutoff=202610, rules_path=rules(tmp_path), char_fixes=fixes, batch_rows=2, code_commit=CODE) == first
    assert [r["normalized_address"] for r in dataset(first, "address-component")] == ["彰化縣彰化市福北路10號"]
    other = normalize(ingested, work, cutoff=202610, rules_path=rules(tmp_path), char_fixes=no_char_fixes(), batch_rows=2, code_commit=CODE)
    assert other != first
    assert [r["normalized_address"] for r in dataset(other, "address-component")] == ["彰化縣彰化市福?路10號"]
    assert load_snapshot(first, "normalize")[1]["char_fixes_sha256"] == fixes.sha256
    assert load_snapshot(other, "normalize")[1]["char_fixes_sha256"] == no_char_fixes().sha256


def test_run_full_refuses_a_source_without_char_fixes_before_writing(tmp_path):
    manifest = tmp_path / "raw_manifest.json"
    manifest.write_text(json.dumps({"rebuild_epoch": REBUILD_EPOCH}), encoding="utf-8")
    descriptor = json.loads(Path("config/sources/address_source.json").read_text(encoding="utf-8"))
    del descriptor["char_fixes"]
    address = tmp_path / "address_source.json"
    address.write_text(json.dumps(descriptor), encoding="utf-8")
    runs = tmp_path / "runs"
    result = run_full(run_id="r1", cutoff=202610, address_dir=tmp_path, runs_root=runs, raw_dir=tmp_path,
                      manifest_path=manifest, address_source=address)
    assert result["completed"] is False
    assert [f["code"] for f in result["failures"]] == ["address_source_missing_char_fixes"]
    assert not runs.exists()


@pytest.mark.skipif(not REAL_ADDRESS_DIR.is_dir(), reason="C:/Code/taiwan-address-data is absent (CI has no address data)")
def test_real_pinned_file_excludes_exactly_the_two_transaction_only_rows():
    descriptor = json.loads(Path("config/sources/address_source.json").read_text(encoding="utf-8"))
    fixes = load_address_char_fixes(REAL_ADDRESS_DIR, descriptor)
    assert set(fixes.excluded_transaction_only) == {("10018", "1001801", "street", "光復路■段"),
                                                    ("66", "6602800", "lane", "■富巷")}
    assert fixes.counts["excluded_transaction_only"] == 2
    assert fixes.counts["unknown_town_code"] == 0
    assert sum(fixes.counts[key] for key in ["kept", "excluded_by_field", "excluded_transaction_only",
                                             "skipped_no_placeholder"]) == 449
