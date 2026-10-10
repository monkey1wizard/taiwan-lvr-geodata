"""R07-2 output contract: three-format parity, null geometry, zero-area guard, partial marks, money once.

Negative tests call ``verify_month`` directly on tampered copies of one month's three files.
verify_month takes a ``{"geoparquet", "geojson", "ndjson"}`` path dict, so no manifest or
hash rewriting is needed and the earlier SHA-256 check of verify_output is not involved.
Synthetic data only.
"""

import json
import shutil

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lvr_pipeline.output.monthly import feature, read_wkb, wkb
from lvr_pipeline.output.package import package_output
from lvr_pipeline.output.verify import verify_month, verify_output
from lvr_pipeline.storage.runs import canonical_json
from test_p2_offline import run

NOTICES = {"publication_authorized": True, "sources": ["synthetic"]}
FORMATS = {"geoparquet": "parquet", "geojson": "geojson", "ndjson": "ndjson"}
CATEGORIES = ("sales", "presale", "rent")
HEADER = ["鄉鎮市區", "建物型態", "單價元平方公尺"]
PRICES = {"single": "5000000", "bbox": "6000000", "same_lng": "7000000", "same_lat": "8000000"}
RECORDS = [
    {"amount": PRICES["single"], "address": "臺北市中正區測試路10號之1", "extra": ["中正區", "住宅大樓", "1"]},
    {"amount": PRICES["bbox"], "address": "臺北市中正區測試路10、11號", "extra": ["中正區", "公寓", "2"]},
    {"amount": PRICES["same_lng"], "address": "臺北市中正區測試路10、12號", "extra": ["中正區", "公寓", "3"]},
    {"amount": PRICES["same_lat"], "address": "臺北市中正區測試路10、13號", "extra": ["中正區", "公寓", "4"]},
    {"amount": "9000000", "address": "臺北市中正區測試路10、99號", "extra": ["中正區", "公寓", "5"]},
    {"amount": "1000000", "address": "臺北市中正區測試路88、99號", "extra": ["中正區", "公寓", "6"]},
]
COORDS = [
    ("臺北市中正區測試路10號之1", "63", "6300100", 121.5, 25.0),
    ("臺北市中正區測試路10號", "63", "6300100", 121.5, 25.0),
    ("臺北市中正區測試路11號", "63", "6300100", 121.6, 25.1),
    ("臺北市中正區測試路12號", "63", "6300100", 121.5, 25.2),  # same longitude as 10號
    ("臺北市中正區測試路13號", "63", "6300100", 121.7, 25.0),  # same latitude as 10號
]
SINGLE, BBOX, SAME_LNG, SAME_LAT, PARTIAL, NONE = (r["address"] for r in RECORDS)
HEAD = '{"type":"FeatureCollection","features":[\n'


@pytest.fixture(scope="module")
def output(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("r07_2")
    converted, *_, state = run(tmp, COORDS, RECORDS, extra_header=HEADER)
    return package_output(converted, state, tmp / "output", notices=NOTICES)


def month_paths(output, category):
    return {fmt: next(output.rglob(f"202601_{category}.{ext}")) for fmt, ext in FORMATS.items()}


def parquet_rows(path):
    return pq.read_table(path).to_pylist()


def ndjson(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def geojson(path):
    return json.loads(path.read_text(encoding="utf-8"))["features"]


def by_address(rows):
    return {r["raw_address"]: r for r in rows}


def rewrite(paths, rows, schema):
    """Write rows to all three formats consistently."""
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), paths["geoparquet"], compression="zstd")
    lines = [canonical_json(feature(r)) for r in rows]
    paths["ndjson"].write_text("".join(x + "\n" for x in lines), encoding="utf-8", newline="\n")
    paths["geojson"].write_text(HEAD + ",\n".join(lines) + "\n]}\n", encoding="utf-8", newline="\n")


# ---------------------------------------------------------------- positive contract


@pytest.mark.parametrize("category", CATEGORIES)
def test_a_ids_identical_in_order_across_formats(output, category):
    p = month_paths(output, category)
    ids = [r["raw_record_id"] for r in parquet_rows(p["geoparquet"])]
    assert len(ids) == len(RECORDS) and len(set(ids)) == len(ids)
    assert [f["id"] for f in geojson(p["geojson"])] == ids
    assert [f["id"] for f in ndjson(p["ndjson"])] == ids


@pytest.mark.parametrize("category", CATEGORIES)
def test_b_all_properties_identical_across_formats(output, category):
    p = month_paths(output, category)
    rows = parquet_rows(p["geoparquet"])
    expected = [{k: v for k, v in r.items() if k != "geometry"} for r in rows]
    nd, geo = ndjson(p["ndjson"]), geojson(p["geojson"])
    assert [f["properties"] for f in nd] == expected
    assert [f["properties"] for f in geo] == expected
    assert all(set(f["properties"]) == set(expected[0]) for f in nd + geo)  # JSON key order is canonical-sorted
    assert nd == geo


@pytest.mark.parametrize("category", CATEGORIES)
def test_c_row_counts_equal_across_formats_and_manifest(output, category):
    p = month_paths(output, category)
    counts = {
        len(parquet_rows(p["geoparquet"])),
        len(ndjson(p["ndjson"])),
        len(geojson(p["geojson"])),
    }
    assert counts == {len(RECORDS)}
    manifest = verify_output(output)
    assert manifest["months"][0]["categories"][category]["rows"] == len(RECORDS)
    assert verify_month(p)["rows"] == len(RECORDS)
    assert manifest["retained_rows"] == len(RECORDS) * 3


@pytest.mark.parametrize("category", CATEGORIES)
def test_d_unlocated_observation_has_null_geometry_in_all_formats(output, category):
    p = month_paths(output, category)
    row = by_address(parquet_rows(p["geoparquet"]))[NONE]
    assert row["geometry"] is None and row["location_status"] == "none"
    assert row["longitude"] is None and row["latitude"] is None
    nd = next(f for f in ndjson(p["ndjson"]) if f["properties"]["raw_address"] == NONE)
    assert "geometry" in nd and nd["geometry"] is None
    raw_lines = p["geojson"].read_text(encoding="utf-8").splitlines()
    assert any('"geometry":null' in x and "88、99" in x for x in raw_lines)
    geo = next(f for f in geojson(p["geojson"]) if f["properties"]["raw_address"] == NONE)
    assert "geometry" in geo and geo["geometry"] is None
    nulls = sum(r["geometry"] is None for r in parquet_rows(p["geoparquet"]))
    assert nulls == 1 == sum(f["geometry"] is None for f in ndjson(p["ndjson"]))


@pytest.mark.parametrize("category", CATEGORIES)
def test_e_shared_axis_points_are_multipoint_and_no_polygon_is_zero_area(output, category):
    p = month_paths(output, category)
    rows = by_address(parquet_rows(p["geoparquet"]))
    for address in (SAME_LNG, SAME_LAT):
        shape = read_wkb(rows[address]["geometry"])
        assert shape["type"] == "MultiPoint" and rows[address]["is_approximation"] is False
        assert rows[address]["unique_point_count"] == 2
    lngs = {c[0] for c in read_wkb(rows[SAME_LNG]["geometry"])["coordinates"]}
    lats = {c[1] for c in read_wkb(rows[SAME_LAT]["geometry"])["coordinates"]}
    assert len(lngs) == 1 and len(lats) == 1
    polygons = 0
    for shapes in (
        [read_wkb(r["geometry"]) for r in parquet_rows(p["geoparquet"])],
        [f["geometry"] for f in ndjson(p["ndjson"])],
        [f["geometry"] for f in geojson(p["geojson"])],
    ):
        for shape in shapes:
            if shape and shape["type"] == "Polygon":
                polygons += 1
                xs = [c[0] for c in shape["coordinates"][0]]
                ys = [c[1] for c in shape["coordinates"][0]]
                assert max(xs) - min(xs) > 0 and max(ys) - min(ys) > 0
    # a real bbox is a Polygon only outside rent; rent never gets one
    assert polygons == (0 if category == "rent" else 3)
    assert read_wkb(rows[BBOX]["geometry"])["type"] == ("MultiPoint" if category == "rent" else "Polygon")


@pytest.mark.parametrize("category", CATEGORIES)
def test_f_partial_member_location_is_marked(output, category):
    p = month_paths(output, category)
    row = by_address(parquet_rows(p["geoparquet"]))[PARTIAL]
    assert row["location_status"] == "partial"
    assert 0 < row["located_component_count"] < row["address_component_count"]
    assert read_wkb(row["geometry"])["type"] == "Point" and row["is_approximation"] is False
    for feat in ndjson(p["ndjson"]) + geojson(p["geojson"]):
        if feat["properties"]["raw_address"] == PARTIAL:
            assert feat["properties"]["location_status"] == "partial"
            assert feat["properties"]["is_approximation"] is False
    assert verify_month(p)["partial_geometry"] == 1
    for r in parquet_rows(p["geoparquet"]):
        if r["is_approximation"]:
            assert read_wkb(r["geometry"])["type"] == "Polygon"


def test_g_multi_door_money_counted_once(output):
    manifest = verify_output(output)
    total = sum(int(r["amount"]) * 100 for r in RECORDS)
    for category in CATEGORIES:
        rows = by_address(parquet_rows(month_paths(output, category)["geoparquet"]))
        for address, price in [(BBOX, "bbox"), (SAME_LNG, "same_lng"), (SAME_LAT, "same_lat")]:
            row = rows[address]
            assert row["address_component_count"] == 2
            assert row["total_price"] == int(PRICES[price])  # source value, not x2
            assert row["amount_minor"] == int(PRICES[price]) * 100  # minor units, once
        assert sum(r["amount_minor"] or 0 for r in rows.values()) == total
        assert manifest["months"][0]["categories"][category]["amount_minor_sum"] == total


# ---------------------------------------------------------------- negative: verify_month


@pytest.fixture
def tampered(output, tmp_path):
    """Copy the sales month; return (paths, tamper) where tamper rewrites all formats consistently."""
    paths = {}
    for fmt, path in month_paths(output, "sales").items():
        paths[fmt] = tmp_path / path.name
        shutil.copy(path, paths[fmt])
    schema = pq.ParquetFile(paths["geoparquet"]).schema_arrow

    def tamper(address, mutate):
        rows = parquet_rows(paths["geoparquet"])
        for row in rows:
            if row["raw_address"] == address:
                mutate(row)
        rewrite(paths, rows, schema)

    return paths, tamper


def test_untampered_rewrite_passes(tampered):
    paths, tamper = tampered
    tamper(SINGLE, lambda row: None)
    assert verify_month(paths)["rows"] == len(RECORDS)


def test_n8_changed_ndjson_id_fails_parity(tampered):
    paths, _ = tampered
    lines = paths["ndjson"].read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["id"] = "tampered-id"
    lines[0] = canonical_json(first)
    paths["ndjson"].write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="Output format parity mismatch"):
        verify_month(paths)


def test_n8b_changed_geojson_id_fails_parity(tampered):
    paths, _ = tampered
    text = paths["geojson"].read_text(encoding="utf-8")
    first_id = json.loads(text)["features"][0]["id"]
    paths["geojson"].write_text(text.replace(first_id, "tampered-id", 1), encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="Output format parity mismatch"):
        verify_month(paths)


def test_n9_consistently_changed_total_price_fails_attribute_check(tampered):
    paths, tamper = tampered
    # parquet, GeoJSON and NDJSON agree, so only the source-derived check can object
    tamper(SINGLE, lambda row: row.update(total_price=row["total_price"] + 1))
    with pytest.raises(ValueError, match="GIS attribute fields differ from source"):
        verify_month(paths)


def test_n9b_parquet_only_total_price_change_fails_parity(tampered):
    paths, _ = tampered
    schema = pq.ParquetFile(paths["geoparquet"]).schema_arrow
    rows = parquet_rows(paths["geoparquet"])
    rows[0]["total_price"] = 1
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), paths["geoparquet"], compression="zstd")
    with pytest.raises(ValueError, match="Output format parity mismatch"):
        verify_month(paths)


def test_n10_wrong_location_status_fails(tampered):
    paths, tamper = tampered
    tamper(PARTIAL, lambda row: row.update(location_status="complete"))
    with pytest.raises(ValueError, match="Output location status invalid"):
        verify_month(paths)


def test_n10b_null_geometry_with_located_components_fails(tampered):
    paths, tamper = tampered
    tamper(SINGLE, lambda row: row.update(geometry=None, longitude=None, latitude=None))
    with pytest.raises(ValueError, match="Output location status invalid"):
        verify_month(paths)


def test_n11_zero_width_polygon_fails(tampered):
    paths, tamper = tampered
    ring = [[121.5, 25.0], [121.5, 25.0], [121.5, 25.1], [121.5, 25.1], [121.5, 25.0]]
    tamper(BBOX, lambda row: row.update(geometry=wkb({"type": "Polygon", "coordinates": [ring]})))
    with pytest.raises(ValueError, match="Invalid approximate bbox"):
        verify_month(paths)


def test_n11b_zero_height_polygon_fails(tampered):
    paths, tamper = tampered
    ring = [[121.5, 25.0], [121.6, 25.0], [121.6, 25.0], [121.5, 25.0], [121.5, 25.0]]
    tamper(BBOX, lambda row: row.update(geometry=wkb({"type": "Polygon", "coordinates": [ring]})))
    with pytest.raises(ValueError, match="Invalid approximate bbox"):
        verify_month(paths)


def test_n11c_polygon_in_rent_fails(output, tmp_path):
    paths = {}
    for fmt, path in month_paths(output, "rent").items():
        paths[fmt] = tmp_path / path.name
        shutil.copy(path, paths[fmt])
    schema = pq.ParquetFile(paths["geoparquet"]).schema_arrow
    ring = [[121.5, 25.0], [121.6, 25.0], [121.6, 25.1], [121.5, 25.1], [121.5, 25.0]]
    rows = parquet_rows(paths["geoparquet"])
    for row in rows:
        if row["raw_address"] == BBOX:
            row["geometry"] = wkb({"type": "Polygon", "coordinates": [ring]})
            row["is_approximation"] = True
    rewrite(paths, rows, schema)
    with pytest.raises(ValueError, match="Invalid approximate bbox"):
        verify_month(paths)


def test_n12_approximate_point_fails(tampered):
    paths, tamper = tampered
    tamper(SINGLE, lambda row: row.update(is_approximation=True))
    with pytest.raises(ValueError, match="Only Polygon may be approximate"):
        verify_month(paths)


def test_n13_extra_ndjson_record_fails(tampered):
    paths, _ = tampered
    first = paths["ndjson"].read_text(encoding="utf-8").splitlines()[0]
    with paths["ndjson"].open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(first + "\n")
    with pytest.raises(ValueError, match="NDJSON has extra records"):
        verify_month(paths)


def test_n13b_geojson_trailing_content_fails_footer(tampered):
    paths, _ = tampered
    with paths["geojson"].open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("extra\n")
    with pytest.raises(ValueError, match="GeoJSON footer/row count mismatch"):
        verify_month(paths)


def test_n13c_missing_last_geojson_feature_is_rejected(tampered):
    # R07-2 finding: no explicit message; json.loads of the footer line raises JSONDecodeError (a ValueError).
    paths, _ = tampered
    lines = paths["geojson"].read_text(encoding="utf-8").splitlines()
    del lines[-2]  # last feature
    lines[-2] = lines[-2].rstrip(",")
    paths["geojson"].write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    with pytest.raises(ValueError) as caught:
        verify_month(paths)
    print("n13c", type(caught.value).__name__, caught.value)


def test_n13d_missing_last_ndjson_record_is_rejected(tampered):
    # R07-2 finding: readline() returns "" and json.loads raises JSONDecodeError (a ValueError).
    paths, _ = tampered
    lines = paths["ndjson"].read_text(encoding="utf-8").splitlines()
    paths["ndjson"].write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8", newline="\n")
    with pytest.raises(ValueError) as caught:
        verify_month(paths)
    print("n13d", type(caught.value).__name__, caught.value)
