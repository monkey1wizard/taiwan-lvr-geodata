"""Synthetic offline evidence, state and public output acceptance."""

import copy
import csv
import subprocess

import pytest

from lvr_pipeline.address_pool import build_pool
from lvr_pipeline.address_state import load_p2, resolve_offline
from lvr_pipeline.offline_lookup import AdministrativeNames, build_index
from lvr_pipeline.packaging import extract_handoff, package_output, verify_output
from lvr_pipeline.export import geometry, read_wkb, wkb
from lvr_pipeline.parquet_io import rows
from lvr_pipeline.sources import sha256_file
from test_p1_conversion import pipeline, ADDRESS


def source(tmp_path, coordinates=None):
    root = tmp_path / "address"
    root.mkdir()
    area = root / "area_2014.csv"
    area.write_text(
        "name,dgbas_id\n臺北市中正區,6300100\n臺北市文山區,6300800\n金門縣金城鎮,0902001\n",
        encoding="utf-8",
    )
    custom = root / "area_custom.csv"
    custom.write_text("name,dgbas_id\n臺北巿木柵區,6300800\n", encoding="utf-8")
    path = root / "roads" / "63-測試路.csv"
    path.parent.mkdir()
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["FULL_ADDR", "COUNTY", "TOWN", "ROAD", "X", "Y"])
        for address, county, town, lng, lat in coordinates or [
            (ADDRESS, "63", "6300100", 121.5, 25.0),
            (ADDRESS, "63", "6300100", 121.5, 25.0),
        ]:
            writer.writerow([address, county, town, "測試路", lng, lat])
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    return root, {
        "commit": commit,
        "administrative_files": [
            {"path": p.name, "sha256": sha256_file(p)} for p in [area, custom]
        ],
        "roads": [{"path": "roads/" + path.name, "sha256": sha256_file(path)}],
    }


def run(tmp_path, coordinates=None, records=None, **kwargs):
    _, _, converted = pipeline(tmp_path, records, **kwargs)
    root, descriptor = source(tmp_path, coordinates)
    index = build_index(root, descriptor, tmp_path / "work", counties=["63"])
    pool = build_pool(converted, root, descriptor, tmp_path / "work", index_path=index)
    state = resolve_offline(pool, index, tmp_path / "work")
    return converted, root, descriptor, index, pool, state


def test_admin_preserves_codes_and_custom_alias_is_not_current_name(tmp_path):
    root, d = source(tmp_path)
    names = AdministrativeNames(root, d["administrative_files"])
    assert names.canonicalize("臺北巿木柵區測試路1號") == (
        "臺北市文山區測試路1號",
        "63",
        "6300800",
    )
    assert names.canonicalize("金門縣金城鎮測試路1號")[1:] == ("09020", "0902001")
    (root / "area_2014.csv").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        AdministrativeNames(root, d["administrative_files"])


def test_pool_cross_category_relations_duplicate_evidence_and_no_money_duplication(
    tmp_path,
):
    converted, root, d, index, pool, state = run(tmp_path)
    _, pr = load_p2(pool, "address-pool")
    _, sr = load_p2(state, "offline-state")
    assert pr["pool_rows"] == 1 and pr["occurrence_rows"] == 3
    result = list(rows(state / "address_index.parquet"))
    assert (
        len(result) == 1
        and result[0]["status"] == "located"
        and result[0]["coordinate_count"] == 1
        and result[0]["evidence_count"] == 2
    )
    assert sr["tgos_started"] is False and sr["dataset_counts"]["tgos-ledger"] == 0
    assert not list(rows(state / "unmatched_addresses.parquet"))
    output = package_output(
        converted,
        state,
        tmp_path / "output",
        notices={
            "publication_authorized": True,
            "legacy_coordinates_authorized": True,
            "sources": ["synthetic"],
        },
    )
    manifest = verify_output(output)
    assert manifest["retained_rows"] == 3
    assert (
        sum(
            m["categories"][c]["amount_minor_sum"]
            for m in manifest["months"]
            for c in m["categories"]
        )
        == 370368
    )
    maintenance = next(a for a in manifest["assets"] if a["kind"] == "maintenance")
    handoff = extract_handoff(output / maintenance["path"], tmp_path / "fresh")
    assert not (tmp_path / "fresh/raw").exists()
    assert (
        load_p2(tmp_path / "fresh" / handoff["state"], "offline-state")[1][
            "status_counts"
        ]
        == sr["status_counts"]
    )
    assert manifest["years"][0]["absent_months"] == [202600 + i for i in range(2, 13)]


def test_conflicting_points_never_pick_first_and_invalid_admin_retained(tmp_path):
    coords = [
        (ADDRESS, "63", "6300100", 121.5, 25),
        (ADDRESS, "63", "6300100", 121.6, 25.1),
        ("臺北市中正區測試路11號", "64", "6400100", 121.4, 25),
    ]
    *_, index, pool, state = run(tmp_path, coords)
    results = list(rows(state / "address_index.parquet"))
    assert results[0]["status"] == "conflict" and results[0]["lng"] is None
    assert list(rows(state / "unmatched_addresses.parquet")) == results
    invalid = list(rows(index / "offline_rows.parquet"))[-1]
    assert invalid["validity"] == "invalid_admin" and invalid["lng"] is None


def test_dirty_or_wrong_address_pin_is_rejected(tmp_path):
    root, d = source(tmp_path)
    wrong = {**d, "commit": "0" * 40}
    with pytest.raises(ValueError, match="pinned"):
        build_index(root, wrong, tmp_path / "work")
    (root / "extra.txt").write_text("changed")
    with pytest.raises(ValueError, match="clean"):
        build_index(root, d, tmp_path / "work")


@pytest.mark.parametrize(
    "points,category,kind,approx",
    [
        ([], "sales", None, False),
        ([(121, 25)], "rent", "Point", False),
        ([(121, 25), (122, 26)], "sales", "Polygon", True),
        ([(121, 25), (121, 26)], "sales", "MultiPoint", False),
        ([(121, 25), (122, 25)], "presale", "MultiPoint", False),
        ([(121, 25), (122, 26)], "rent", "MultiPoint", False),
    ],
)
def test_geometry_wkb_roundtrip(points, category, kind, approx):
    shape, approximation = geometry(points, category)
    assert (shape["type"] if shape else None) == kind and approximation == approx
    assert read_wkb(wkb(shape)) == shape


def test_corrupted_public_month_fails(tmp_path):
    converted, *_, state = run(tmp_path)
    output = package_output(
        converted,
        state,
        tmp_path / "output",
        notices={
            "publication_authorized": True,
            "legacy_coordinates_authorized": True,
            "sources": ["synthetic"],
        },
    )
    file = next(output.rglob("*.ndjson"))
    file.write_text("changed")
    with pytest.raises(ValueError, match="hash"):
        verify_output(output)


def test_unproven_legacy_permission_cannot_publish(tmp_path):
    converted, *_, state = run(tmp_path)
    with pytest.raises(ValueError, match="Legacy README"):
        package_output(
            converted,
            state,
            tmp_path / "output",
            notices={"publication_authorized": True, "sources": ["synthetic"]},
        )


def test_changed_source_removal_cannot_reuse_old_coordinate(tmp_path):
    converted, root, d, index, pool, state = run(tmp_path)
    path = root / d["roads"][0]["path"]
    path.write_text(
        "FULL_ADDR,COUNTY,TOWN,ROAD,X,Y\n臺北市中正區測試路11號,63,6300100,測試路,121.6,25.1\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(root), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-m",
            "new evidence",
        ],
        check=True,
        capture_output=True,
    )
    changed = copy.deepcopy(d)
    changed["commit"] = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    changed["roads"][0]["sha256"] = sha256_file(path)
    new_index = build_index(root, changed, tmp_path / "work", counties=["63"])
    new_pool = build_pool(
        converted, root, changed, tmp_path / "work", index_path=new_index
    )
    new_state = resolve_offline(
        new_pool, new_index, tmp_path / "work", prior_state=state
    )
    assert list(rows(new_state / "address_index.parquet"))[0]["status"] == "unmatched"
    assert list(rows(state / "address_index.parquet"))[0]["status"] == "located"


def test_transaction_months_cross_years_and_categories_share_one_key(tmp_path):
    records = [{"date": "1141102"}, {"date": "1150102"}]
    converted, *_, state = run(tmp_path, records=records)
    assert load_p2(state, "offline-state")[1]["pool_rows"] == 1
    output = package_output(
        converted,
        state,
        tmp_path / "output",
        notices={
            "publication_authorized": True,
            "legacy_coordinates_authorized": True,
            "sources": ["synthetic"],
        },
    )
    manifest = verify_output(output)
    assert [m["tx_yyyymm"] for m in manifest["months"]] == [202511, 202601]
    assert manifest["retained_rows"] == 6 and len(manifest["years"]) == 2


def test_scope_keeps_other_county_unlocated_and_nearby_door_distinct(tmp_path):
    converted, *_, state = run(
        tmp_path,
        records=[
            {"address": ADDRESS},
            {"address": "臺北市中正區測試路11號"},
            {"address": "金門縣金城鎮測試路1號"},
        ],
    )
    results = list(rows(state / "address_index.parquet"))
    assert len(results) == 3
    assert {r["status"] for r in results} == {"located", "unmatched", "outside_scope"}
    assert any(
        r["county_code"] == "09020" and r["town_code"] == "0902001" for r in results
    )


def test_index_validates_fields_without_filename_geographic_guess(tmp_path):
    coordinates = [
        ("金門縣金城鎮測試路1號", "09020", "0902001", 118.3, 24.4),
        ("臺北市中正區測試路10號之2", "63", "6300100", float("nan"), 25),
    ]
    root, d = source(tmp_path, coordinates)
    index = build_index(root, d, tmp_path / "work")
    values = list(rows(index / "offline_rows.parquet"))
    assert values[0]["county_code"] == "09020" and values[0]["validity"] == "valid"
    assert values[1]["validity"] == "invalid_coordinate" and values[1]["lng"] is None


def test_garbled_candidate_requires_unique_coordinate_not_corpus_only(tmp_path):
    address = "臺北市中正區測?路10號之1"
    *_, pool, state = run(tmp_path, records=[{"address": address}])
    occurrence = list(rows(pool / "address_occurrences.parquet"))
    assert all(r["reason"] == "coordinate_confirmed_candidate" for r in occurrence)
    assert all(r["status"] == "located" for r in rows(state / "address_index.parquet"))
    # Matching road corpus with an absent complete door has no coordinate evidence.
    other = tmp_path / "other"
    other.mkdir()
    *_, pool, state = run(other, records=[{"address": "臺北市中正區測?路99號"}])
    assert all(
        r["building_key"] is None and r["reason"] == "candidate_review"
        for r in rows(pool / "address_occurrences.parquet")
    )


def test_official_profile_rejects_unknown_crs_and_hash(tmp_path):
    root, d = source(tmp_path)
    path = tmp_path / "official.csv"
    path.write_text("synthetic")
    official = {
        "path": str(path),
        "sha256": sha256_file(path),
        "crs": "guess",
        "county_code": "63",
    }
    with pytest.raises(ValueError, match="CRS"):
        build_index(root, d, tmp_path / "work", official=official)
    official["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash"):
        build_index(root, d, tmp_path / "work", official=official)


def test_real_format_geometry_partial_null_and_amount_once(tmp_path):
    records = [
        {"address": "臺北市中正區測試路10、11號"},
        {"address": "臺北市中正區測試路10、99號"},
        {"address": "臺北市中正區測試路88、99號"},
    ]
    coords = [
        ("臺北市中正區測試路10號", "63", "6300100", 121.5, 25),
        ("臺北市中正區測試路11號", "63", "6300100", 121.6, 25.1),
    ]
    converted, *_, state = run(tmp_path, coords, records)
    output = package_output(
        converted,
        state,
        tmp_path / "output",
        notices={
            "publication_authorized": True,
            "legacy_coordinates_authorized": True,
            "sources": ["synthetic"],
        },
    )
    manifest = verify_output(output)
    import pyarrow.parquet as pq

    for category in ["sales", "presale", "rent"]:
        table = pq.read_table(
            next(output.rglob(f"202601_{category}.parquet"))
        ).to_pylist()
        assert {r["location_status"] for r in table} == {"complete", "partial", "none"}
        assert sum(r["amount_minor"] for r in table) == 3 * 123456
        complete = next(r for r in table if r["location_status"] == "complete")
        assert read_wkb(complete["geometry"])["type"] == (
            "MultiPoint" if category == "rent" else "Polygon"
        )
    assert manifest["retained_rows"] == 9


def test_annual_part_budget_preserves_all_original_months(tmp_path):
    records = [{"date": f"114{month:02d}02"} for month in range(1, 13)]
    converted, *_, state = run(tmp_path, records=records)
    output = package_output(
        converted,
        state,
        tmp_path / "output",
        max_asset_bytes=100000,
        notices={
            "publication_authorized": True,
            "legacy_coordinates_authorized": True,
            "sources": ["synthetic"],
        },
    )
    manifest = verify_output(output)
    annual = next(y for y in manifest["years"] if y["year"] == 2025)
    assert len(annual["formats"]["geoparquet"]) > 1 and not annual["absent_months"]
    assert annual["year_coverage_status"] == "scope_limited"


def test_empty_category_ndjson_has_no_records_and_nonzero_transfer_length(tmp_path):
    converted, *_, state = run(tmp_path, categories=("sales",))
    output = package_output(
        converted,
        state,
        tmp_path / "output",
        notices={
            "publication_authorized": True,
            "legacy_coordinates_authorized": True,
            "sources": ["synthetic"],
        },
    )
    manifest = verify_output(output)
    for category in ["presale", "rent"]:
        assert next(output.rglob(f"202601_{category}.ndjson")).read_bytes() == b"\n"
        assert manifest["months"][0]["categories"][category]["rows"] == 0
        assert (
            manifest["months"][0]["categories"][category]["status"] == "empty_in_scope"
        )
