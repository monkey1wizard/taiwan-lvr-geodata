# -*- coding: utf-8 -*-
"""B-1.1: admin_towns villages are one entry per village code and name, with the rows excluded for a wrong town."""
import hashlib
import json

from lvr_pipeline.addresses import admin
from lvr_pipeline.addresses.admin import build_admin_towns, write_admin_towns

FILES = {
    "area_1984.csv": (
        "name,dgbas_id\n高雄縣燕巢鄉,1001210\n高雄縣燕巢鄉橫山村,1001210-010\n"
        "高雄縣燕巢鄉安招村,1001210-011\n桃園縣大園鄉,1000805\n"
        "高雄縣三民鄉,1001227\n高雄縣那瑪夏鄉,1001227\n高雄縣三民鄉民生村,1001227-001\n"
    ),
    "area_2010.csv": (
        "name,dgbas_id\n燕巢區,6402100\n復興區,6801300\n"
        "燕巢區橫山里,6402100-010\n高雄市燕巢區安招里,6402100-011\n"
        "大園鄉三光里,6801300-006\n復興區三光里,6801300-006\n"
        "三民里,6801300-001\n桃園市義盛里,6801300-003\n"
    ),
    "area_2015.csv": "name,dgbas_id\n高雄市燕巢區,6402100\n高雄市燕巢區橫山里,6402100-010\n桃園市復興區,6801300\n桃園市大園區,6800500\n",
}


def synthetic(tmp_path):
    for name, text in FILES.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    return {
        "commit": "a" * 40,
        "administrative_files": [
            {"path": name, "sha256": hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()} for name in sorted(FILES)
        ],
    }


def entries(document, code):
    return [v for v in document["villages"] if v["code"] == code]


def test_old_cun_and_new_li_are_both_listed_under_the_new_county(tmp_path, monkeypatch):
    document = write_admin_towns(tmp_path, synthetic(tmp_path), tmp_path / "admin_towns.json")
    (old,) = entries(document, "1001210-010")
    assert old == {"county": "64", "town_code": "1001210", "code": "1001210-010", "name": "橫山村",
                   "old_county": "高雄縣", "sources": ["area_1984.csv"]}
    (new,) = entries(document, "6402100-010")
    assert new["county"] == "64" and new["name"] == "橫山里" and "old_county" not in new
    assert document["town_codes"]["64"]["1001210"] == ["燕巢區", "燕巢鄉"]
    monkeypatch.setattr(admin, "ADMIN_TOWNS_PATH", tmp_path / "admin_towns.json")
    admin.load_admin_towns.cache_clear()
    try:
        assert {"橫山村", "橫山里"} <= set(admin.town_villages("64", "燕巢區"))
        assert admin.town_villages("64", "燕巢鄉") == ("安招村", "橫山村")  # the old name only has the old code
    finally:
        admin.load_admin_towns.cache_clear()


def test_renamed_town_rows_are_excluded_with_reason(tmp_path):
    document = build_admin_towns(tmp_path, synthetic(tmp_path))
    assert [e["dgbas_id"] for e in document["renamed_excluded"]] == ["1001227"]
    assert not entries(document, "1001227-001")
    assert "1001227" not in document["town_codes"].get("64", {})
    assert {"source": "area_1984.csv", "name": "高雄縣三民鄉民生村", "code": "1001227-001", "reason": "renamed_town",
            "written_town": "三民鄉", "code_towns": ["三民鄉", "那瑪夏鄉"]} in document["village_excluded"]


def test_row_with_other_written_town_goes_to_village_excluded(tmp_path):
    document = build_admin_towns(tmp_path, synthetic(tmp_path))
    assert [e for e in document["village_excluded"] if e["reason"] != "renamed_town"] == [
        {"source": "area_2010.csv", "name": "大園鄉三光里", "code": "6801300-006", "reason": "town_mismatch",
         "written_town": "大園鄉", "code_towns": ["復興區"]},
    ]
    assert [v["name"] for v in entries(document, "6801300-006")] == ["三光里"]
    assert entries(document, "6801300-006")[0]["sources"] == ["area_2010.csv"]


def test_village_only_name_is_accepted_by_code(tmp_path):
    document = build_admin_towns(tmp_path, synthetic(tmp_path))
    assert [(v["town_code"], v["name"]) for v in entries(document, "6801300-001")] == [("6801300", "三民里")]
    assert [(v["county"], v["name"]) for v in entries(document, "6801300-003")] == [("68", "義盛里")]


def test_sources_are_merged_when_the_same_row_is_in_two_tables(tmp_path):
    document = build_admin_towns(tmp_path, synthetic(tmp_path))
    (entry,) = entries(document, "6402100-010")
    assert entry == {"county": "64", "town_code": "6402100", "code": "6402100-010", "name": "橫山里",
                     "sources": ["area_2010.csv", "area_2015.csv"]}
    (entry,) = entries(document, "6402100-011")
    assert entry["sources"] == ["area_2010.csv"]  # written with the county in area_2010 only


def test_villages_are_sorted_and_town_villages_follow_every_spelling(tmp_path, monkeypatch):
    document = write_admin_towns(tmp_path, synthetic(tmp_path), tmp_path / "admin_towns.json")
    codes = [(v["code"], v["name"], v["county"]) for v in document["villages"]]
    assert codes == sorted(codes)
    monkeypatch.setattr(admin, "ADMIN_TOWNS_PATH", tmp_path / "admin_towns.json")
    admin.load_admin_towns.cache_clear()
    try:
        assert admin.town_villages("64", "燕巢區") == ("安招村", "安招里", "橫山村", "橫山里")
        assert admin.town_villages("68", "復興區") == ("三光里", "三民里", "義盛里")
        assert admin.town_villages("68", "大園區") == ()
        assert admin.town_villages("64", "不存在區") == ()
    finally:
        admin.load_admin_towns.cache_clear()


def test_written_county_other_than_the_code_county_is_excluded(tmp_path):
    files = {
        "area_2010.csv": "name,dgbas_id\n燕巢區,6402100\n臺北市大安區,6300100\n燕巢區橫山里,6402100-010\n臺北市大安區和平里,6402100-011\n",
    }
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    descriptor = {"commit": "c" * 40, "administrative_files": [
        {"path": n, "sha256": hashlib.sha256((tmp_path / n).read_bytes()).hexdigest()} for n in files]}
    document = build_admin_towns(tmp_path, descriptor)
    assert [v["name"] for v in document["villages"]] == ["橫山里"]
    assert document["village_excluded"] == [
        {"source": "area_2010.csv", "name": "臺北市大安區和平里", "code": "6402100-011", "reason": "county_mismatch",
         "written_town": "臺北市大安區", "code_towns": ["燕巢區"]},
    ]


def test_generated_file_is_json_with_the_new_keys(tmp_path):
    write_admin_towns(tmp_path, synthetic(tmp_path), tmp_path / "out.json")
    document = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert {"villages", "village_excluded", "town_codes"} <= set(document)
    assert document["town_codes"]["64"] == {"1001210": ["燕巢區", "燕巢鄉"], "6402100": ["燕巢區"]}
