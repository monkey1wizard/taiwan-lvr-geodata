"""Synthetic address-base audit: five report categories, no row selection."""

import csv
import json
import math
import subprocess
import sys
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from lvr_pipeline.offline.index import EARTH_RADIUS_M, audit_address_source
from lvr_pipeline.sources import sha256_file

ROOT = Path(__file__).resolve().parents[2]
HEADER = ["FULL_ADDR", "COUNTY", "TOWN", "ROAD", "X", "Y"]
MAIN = [
    ("臺北市中正區測試路1號", "63", "6300100", "測試路", "121.5", "25.0"),
    ("臺北市中正區測試路1號", "63", "6300100", "測試路", "121.5", "25.0"),
    ("臺北市中正區測試路2號", "63", "6300100", "測試路", "121.51", "25.01"),
    ("臺北市中正區測試路２號", "63", "6300100", "測試路", "121.51", "25.01"),
    ("臺北市中正區測試路3號", "63", "6300100", "測試路", "121.5", "25.0"),
    ("臺北市中正區測試路3號", "63", "6300100", "測試路", "121.5", "25.001"),
    ("臺北市中正區測試路3號", "63", "6300100", "測試路", "121.5", "25.0000"),
    ("臺北市中正區測試路", "63", "6300100", "測試路", "121.5", "25.0"),
    ("臺北市文山區測試路5號", "63", "6300100", "測試路", "121.5", "25.0"),
    ("臺北市中正區測試路6號", "63", "6300100", "測試路", "abc", "25.0"),
    ("火星市測試路7號", "63", "6300100", "測試路", "121.5", "25.0"),
]
OTHER = [("臺北市中正區測試路1號", "63", "6300100", "測試路", "121.5", "25.0")]


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def source(tmp_path):
    root = tmp_path / "address"
    (root / "roads").mkdir(parents=True)
    area = root / "area_2014.csv"
    area.write_text(
        "name,dgbas_id\n臺北市中正區,6300100\n臺北市文山區,6300800\n", encoding="utf-8"
    )
    roads = []
    for name, rows in [("63-測試路.csv", MAIN), ("63-.csv", OTHER)]:
        path = root / "roads" / name
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(HEADER)
            writer.writerows(rows)
        roads.append({"path": "roads/" + name, "sha256": sha256_file(path)})
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    _git(root, "add", ".")
    _git(
        root,
        "-c", "user.name=Fixture",
        "-c", "user.email=fixture@example.invalid",
        "-c", "core.hooksPath=/dev/null",
        "commit", "-m", "fixture",
    )
    commit = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    descriptor = {
        "commit": commit,
        "administrative_files": [{"path": area.name, "sha256": sha256_file(area)}],
        "roads": sorted(roads, key=lambda x: x["path"]),
    }
    return root, descriptor


def table(directory, name):
    return pq.read_table(directory / f"{name}.parquet").to_pylist()


def test_audit_reports_five_categories(tmp_path):
    root, descriptor = source(tmp_path)
    out = tmp_path / "audit"
    summary = audit_address_source(root, descriptor, out)
    categories = summary["categories"]
    assert summary["rows"] == len(MAIN) + len(OTHER)
    assert summary["validity_counts"] == {
        "invalid_address": 1,
        "invalid_admin": 2,
        "invalid_coordinate": 1,
        "valid": 8,
    }

    # 1. Exact duplicates, also across files.
    assert categories["exact_duplicates"] == {"groups": 1, "rows": 3}
    [group] = table(out, "exact_duplicates")
    assert group["full_addr"] == "臺北市中正區測試路1號"
    assert group["sources"] == ["roads/63-.csv:2", "roads/63-測試路.csv:2", "roads/63-測試路.csv:3"]

    # 2. Same key, different address text.
    assert categories["key_text_variants"] == {"keys": 1, "rows": 2}
    texts = table(out, "key_text_variants")
    assert {row["full_addr"] for row in texts} == {"臺北市中正區測試路2號", "臺北市中正區測試路２號"}
    assert len({row["building_key"] for row in texts}) == 1

    # 3. Same key, different coordinates; "25.0" and "25.0000" are the same point.
    coordinate = categories["key_coordinate_variants"]
    assert (coordinate["keys"], coordinate["rows"]) == (1, 3)
    [conflict] = table(out, "key_coordinate_variants")
    assert conflict["distinct_coordinates"] == 2
    expected = EARTH_RADIUS_M * math.radians(0.001)
    assert conflict["max_distance_m"] == pytest.approx(expected, abs=1e-6)
    assert round(conflict["max_distance_m"], 2) == 111.2
    assert {(conflict["lat_a"], conflict["lat_b"])} == {(25.0, 25.001)}
    assert coordinate["max_distance_m_quantiles"]["max"] == pytest.approx(expected, abs=1e-6)
    assert coordinate["keys_by_county"] == {"63": 1}

    # 4. Unparsed: no key (missing 號, and an unresolvable county).
    assert categories["unparsed_addresses"] == {"rows": 2}
    unparsed = {row["full_addr"]: row for row in table(out, "unparsed_addresses")}
    assert set(unparsed) == {"臺北市中正區測試路", "火星市測試路7號"}
    assert all(row["building_key"] is None for row in unparsed.values())

    # 5. Administrative code differs from the parsed address, or cannot be parsed.
    assert categories["invalid_admin"] == {"rows": 2}
    admin = {row["full_addr"]: row for row in table(out, "invalid_admin")}
    assert set(admin) == {"臺北市文山區測試路5號", "火星市測試路7號"}
    assert admin["臺北市文山區測試路5號"]["town_code"] == "6300800"

    written = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert written["address_source_commit"] == descriptor["commit"]
    assert written["categories"] == json.loads(json.dumps(categories))


def test_audit_refuses_non_empty_output_and_wrong_commit(tmp_path):
    root, descriptor = source(tmp_path)
    out = tmp_path / "audit"
    out.mkdir()
    (out / "keep.txt").write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError):
        audit_address_source(root, descriptor, out)
    assert (out / "keep.txt").read_text(encoding="utf-8") == "x"
    with pytest.raises(ValueError, match="pinned commit"):
        audit_address_source(root, {**descriptor, "commit": "0" * 40}, tmp_path / "other")


def test_audit_command_writes_report(tmp_path):
    root, descriptor = source(tmp_path)
    descriptor_path = tmp_path / "address_source.json"
    descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
    out = tmp_path / "audit"
    result = subprocess.run(
        [
            sys.executable, "-m", "lvr_pipeline", "audit-address-source",
            "--address-dir", str(root),
            "--address-source", str(descriptor_path),
            "--output-dir", str(out),
        ],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    report = json.loads(result.stdout)
    assert report["rows"] == len(MAIN) + len(OTHER)
    assert report["categories"]["exact_duplicates"]["rows"] == 3
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [
            "exact_duplicates.parquet",
            "invalid_admin.parquet",
            "key_coordinate_variants.parquet",
            "key_text_variants.parquet",
            "summary.json",
            "unparsed_addresses.parquet",
        ]
    )
