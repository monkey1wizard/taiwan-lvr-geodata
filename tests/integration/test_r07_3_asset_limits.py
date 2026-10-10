"""R07-3: asset limits, annual ZIP parts, verify size check, index/part mismatch, size report."""

import json
import shutil
import zipfile
from collections import Counter

import pytest

import lvr_pipeline.output.package as package_module
import lvr_pipeline.output.verify as verify_module
from lvr_pipeline.output.package import package_output
from lvr_pipeline.output.verify import verify_output
from lvr_pipeline.storage.runs import sha256_file
from test_p2_offline import run

NOTICES = {
    "publication_authorized": True,
    "legacy_coordinates_authorized": True,
    "sources": ["synthetic"],
}
LIMIT = 100000
# 2 months of 2024 and 12 months of 2025 (ROC 113 and 114)
RECORDS = [{"date": f"113{month:02d}02"} for month in (11, 12)] + [
    {"date": f"114{month:02d}02"} for month in range(1, 13)
]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("r07_3")
    converted, *_, state = run(tmp, records=RECORDS)
    output = package_output(
        converted, state, tmp / "output", notices=NOTICES, max_asset_bytes=LIMIT
    )
    return converted, state, output


def manifest_of(root):
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def assets_of(root, kind):
    return [a for a in manifest_of(root)["assets"] if a["kind"] == kind]


def test_annual_zip_sharding_respects_limit_and_keeps_every_month_once(built):
    _, _, output = built
    manifest = manifest_of(output)
    annual = assets_of(output, "annual")
    monthly = assets_of(output, "monthly")
    # every annual asset fits, and the limit really forces sharding
    assert all(a["bytes"] <= LIMIT for a in annual)
    by_group = Counter((a["year"], a["format"]) for a in annual)
    assert any(count > 1 for count in by_group.values())
    assert {y for y, _ in by_group} == {2024, 2025}
    # part names: _partNNN only when the group has several parts, numbered from 1
    for (year, fmt), count in by_group.items():
        group = [a for a in annual if (a["year"], a["format"]) == (year, fmt)]
        assert sorted(a["part"] for a in group) == list(range(1, count + 1))
        for a in group:
            expected = (
                f"lvr_{year}_{fmt}_part{a['part']:03d}.zip"
                if count > 1
                else f"lvr_{year}_{fmt}.zip"
            )
            assert a["asset_name"] == expected
    # every original monthly file appears in exactly one part for its year and format
    for a in monthly:
        holders = [
            p
            for p in annual
            if p["year"] == a["tx_yyyymm"] // 100
            and p["format"] == a["format"]
            and a["path"] in {m["path"] for m in p["members"]}
        ]
        assert len(holders) == 1, a["path"]
        with zipfile.ZipFile(output / holders[0]["path"]) as archive:
            assert a["path"] in archive.namelist()
    members = [m["path"] for p in annual for m in p["members"]]
    assert sorted(members) == sorted(a["path"] for a in monthly)
    assert len(members) == len(set(members))
    assert [y["included_months"] for y in manifest["years"]] == [
        [202411, 202412],
        [202500 + m for m in range(1, 13)],
    ]
    verify_output(output)


def test_single_monthly_asset_over_limit_raises(built, tmp_path):
    converted, state, output = built
    largest = max(a["bytes"] for a in assets_of(output, "monthly"))
    # the check is bytes + 512 + 4096 > limit, so this limit rejects exactly the largest file
    limit = largest + 512 + 4096 - 1
    with pytest.raises(ValueError, match="Single monthly asset exceeds"):
        package_output(
            converted, state, tmp_path / "out", notices=NOTICES, max_asset_bytes=limit
        )
    # no sharding fallback: nothing was published
    published = [p for p in (tmp_path / "out").iterdir() if p.name != ".staging"]
    assert published == []


def test_annual_zip_over_declared_budget_is_rejected(built, tmp_path, monkeypatch):
    """The 'Annual ZIP exceeds declared asset budget' post-check in package_output.

    Not dead, but practically unreachable with real ZIPs at small limits: each member is
    budgeted at raw size + 512 and each ZIP at +4096, while ZIP framing for these names is
    about 250 bytes per member and deflate expands incompressible data by only ~5 bytes per
    64 KiB block. Only incompressible content very close to the limit could overflow it, and
    synthetic data cannot produce that. The branch is therefore reached by making
    package_output's zip_files write annual ZIPs with trailing padding bytes.
    """
    converted, state, _ = built
    real = package_module.zip_files

    def padded(path, files):
        real(path, files)
        if "yearly" in path.parts:
            data = path.read_bytes()
            pad = b"p" * max(LIMIT + 1 - len(data), 0)
            path.write_bytes(data + pad)  # trailing bytes only; the branch checks size

    monkeypatch.setattr(package_module, "zip_files", padded)
    with pytest.raises(ValueError, match="Annual ZIP exceeds declared asset budget"):
        package_output(
            converted, state, tmp_path / "out", notices=NOTICES, max_asset_bytes=LIMIT
        )


def test_verify_output_rejects_asset_over_github_limit(built, monkeypatch):
    _, _, output = built
    verify_output(output)
    smallest = min(a["bytes"] for a in manifest_of(output)["assets"])
    # verify reads the module global at call time
    monkeypatch.setattr(verify_module, "MAX_ASSET_BYTES", smallest - 1)
    with pytest.raises(ValueError, match="GitHub asset exceeds 2GiB"):
        verify_output(output)


def test_verify_output_rejects_index_part_set_mismatch_with_valid_hashes(
    built, tmp_path
):
    _, _, output = built
    root = tmp_path / "bad"
    shutil.copytree(output, root)
    manifest = manifest_of(root)
    index = next(
        a
        for a in manifest["assets"]
        if a["kind"] == "maintenance" and a.get("role") == "index"
    )
    path = root / index["path"]
    value = json.loads(path.read_text(encoding="utf-8"))
    assert len(value["parts"]) > 1
    value["parts"].pop()
    value["part_count"] -= 1
    path.write_text(json.dumps(value), encoding="utf-8")
    # keep the manifest consistent with the rewritten index so the hash check passes
    index["bytes"] = path.stat().st_size
    index["sha256"] = sha256_file(path)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(
        ValueError, match="Maintenance index differs from released parts"
    ):
        verify_output(root)


def test_size_report_is_consistent_with_manifest_assets(built):
    _, _, output = built
    manifest = manifest_of(output)
    reports = [a for a in manifest["assets"] if a["kind"] == "size-report"]
    assert len(reports) == 1 and reports[0]["asset_name"] == "size_report.json"
    report = json.loads((output / reports[0]["path"]).read_text(encoding="utf-8"))
    # the report is written before its own asset entry, so it excludes itself
    counted = [a for a in manifest["assets"] if a["kind"] != "size-report"]

    def total(kind):
        return sum(a["bytes"] for a in counted if a["kind"] == kind)

    assert report["snapshot_id"] == manifest["snapshot_id"]
    assert report["release_asset_bytes"] == sum(a["bytes"] for a in counted)
    assert report["monthly_bytes"] == total("monthly")
    assert report["annual_bytes"] == total("annual")
    assert report["annual_points_bytes"] == total("annual_points")
    assert report["maintenance_bytes"] == total("maintenance")
    assert report["maintenance_parts"] == sum(
        1 for a in counted if a["kind"] == "maintenance" and a.get("role") == "part"
    )
    assert report["maintenance_parts"] > 1
    assert report["largest_asset_bytes"] == max(a["bytes"] for a in counted)
    assert report["largest_asset_bytes"] <= report["max_asset_bytes"] == LIMIT
    assert report["git_generated_history_growth_bytes"] == 0
    for key in (
        "retained_build_staging_bytes",
        "conservative_working_disk_budget_bytes",
        "elapsed_seconds",
        "process_peak_rss_bytes",
    ):
        assert key in report and report[key] >= 0
    assert (
        report["conservative_working_disk_budget_bytes"]
        > 2 * report["release_asset_bytes"]
    )
