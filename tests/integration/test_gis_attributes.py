"""GIS attribute contract and yearly point CSVs (synthetic data only)."""

import csv
import json
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lvr_pipeline import export, packaging
from lvr_pipeline.addresses.parse import _COUNTY_CODE
from lvr_pipeline.export import attributes, county_letters, parse_float, parse_int, roc_date
from lvr_pipeline.packaging import package_output, verify_output
from lvr_pipeline.sources import sha256_file
from test_p2_offline import run

NOTICES = {"publication_authorized": True, "sources": ["synthetic"]}
HEADER = ["鄉鎮市區", "建物型態", "單價元平方公尺"]
RECORDS = [
    {"amount": "5000000", "extra": ["中正區", "住宅大樓", "487804"]},
    {"extra": ["中正區", "", "abc"]},
    {"address": "臺北市中正區測試路10、11號", "amount": "6000000", "extra": ["中正區", "公寓", "100"]},
    {"address": "臺北市中正區測試路10、99號", "extra": ["中正區", "公寓", "100"]},
    {"address": "臺北市中正區測試路88、99號", "extra": ["中正區", "公寓", "100"]},
]
COORDS = [
    ("臺北市中正區測試路10號之1", "63", "6300100", 121.5, 25.0),
    ("臺北市中正區測試路10號", "63", "6300100", 121.5, 25.0),
    ("臺北市中正區測試路11號", "63", "6300100", 121.6, 25.1),
]
BOM = "﻿"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gis")
    converted, *_, state = run(tmp, COORDS, RECORDS, extra_header=HEADER)
    return converted, state, tmp


@pytest.fixture
def output(built, tmp_path):
    converted, state, _ = built
    return package_output(converted, state, tmp_path / "output", notices=NOTICES)


def monthly(output, category, fmt="parquet"):
    return next(output.rglob(f"202601_{category}.{fmt}"))


def test_county_letter_table_matches_address_codes_and_scripts():
    letters = county_letters()
    assert len(letters) == 22 and letters["a"] == "臺北市" and letters["z"] == "連江縣"
    with Path("config/reference/lvr_county_letters.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [r["letter"] for r in rows] == sorted(letters)
    assert all(_COUNTY_CODE[r["county"]] == r["county_code"] for r in rows)
    sys.path.insert(0, "scripts")
    import export_kepler_csv

    assert export_kepler_csv.COUNTY_BY_LETTER == letters


@pytest.mark.parametrize(
    "value,expected",
    [("1150102", "2026-01-02"), ("1150231", None), ("115012", None), ("", None), (None, None), ("abcdefg", None)],
)
def test_trade_date_iso_or_null(value, expected):
    assert roc_date(value) == expected


@pytest.mark.parametrize("value", [None, "", " ", "abc", "1,234", "12.5", "-5", True, [], "1e3"])
def test_non_integer_prices_are_null_never_zero(value):
    assert parse_int(value) is None


def test_number_parsing_keeps_real_values():
    assert parse_int("487804") == 487804 and parse_int(" 12 ") == 12 and parse_int("100.0") == 100
    assert parse_int("0") == 0 and parse_int(str(2**63)) is None
    assert parse_float("10.25") == 10.25 and parse_float("7") == 7.0
    assert parse_float("") is None and parse_float("x") is None and parse_float(None) is None


def test_attributes_null_rules_and_point_only_coordinates():
    row = {
        "category": "sales",
        "props_json": json.dumps({"鄉鎮市區": "中正區", "總價元": "x"}),
        "tx_date_raw": "1150230",
        "member_path": "dir/a_lvr_land_a.csv",
        "raw_address": "地址",
    }
    point = {"type": "Point", "coordinates": [121.5, 25.0]}
    got = attributes(row, point)
    assert got["county"] == "臺北市" and got["district"] == "中正區" and got["address"] == "地址"
    assert got["trade_date"] is None and got["total_price"] is None and got["building_type"] is None
    assert (got["longitude"], got["latitude"]) == (121.5, 25.0)
    for shape in [None, {"type": "MultiPoint", "coordinates": [[1, 2], [3, 4]]}]:
        assert attributes(row, shape)["longitude"] is None
    broken = attributes({**row, "props_json": "not json", "member_path": "unknown.csv"}, None)
    assert broken["district"] is None and broken["county"] is None


RENT_PROPS = {"總價元": "999", "總額元": "13500", "建物移轉總面積平方公尺": "9.9",
              "建物總面積平方公尺": "115.83", "單價元平方公尺": "117"}


def _row(category, props):
    return {"category": category, "props_json": json.dumps(props), "tx_date_raw": "1130105",
            "member_path": "a_lvr_land_c.csv", "raw_address": "地址"}


def test_rent_maps_rent_total_and_building_area():
    got = attributes(_row("rent", RENT_PROPS), None)
    assert (got["total_price"], got["building_area_sqm"], got["unit_price_sqm"]) == (13500, 115.83, 117)


@pytest.mark.parametrize("category", ["sales", "presale"])
def test_sales_and_presale_keep_sale_sources(category):
    got = attributes(_row(category, RENT_PROPS), None)
    assert (got["total_price"], got["building_area_sqm"], got["unit_price_sqm"]) == (999, 9.9, 117)


def test_rent_non_numeric_is_null_and_ignores_sale_keys():
    props = {**RENT_PROPS, "總額元": "abc", "建物總面積平方公尺": "x"}
    got = attributes(_row("rent", props), None)
    assert got["total_price"] is None and got["building_area_sqm"] is None
    only_sale = attributes(_row("rent", {"總價元": "5", "建物移轉總面積平方公尺": "1"}), None)
    assert only_sale["total_price"] is None and only_sale["building_area_sqm"] is None


def test_kepler_script_agrees_with_output_rule_for_rent(tmp_path):
    sys.path.insert(0, "scripts")
    import export_kepler_csv

    shape = {"type": "Point", "coordinates": [121.5, 25.0]}
    for category, expected in [("rent", ("13500", "115.83")), ("sales", ("999", "9.9"))]:
        month = tmp_path / category / "monthly" / "2024" / "202401"
        month.mkdir(parents=True)
        base = _row(category, RENT_PROPS)
        record = {**base, "raw_record_id": "r1", "tx_yyyymm": 202401, "location_status": "complete",
                  "is_approximation": False, "unique_point_count": 1, "geometry": export.wkb(shape)}
        pq.write_table(pa.Table.from_pylist([record]), month / f"202401_{category}.parquet")
        out = tmp_path / f"{category}.csv"
        export_kepler_csv.main(["--output-root", str(tmp_path / category), "--category", category, "--out", str(out)])
        row = next(csv.DictReader(out.read_text(encoding="utf-8-sig").splitlines()))
        ours = attributes(base, shape)
        assert (row["total_price"], row["building_area_sqm"]) == expected
        assert (int(row["total_price"]), float(row["building_area_sqm"])) == (ours["total_price"], ours["building_area_sqm"])


def test_monthly_formats_carry_typed_attributes(output):
    table = pq.read_table(monthly(output, "sales"))
    types = {f.name: f.type for f in table.schema}
    assert types["total_price"] == types["unit_price_sqm"] == pa.int64()
    assert types["building_area_sqm"] == types["longitude"] == types["latitude"] == pa.float64()
    assert all(types[k] == pa.string() for k in ["trade_date", "county", "district", "address", "building_type"])
    by_address = {r["raw_address"]: r for r in table.to_pylist()}
    one = by_address["臺北市中正區測試路10號之1"]
    assert (one["trade_date"], one["county"], one["district"], one["building_type"]) == (
        "2026-01-02", "臺北市", "中正區", "住宅大樓")
    assert (one["total_price"], one["unit_price_sqm"], one["building_area_sqm"]) == (5000000, 487804, 10.25)
    assert (one["longitude"], one["latitude"]) == (121.5, 25.0) and json.loads(one["props_json"])["new_field"]
    multi = by_address["臺北市中正區測試路10、11號"]
    assert multi["longitude"] is None and multi["latitude"] is None and multi["total_price"] == 6000000
    partial = by_address["臺北市中正區測試路10、99號"]
    assert partial["location_status"] == "partial" and partial["longitude"] == 121.5
    assert partial["total_price"] is None  # fractional source price is not an integer
    none = by_address["臺北市中正區測試路88、99號"]
    assert none["longitude"] is None and none["address"] == none["raw_address"]
    nd = [json.loads(x) for x in monthly(output, "sales", "ndjson").read_text(encoding="utf-8").splitlines()]
    geo = json.loads(monthly(output, "sales", "geojson").read_text(encoding="utf-8"))["features"]
    for name in export.ATTRIBUTE_NAMES:
        assert [f["properties"][name] for f in nd] == [f["properties"][name] for f in geo]
        assert [f["properties"][name] for f in nd] == table[name].to_pylist()
    rent = pq.read_table(monthly(output, "rent")).to_pylist()
    # fixture rent rows carry 總額元 and 建物總面積平方公尺 (10.25); these are the rent sources
    assert {r["total_price"] for r in rent} == {5000000, 6000000, None}
    assert {r["building_area_sqm"] for r in rent} == {10.25}


def test_yearly_points_only_fully_located_single_points(output):
    manifest = verify_output(output)
    assert manifest["gis_attribute_contract"] == "1.0"
    items = [a for a in manifest["assets"] if a["kind"] == "annual_points"]
    assert sorted(a["category"] for a in items) == ["presale", "rent", "sales"]
    for item in items:
        path = output / item["path"]
        assert item["path"] == f"yearly/2026/2026_{item['category']}_points.csv"
        raw = path.read_bytes()
        assert not raw.startswith(BOM.encode()) and b"\r" not in raw
        assert item["bytes"] == len(raw) and item["sha256"] == sha256_file(path)
        rows = list(csv.DictReader(raw.decode("utf-8").splitlines()))
        assert list(rows[0]) == export.POINT_COLUMNS and item["columns"] == export.POINT_COLUMNS
        assert len(rows) == item["rows"] == 2
        assert all(r["address"] == "臺北市中正區測試路10號之1" for r in rows)
        assert all(r["location_status"] == "complete" for r in rows)
        assert all((r["longitude"], r["latitude"]) == ("121.5", "25.0") for r in rows)
    sales = next(a for a in items if a["category"] == "sales")
    table = pq.read_table(monthly(output, "sales")).to_pylist()
    singles = [r for r in table if r["location_status"] == "complete" and r["longitude"] is not None]
    assert len(singles) == sales["rows"] == 2
    assert sales["path"] in manifest["years"][0]["points"]
    with (output / sales["path"]).open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert sorted(r["raw_record_id"] for r in rows) == sorted(r["raw_record_id"] for r in singles)
    full = next(r for r in rows if r["building_type"])
    assert full["total_price"] == "5000000" and full["building_area_sqm"] == "10.25"
    blank = next(r for r in rows if not r["building_type"])
    assert blank["unit_price_sqm"] == "" and blank["total_price"] == ""  # null, never 0


def retamper(output, item, text):
    path = output / item["path"]
    path.write_bytes(text.encode("utf-8"))
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    entry = next(a for a in manifest["assets"] if a["path"] == item["path"])
    entry["bytes"], entry["sha256"] = path.stat().st_size, sha256_file(path)
    packaging.write_json(output / "manifest.json", manifest)


def points_item(output, category="sales"):
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    return next(a for a in manifest["assets"] if a["kind"] == "annual_points" and a["category"] == category)


def test_points_file_tampering_is_detected(output):
    item = points_item(output)
    original = (output / item["path"]).read_text(encoding="utf-8")
    (output / item["path"]).write_text(original + "x", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        verify_output(output)
    header, row = original.splitlines()[:2]
    retamper(output, item, header + "\n")
    with pytest.raises(ValueError, match="row count|content"):
        verify_output(output)
    retamper(output, item, header + "\n" + row.replace("5000000", "5000001") + "\n")
    with pytest.raises(ValueError, match="content differs"):
        verify_output(output)
    retamper(output, item, BOM + original)
    with pytest.raises(ValueError, match="BOM"):
        verify_output(output)
    retamper(output, item, original)
    verify_output(output)


def test_missing_points_asset_is_detected(output):
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    manifest["assets"] = [
        a for a in manifest["assets"] if not (a["kind"] == "annual_points" and a["category"] == "rent")
    ]
    packaging.write_json(output / "manifest.json", manifest)
    with pytest.raises(ValueError, match="differ"):
        verify_output(output)


def test_points_file_must_fit_asset_limit(built, tmp_path):
    converted, state, _ = built
    with pytest.raises(ValueError, match="points file exceeds"):
        package_output(converted, state, tmp_path / "small", notices=NOTICES, max_asset_bytes=300)


class NoPoints:
    def __init__(self, staging):
        pass

    def add(self, *args):
        pass

    def close(self):
        pass

    def assets(self, limit):
        return []


def test_old_contract_output_still_verifies(built, tmp_path, monkeypatch):
    converted, state, _ = built
    monkeypatch.setattr(export, "ATTRIBUTE_FIELDS", [])
    monkeypatch.setattr(export, "attributes", lambda row, shape: {})
    monkeypatch.setattr(packaging, "PointsWriter", NoPoints)
    monkeypatch.setattr(packaging, "GIS_CONTRACT", None)
    old = package_output(converted, state, tmp_path / "old", notices=NOTICES)
    monkeypatch.undo()
    manifest = json.loads((old / "manifest.json").read_text(encoding="utf-8"))
    manifest.pop("gis_attribute_contract")
    packaging.write_json(old / "manifest.json", manifest)
    assert "trade_date" not in pq.read_table(monthly(old, "sales")).schema.names
    assert not list(old.rglob("*_points.csv"))
    assert verify_output(old)["snapshot_id"] == manifest["snapshot_id"]
    manifest["gis_attribute_contract"] = "1.0"
    packaging.write_json(old / "manifest.json", manifest)
    with pytest.raises(ValueError, match="lacks the declared"):
        verify_output(old)
