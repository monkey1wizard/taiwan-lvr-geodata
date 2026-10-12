"""R07-8a: door-row output in GeoParquet, GeoPackage, CSV and GeoJSON. Synthetic data only."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import re
import sqlite3
import struct
import sys
import xml.etree.ElementTree as ET

import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lvr_pipeline.addresses.pool import build_pool  # noqa: E402
from lvr_pipeline.offline.index import build_index  # noqa: E402
from lvr_pipeline.output import rows as rows_module  # noqa: E402
from lvr_pipeline.output.build import write_output  # noqa: E402
from lvr_pipeline.output.rows import iter_door_rows, make_transaction_id  # noqa: E402
from lvr_pipeline.results.reconcile import resolve_offline  # noqa: E402
from test_p1_conversion import add_members, make_source  # noqa: E402
from test_p2_offline import source  # noqa: E402
from test_r07_7_details import all_details, stages  # noqa: E402

CONTRACT = Path(__file__).resolve().parents[2] / "docs" / "輸出欄位.md"
ROAD = "臺北市中正區測試路"
EXTRA_HEADER = ["鄉鎮市區", "建物型態", "單價元平方公尺", "車位類別", "車位總價元", "車位總額元",
                "車位移轉總面積平方公尺", "車位面積平方公尺"]
EXTRA = ["中正區", "住宅大樓", "50000", "坡道平面", "100000", "7000", "12.5", "9.5"]
MULTI_A = f"{ROAD}10號之1、10號之2、10號之3、10號之4"
MULTI_B = f"{ROAD}20號之1、20號之2、20號之3、20號之4"
SINGLE = f"{ROAD} 10 號之 1 二樓"
RECORDS = [
    {"address": MULTI_A, "serial": "S1", "date": "1150102", "amount": "3250000", "extra": EXTRA},
    {"address": MULTI_B, "serial": "S2", "date": "1150215", "amount": "3250000", "extra": EXTRA},
    {"address": SINGLE, "serial": "S3", "date": "1141230", "amount": "3250000", "extra": EXTRA},
]
# S1 doors all located, S2 doors 1 and 2 located, the single door is 10號之1 of S1.
COORDINATES = [(f"{ROAD}10號之{n}", "63", "6300100", 121.5 + n / 100, 25.0 + n / 100) for n in (1, 2, 3, 4)] + [
    (f"{ROAD}20號之{n}", "63", "6300100", 121.6 + n / 100, 25.1 + n / 100) for n in (1, 2)
]
CATEGORIES = ("sales", "presale", "rent")


def contract_columns():
    """(name, type) rows of the contract table 欄位, read from docs/輸出欄位.md."""
    text = CONTRACT.read_text(encoding="utf-8")
    section = text.split("\n## 欄位\n", 1)[1].split("\n## 明細關聯契約", 1)[0]
    found = re.findall(r"^\| (\d+) \| `(\w+)` \| (\w+) \|", section, flags=re.M)
    assert [int(n) for n, _, _ in found] == list(range(1, len(found) + 1))
    return [(name, kind) for _, name, kind in found]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("r07_8")
    raw, _ = make_source(tmp, RECORDS, extra_header=EXTRA_HEADER)
    converted = stages(tmp, raw, add_members(tmp, all_details()))[2]
    root, descriptor = source(tmp, COORDINATES)
    index = build_index(root, descriptor, tmp / "work", counties=["63"])
    pool = build_pool(converted, root, descriptor, tmp / "work", index_path=index)
    state = resolve_offline(pool, index, tmp / "work")
    public, release = tmp / "public", tmp / "release"
    summary = write_output(converted, state, public, release)
    return {"converted": converted, "state": state, "public": public, "release": release, "summary": summary,
            "rows": list(iter_door_rows(converted, state))}


def only(rows, **match):
    return [r for r in rows if all(r[k] == v for k, v in match.items())]


def keys(rows):
    return {(r["transaction_id"], r["door_index"]) for r in rows}


def read_parquet(built):
    return pq.read_table(built["release"] / "taiwanlvr.parquet")


def gpkg_rows(path, layer):
    db = sqlite3.connect(path)
    try:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(f'SELECT * FROM "{layer}" ORDER BY fid')]
    finally:
        db.close()


def csv_rows(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def test_column_names_order_and_types_follow_contract(built):
    columns = contract_columns()
    names = [n for n, _ in columns]
    kinds = {"string": "string", "int64": "int64", "double": "double", "geometry": "binary"}
    table = read_parquet(built)
    assert table.schema.names == names
    assert [str(f.type) for f in table.schema] == [kinds[k] for _, k in columns]
    # GeoPackage: fid, geom, then the attributes in contract order
    gpkg = next(built["public"].glob("gpkg/*.gpkg"))
    db = sqlite3.connect(gpkg)
    info = db.execute('PRAGMA table_info("sales")').fetchall()
    db.close()
    attributes = [(n, k) for n, k in columns if n != "geometry"]
    assert [r[1] for r in info] == ["fid", "geom"] + [n for n, _ in attributes]
    sql = {"string": "TEXT", "int64": "INTEGER", "double": "REAL"}
    assert [r[2] for r in info][2:] == [sql[k] for _, k in attributes]
    # CSV: geometry becomes longitude and latitude at the same position
    csv_names = [x for n in names for x in (["longitude", "latitude"] if n == "geometry" else [n])]
    first = next(built["public"].glob("csv/*/taiwanlvr_????_sales.csv"))
    assert first.read_text(encoding="utf-8").split("\n", 1)[0] == ",".join(csv_names)
    assert len(csv_names) == len(names) + 1
    # GeoJSON: properties are the non-geometry attributes
    feature = json.loads(next(built["public"].glob("geojson/*/*sales.geojson")).read_text(encoding="utf-8"))["features"][0]
    assert list(feature["properties"]) == [n for n, _ in attributes]
    # dict rows carry the same names plus longitude and latitude
    assert set(built["rows"][0]) == set(names) - {"geometry"} | {"longitude", "latitude"}


def test_four_located_doors_give_four_rows_with_shared_transaction_values(built):
    rows = only(built["rows"], category="sales", transaction_addresses=MULTI_A)
    assert [r["door_index"] for r in rows] == [1, 2, 3, 4]
    assert {r["transaction_id"] for r in rows} == {"115q1/a_lvr_land_a.csv/3"}
    assert {r["door_count"] for r in rows} == {4}
    assert [r["address"] for r in rows] == [f"{ROAD}10號之{n}" for n in (1, 2, 3, 4)]
    assert {r["transaction_addresses"] for r in rows} == {MULTI_A}
    assert {r["price_twd"] for r in rows} == {3250000}
    assert [r["longitude"] for r in rows] == [121.5 + n / 100 for n in (1, 2, 3, 4)]
    assert {r["unit_price_sqm"] for r in rows} == {50000}


def test_partial_location_keeps_door_count_and_drops_unlocated_doors(built):
    rows = only(built["rows"], category="sales", transaction_addresses=MULTI_B)
    assert [r["door_index"] for r in rows] == [1, 2]
    assert {r["door_count"] for r in rows} == {4}
    assert [r["address"] for r in rows] == [f"{ROAD}20號之{n}" for n in (1, 2)]


def test_single_door_uses_raw_address_text(built):
    rows = only(built["rows"], category="sales", transaction_addresses=SINGLE)
    assert len(rows) == 1 and rows[0]["door_count"] == 1 and rows[0]["door_index"] == 1
    assert rows[0]["address"] == SINGLE == rows[0]["transaction_addresses"]


def test_money_area_parking_and_rent_columns(built):
    sales = only(built["rows"], category="sales", transaction_addresses=SINGLE)[0]
    rent = only(built["rows"], category="rent", transaction_addresses=SINGLE)[0]
    assert sales["trade_date"] == "2025-12-30" and sales["county"] == "臺北市" and sales["district"] == "中正區"
    assert sales["transaction_target"] == "房地(土地+建物)" and sales["building_type"] == "住宅大樓"
    assert sales["area_m2_decimal"] == 10.25 and sales["unit_price_sqm"] == 50000
    assert sales["parking_type"] == "坡道平面" and sales["parking_price_twd"] == 100000
    assert sales["parking_area_m2"] == 12.5
    # rent reads 車位總額元 and 車位面積平方公尺 only when the sale name is absent; the first present name wins
    assert rent["parking_price_twd"] == 7000 and rent["parking_area_m2"] == 12.5
    assert sales["props_json"] == json.dumps(json.loads(sales["props_json"]), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_details_are_ordered_arrays_identical_on_all_rows(built):
    rows = only(built["rows"], category="sales", transaction_addresses=MULTI_A)
    for column, kind, count in [("build_json", "b", 3), ("land_json", "l", 2), ("park_json", "p", 1)]:
        assert len({r[column] for r in rows}) == 1
        parsed = json.loads(rows[0][column])
        assert [x["棟別"] for x in parsed] == [f"{kind}{n}" for n in range(1, count + 1)]
        assert rows[0][column] == json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    for r in only(built["rows"], category="sales", transaction_addresses=MULTI_B) + only(built["rows"], category="rent"):
        assert (r["build_json"], r["land_json"], r["park_json"]) == ("[]", "[]", "[]")


def test_order_and_unknown_order_name(built):
    rows = built["rows"]
    order = [(r["trade_date"], r["category"], r["transaction_id"], r["door_index"]) for r in rows]
    assert order == sorted(order)
    assert len(rows) == 21
    with pytest.raises(ValueError, match="order_by"):
        next(iter_door_rows(built["converted"], built["state"], order_by=("nope",)))


def test_all_formats_hold_the_same_rows(built):
    expected = keys(built["rows"])
    assert len(expected) == 21
    table = read_parquet(built)
    assert table.num_rows == 21
    assert {(a, b) for a, b in zip(table["transaction_id"].to_pylist(), table["door_index"].to_pylist())} == expected
    gpkg_keys, yearly_keys, monthly_csv, monthly_json = set(), set(), set(), set()
    for path in built["public"].glob("gpkg/*.gpkg"):
        for layer in CATEGORIES:
            gpkg_keys |= keys(gpkg_rows(path, layer))
    for path in built["public"].glob("csv/*/taiwanlvr_????_*.csv"):
        yearly_keys |= {(r["transaction_id"], int(r["door_index"])) for r in csv_rows(path)}
    for path in built["public"].glob("csv/*/taiwanlvr_??????_*.csv"):
        monthly_csv |= {(r["transaction_id"], int(r["door_index"])) for r in csv_rows(path)}
    for path in built["public"].glob("geojson/*/*.geojson"):
        for f in json.loads(path.read_text(encoding="utf-8"))["features"]:
            monthly_json.add((f["properties"]["transaction_id"], f["properties"]["door_index"]))
    assert gpkg_keys == yearly_keys == monthly_csv == monthly_json == expected
    summary = built["summary"]
    assert summary["total_rows"] == 21
    by_format = {}
    for info in summary["files"].values():
        by_format[info["format"]] = by_format.get(info["format"], 0) + info["rows"]
    assert by_format == {"geoparquet": 21, "gpkg": 21, "geojson": 21, "csv": 42}
    assert "taiwanlvr.parquet" in summary["files"] and "gpkg/taiwanlvr_2026.gpkg" in summary["files"]
    month = summary["files"]["csv/2026/taiwanlvr_202601_sales.csv"]
    assert (month["year"], month["month"], month["category"], month["rows"]) == (2026, 1, "sales", 4)
    # no file for a month and category without rows, and file names use two-digit months
    assert not list(built["public"].glob("*/*/taiwanlvr_202603_*"))
    assert sorted(p.name for p in built["public"].glob("csv/2025/*")) == sorted(
        [f"taiwanlvr_2025_{c}.csv" for c in CATEGORIES] + [f"taiwanlvr_202512_{c}.csv" for c in CATEGORIES]
    )


def test_geoparquet_metadata_and_sort_order(built):
    table = read_parquet(built)
    geo = json.loads(table.schema.metadata[b"geo"])
    column = geo["columns"]["geometry"]
    assert geo["version"] == "1.1.0" and geo["primary_column"] == "geometry"
    assert column["encoding"] == "WKB" and column["geometry_types"] == ["Point"]
    assert column["crs"]["id"] == {"authority": "OGC", "code": "CRS84"}
    lngs = [r["longitude"] for r in built["rows"]]
    lats = [r["latitude"] for r in built["rows"]]
    assert column["bbox"] == [min(lngs), min(lats), max(lngs), max(lats)]
    first = table["geometry"][0].as_py()
    assert struct.unpack("<BIdd", first)[0:2] == (1, 1)
    order = list(zip(table["trade_date"].to_pylist(), table["category"].to_pylist(),
                     table["transaction_id"].to_pylist(), table["door_index"].to_pylist()))
    assert order == sorted(order)
    assert pq.ParquetFile(built["release"] / "taiwanlvr.parquet").metadata.row_group(0).column(0).compression == "ZSTD"


def test_gpkg_structure_geometry_and_default_style(built):
    path = built["public"] / "gpkg" / "taiwanlvr_2026.gpkg"
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA application_id").fetchone()[0] == 0x47504B47
    assert db.execute("PRAGMA user_version").fetchone()[0] == 10400
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert {r[0] for r in db.execute("SELECT srs_id FROM gpkg_spatial_ref_sys")} == {-1, 0, 4326}
    assert "WGS 84" in db.execute("SELECT definition FROM gpkg_spatial_ref_sys WHERE srs_id=4326").fetchone()[0]
    contents = {r[0]: r for r in db.execute("SELECT table_name,data_type,srs_id,min_x,min_y,max_x,max_y FROM gpkg_contents")}
    assert set(contents) == set(CATEGORIES) and all(r[1] == "features" and r[2] == 4326 for r in contents.values())
    assert {tuple(r) for r in db.execute("SELECT table_name,column_name,geometry_type_name,srs_id,z,m FROM gpkg_geometry_columns")} == {
        (c, "geom", "POINT", 4326, 0, 0) for c in CATEGORIES
    }
    sales = [r for r in built["rows"] if r["category"] == "sales" and r["trade_date"].startswith("2026")]
    assert contents["sales"][3:] == (min(r["longitude"] for r in sales), min(r["latitude"] for r in sales),
                                     max(r["longitude"] for r in sales), max(r["latitude"] for r in sales))
    blob = db.execute('SELECT geom FROM "sales" ORDER BY fid LIMIT 1').fetchone()[0]
    assert blob[:2] == b"GP" and blob[2] == 0 and blob[3] == 0x01 and struct.unpack("<i", blob[4:8])[0] == 4326
    lng, lat = struct.unpack("<dd", blob[8 + 5:])
    assert blob[8:13] == struct.pack("<BI", 1, 1)
    first = gpkg_rows(path, "sales")[0]
    assert first["transaction_id"] == sales[0]["transaction_id"] and (lng, lat) == (sales[0]["longitude"], sales[0]["latitude"])
    styles = db.execute("SELECT f_table_name,f_geometry_column,styleName,styleQML,useAsDefault FROM layer_styles").fetchall()
    assert sorted(s[0] for s in styles) == sorted(CATEGORIES)
    columns = [r[1] for r in db.execute("PRAGMA table_info(layer_styles)")]
    assert columns == ["id", "f_table_catalog", "f_table_schema", "f_table_name", "f_geometry_column", "styleName",
                       "styleQML", "styleSLD", "useAsDefault", "description", "owner", "ui", "update_time"]
    for _, geometry, _, qml, default in styles:
        assert geometry == "geom" and default == 1
        root = ET.fromstring(qml.split("\n", 1)[1])
        assert root.tag == "qgis" and root.get("version") == "3.34"
        filters = [r.get("filter") for r in root.iter("rule")]
        assert filters == ['"door_count" > 1', "ELSE", '"door_count" > 1']
        text = root.find("labeling").find("rules").find("rule").find("settings").find("text-style")
        assert text.get("fieldName") == "\"door_index\" || '/' || \"door_count\""
    db.close()


def test_geojson_has_no_crs_and_feature_ids(built):
    path = built["public"] / "geojson" / "2026" / "taiwanlvr_202601_sales.geojson"
    collection = json.loads(path.read_text(encoding="utf-8"))
    assert collection["type"] == "FeatureCollection" and "crs" not in collection
    assert [f["id"] for f in collection["features"]] == [f"115q1/a_lvr_land_a.csv/3#{n}" for n in (1, 2, 3, 4)]
    feature = collection["features"][1]
    assert feature["geometry"] == {"type": "Point", "coordinates": [121.52, 25.02]}
    assert "id" not in feature["properties"] and feature["properties"]["door_index"] == 2
    assert path.read_bytes().endswith(b"\n")


def test_csv_has_no_bom_by_default_and_bom_on_request(built, tmp_path):
    path = built["public"] / "csv" / "2026" / "taiwanlvr_2026_sales.csv"
    data = path.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf") and b"\r" not in data
    rows = csv_rows(path)
    assert len(rows) == 6 and rows[0]["longitude"] == "121.51" and rows[0]["price_twd"] == "3250000"
    result = write_output(built["converted"], built["state"], tmp_path / "p", tmp_path / "r", bom=True)
    assert result["total_rows"] == 21
    assert (tmp_path / "p" / "csv" / "2026" / "taiwanlvr_2026_sales.csv").read_bytes().startswith(b"\xef\xbb\xbf")


def test_non_empty_target_is_refused_and_never_overwritten(built, tmp_path):
    with pytest.raises(FileExistsError):
        write_output(built["converted"], built["state"], built["public"], tmp_path / "r")
    assert not (tmp_path / "r").exists() or not any((tmp_path / "r").iterdir())
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "keep.txt").write_text("keep")
    with pytest.raises(FileExistsError):
        write_output(built["converted"], built["state"], tmp_path / "p2", tmp_path / "busy")
    assert (tmp_path / "busy" / "keep.txt").read_text() == "keep"


def test_transaction_id_validation():
    assert make_transaction_id("115q1", "a_lvr_land_a.csv", 6076) == "115q1/a_lvr_land_a.csv/6076"
    for args in [("115q1", "dir/a.csv", 1), ("115q5", "a.csv", 1), ("2026q1", "a.csv", 1), ("115q1", "a.csv", 0),
                 ("115q1", "a.csv", True), ("115q1", "", 1), ("115q1", "a.csv", "3")]:
        with pytest.raises(ValueError):
            make_transaction_id(*args)
    assert rows_module.parse_int("1234.56") is None and rows_module.parse_int("1234") == 1234


def test_county_letter_table_matches_address_codes():
    """Restored from the deleted test_gis_attributes.py against the new row builder."""
    import csv
    from lvr_pipeline.addresses.parse import _COUNTY_CODE
    from lvr_pipeline.output.rows import county_letters, county_of

    letters = county_letters()
    assert len(letters) == 22 and letters["a"] == "臺北市" and letters["z"] == "連江縣"
    with Path("config/reference/lvr_county_letters.csv").open(encoding="utf-8", newline="") as stream:
        table = list(csv.DictReader(stream))
    assert [r["letter"] for r in table] == sorted(letters)
    assert all(_COUNTY_CODE[r["county"]] == r["county_code"] for r in table)
    assert county_of("a_lvr_land_a.csv") == "臺北市"
