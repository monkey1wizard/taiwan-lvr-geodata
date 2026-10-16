"""R07-9: catalog.json, output verification and the data/output Git rule. Synthetic data only."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "acceptance"))

from lvr_pipeline.addresses.admin import admin_towns_sha256  # noqa: E402
from lvr_pipeline.addresses.identity import NORMALIZATION_VERSION  # noqa: E402
from lvr_pipeline.output import verify as verify_module  # noqa: E402
from lvr_pipeline.output.catalog import write_catalog  # noqa: E402
from lvr_pipeline.output.rows import CSV_COLUMNS  # noqa: E402
from lvr_pipeline.output.verify import OutputVerificationError, verify_output  # noqa: E402
from lvr_pipeline.pipeline import run_full  # noqa: E402
from lvr_pipeline.storage.runs import load_snapshot, sha256_file  # noqa: E402
from test_r07_8_output import built  # noqa: E402,F401  (module fixture: converted, state and rows)
from test_run_full import checkpoints, setup_inputs  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
MONTH_CSV = "csv/2026/taiwanlvr_202601_sales.csv"
CUTOFF = "202610"


@pytest.fixture(scope="module")
def base(built, tmp_path_factory):
    """A written output with its catalog; tests copy it before they change anything."""
    from lvr_pipeline.output.build import write_output

    root = tmp_path_factory.mktemp("r07_9")
    summary = write_output(built["converted"], built["state"], root / "public", root / "release", spill_dir=root / "spill")
    _, report = load_snapshot(built["converted"], "converted")
    catalog = write_catalog(root / "public", root / "release", summary, converted_report=report, cutoff_yyyymm=CUTOFF)
    return {"root": root, "summary": summary, "report": report, "catalog": catalog}


@pytest.fixture
def out(base, tmp_path):
    shutil.copytree(base["root"] / "public", tmp_path / "public")
    shutil.copytree(base["root"] / "release", tmp_path / "release")
    return tmp_path / "public", tmp_path / "release"


def rehash(public, release):
    """Make the catalog agree with the files on disk again (size and sha256 only, rows stay)."""
    path = public / "catalog.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    for entry in catalog["files"]:
        file = (release if entry["location"] == "release" else public) / entry["path"]
        if file.is_file():
            entry["size_bytes"], entry["sha256"] = file.stat().st_size, sha256_file(file)
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def failures_of(public, release):
    with pytest.raises(OutputVerificationError) as raised:
        verify_output(public, release, Path(public).parent / "spill")
    assert isinstance(raised.value, ValueError) and raised.value.report["passed"] is False
    return raised.value.report["failures"]


def named(failures, check, *, path=None, text=""):
    found = [f for f in failures if f["check"] == check and (path is None or f["path"].startswith(path)) and text in f["detail"]]
    assert found, f"no {check} failure for {path} containing {text!r} in {failures}"
    return found


def rewrite_csv(path, change):
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.reader(stream))
    change(rows)
    with path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream, lineterminator="\n").writerows(rows)


def test_catalog_content_and_hashes(base):
    catalog = json.loads((base["root"] / "public" / "catalog.json").read_text(encoding="utf-8"))
    assert catalog == base["catalog"]
    assert set(catalog) == {"currency", "contract_version", "door_rule_version", "cutoff_yyyymm", "source_zip_sha256",
                            "files", "total_rows", "admin_towns_sha256", "char_fixes_sha256"}
    assert catalog["currency"] == "TWD" and catalog["contract_version"] == "2026-10-12-22col"
    assert catalog["door_rule_version"] == NORMALIZATION_VERSION and catalog["cutoff_yyyymm"] == CUTOFF
    assert catalog["admin_towns_sha256"] == admin_towns_sha256()  # R07-6b
    assert catalog["char_fixes_sha256"] == base["report"]["char_fixes_sha256"] and len(catalog["char_fixes_sha256"]) == 64  # R07-6d
    assert catalog["source_zip_sha256"] == base["report"]["source_sha256"] and catalog["source_zip_sha256"]
    assert catalog["total_rows"] == 21
    text = (base["root"] / "public" / "catalog.json").read_text(encoding="utf-8")
    assert text.endswith("}\n") and text.startswith('{\n  "') and "\\u" not in text
    paths = [e["path"] for e in catalog["files"]]
    assert paths == sorted(paths) and "catalog.json" not in paths and len(paths) == len(set(paths))
    for entry in catalog["files"]:
        assert set(entry) == {"path", "location", "format", "period", "year", "month", "category", "rows",
                              "size_bytes", "sha256"}
        file = base["root"] / ("release" if entry["location"] == "release" else "public") / entry["path"]
        assert entry["size_bytes"] == file.stat().st_size and entry["sha256"] == sha256_file(file)
    by_path = {e["path"]: e for e in catalog["files"]}
    parquet = by_path["taiwanlvr.parquet"]
    assert (parquet["location"], parquet["format"], parquet["period"], parquet["year"], parquet["month"],
            parquet["category"], parquet["rows"]) == ("release", "geoparquet", "all", None, None, None, 21)
    month_csv = by_path[MONTH_CSV]
    assert (month_csv["location"], month_csv["format"], month_csv["period"], month_csv["year"], month_csv["month"],
            month_csv["category"], month_csv["rows"]) == ("public", "csv", "month", 2026, 1, "sales", 4)
    assert {e["period"] for e in catalog["files"]} == {"all", "month"}
    assert {e["format"] for e in catalog["files"]} == {"geoparquet", "csv"}


def test_output_holds_no_gpkg_geojson_or_yearly_csv(base):
    public = base["root"] / "public"
    names = {p.relative_to(public).as_posix() for p in public.rglob("*") if p.is_file()}
    assert not [n for n in names if n.endswith((".gpkg", ".geojson"))]
    assert not [n for n in names if re.search(r"taiwanlvr_[0-9]{4}_[a-z]+\.csv$", n)]
    assert not [e for e in base["catalog"]["files"] if e["period"] == "year"]
    assert sum(e["rows"] for e in base["catalog"]["files"] if e["format"] == "csv") == base["catalog"]["total_rows"]


def test_catalog_requires_source_hashes_and_a_cutoff(base, tmp_path):
    summary = base["summary"]
    shutil.copytree(base["root"] / "public", tmp_path / "p")
    shutil.copytree(base["root"] / "release", tmp_path / "r")
    for report in ({}, {"source_sha256": {}}):
        with pytest.raises(ValueError, match="source_sha256"):
            write_catalog(tmp_path / "p", tmp_path / "r", summary, converted_report=report, cutoff_yyyymm=CUTOFF)
    with pytest.raises(ValueError, match="cutoff"):
        write_catalog(tmp_path / "p", tmp_path / "r", summary, converted_report=base["report"], cutoff_yyyymm="2026-10")
    assert write_catalog(tmp_path / "p", tmp_path / "r", summary, converted_report=base["report"], cutoff_yyyymm=202610)[
        "cutoff_yyyymm"] == CUTOFF


def test_verify_passes_untouched_output_and_reports_resources_and_quality(out):
    public, release = out
    report = verify_output(public, release, Path(public).parent / "spill")
    assert report["passed"] is True and report["failures"] == [] and report["warnings"] == []
    assert set(report["checks"].values()) == {"pass"} and report["total_rows"] == 21
    assert report["catalog_sha256"] == sha256_file(public / "catalog.json")
    sizes = report["bytes_by_format"]
    assert set(sizes) == {"geoparquet", "csv"} and all(v > 0 for v in sizes.values())
    assert sizes["geoparquet"] == (release / "taiwanlvr.parquet").stat().st_size
    assert sizes["csv"] == sum(p.stat().st_size for p in public.glob("csv/*/*.csv"))
    assert sum(q["rows"] for q in report["quality"].values()) == 21 and report["quality"]["sales"]["rows"] == 7
    assert all(q[f"null_{n}"] == 0 for q in report["quality"].values()
               for n in ("price_twd", "unit_price_sqm", "area_m2_decimal", "trade_date"))


def test_missing_catalog_is_a_failure(out):
    public, release = out
    (public / "catalog.json").unlink()
    named(failures_of(public, release), "catalog", path="catalog.json", text="missing")


def test_catalog_made_with_another_town_table_is_a_failure(out):
    public, release = out
    path = public / "catalog.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    catalog["admin_towns_sha256"] = "0" * 64
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
    assert any("admin_towns_sha256" in json.dumps(f, ensure_ascii=False) for f in failures_of(public, release))


def test_catalog_without_a_char_fixes_hash_is_a_failure(out):
    public, release = out
    path = public / "catalog.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    catalog["char_fixes_sha256"] = "not a hash"
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
    assert any("char_fixes_sha256" in json.dumps(f, ensure_ascii=False) for f in failures_of(public, release))


def test_changed_csv_byte_is_found_by_the_hash(out):
    public, release = out
    path = public / MONTH_CSV
    data = path.read_bytes()
    path.write_bytes(data.replace(b"3250000", b"3250001", 1))
    failures = failures_of(public, release)
    named(failures, "catalog", path=MONTH_CSV, text="sha256")
    named(failures, "cross-format", text="differs in ['price_twd']")


def test_extra_file_under_public_is_found(out):
    public, release = out
    (public / "csv" / "2026" / "stray.csv").write_text("x", encoding="utf-8")
    named(failures_of(public, release), "catalog", path="csv/2026/stray.csv", text="not listed")


def test_extra_file_in_release_is_found(out):
    public, release = out
    (release / "other.parquet").write_bytes(b"x")
    named(failures_of(public, release), "catalog", text="exactly taiwanlvr.parquet")


def test_missing_file_is_found(out):
    public, release = out
    (public / MONTH_CSV).unlink()
    failures = failures_of(public, release)
    named(failures, "catalog", path=MONTH_CSV, text="does not exist")
    named(failures, "cross-format", path="2026 sales", text="monthly CSV")


@pytest.mark.parametrize("stray", [
    "gpkg/taiwanlvr_2026.gpkg",
    "geojson/2026/taiwanlvr_202601_sales.geojson",
    "csv/2026/taiwanlvr_2026_sales.csv",
])
def test_removed_format_file_in_public_is_found(out, stray):
    public, release = out
    (public / stray).parent.mkdir(parents=True, exist_ok=True)
    (public / stray).write_bytes(b"x")
    named(failures_of(public, release), "catalog", path=stray, text="not listed")


def test_changed_csv_value_with_rehashed_catalog_is_found_across_formats(out):
    public, release = out

    def change(rows):
        rows[2][CSV_COLUMNS.index("building_type")] = "tampered"

    rewrite_csv(public / MONTH_CSV, change)
    rehash(public, release)
    failures = failures_of(public, release)
    [found] = named(failures, "cross-format", path="2026 sales", text="monthly CSV")
    assert "differing 1" in found["detail"] and "differs in ['building_type']" in found["detail"]
    assert "115q1/a_lvr_land_a.csv/3#2" in found["detail"]
    named(failures, "transaction", path=MONTH_CSV, text="building_type")


def test_deleted_csv_row_with_rehashed_catalog_is_found(out):
    public, release = out
    rewrite_csv(public / MONTH_CSV, lambda rows: rows.pop(1))
    rehash(public, release)
    failures = failures_of(public, release)
    named(failures, "catalog", path=MONTH_CSV, text="holds 3 rows, catalog says 4")
    [found] = named(failures, "cross-format", path="2026 sales", text="monthly CSV")
    assert "5 rows, GeoParquet 6" in found["detail"] and "missing 1" in found["detail"]


def test_reordered_csv_header_with_rehashed_catalog_is_found(out):
    public, release = out

    def swap(rows):
        rows[0][0], rows[0][1] = rows[0][1], rows[0][0]

    rewrite_csv(public / MONTH_CSV, swap)
    rehash(public, release)
    named(failures_of(public, release), "columns", path=MONTH_CSV, text="header differs")


def test_geoparquet_geo_metadata_is_checked(out):
    public, release = out
    path = release / "taiwanlvr.parquet"
    table = pq.read_table(path)
    geo = json.loads(table.schema.metadata[b"geo"])
    geo["version"] = "1.0.0"
    geo["columns"]["geometry"]["geometry_types"] = ["Polygon"]
    pq.write_table(table.replace_schema_metadata({**table.schema.metadata, b"geo": json.dumps(geo).encode()}), path)
    rehash(public, release)
    failures = failures_of(public, release)
    named(failures, "metadata", path="taiwanlvr.parquet", text="version")
    named(failures, "metadata", path="taiwanlvr.parquet", text="geometry_types")


def test_duplicate_key_is_found(out):
    public, release = out

    def duplicate(rows):
        rows.append(list(rows[1]))

    rewrite_csv(public / MONTH_CSV, duplicate)
    rehash(public, release)
    named(failures_of(public, release), "keys", path=MONTH_CSV, text="duplicate key 115q1/a_lvr_land_a.csv/3#1")


def test_invalid_id_and_door_index_are_found(out):
    public, release = out

    def damage(rows):
        rows[1][CSV_COLUMNS.index("transaction_id")] = "115q9/a.csv/0"
        rows[2][CSV_COLUMNS.index("door_index")] = "9"

    rewrite_csv(public / MONTH_CSV, damage)
    rehash(public, release)
    failures = failures_of(public, release)
    named(failures, "keys", path=MONTH_CSV, text="does not match the contract format")
    named(failures, "keys", path=MONTH_CSV, text="not within 1..door_count")


def test_row_in_the_wrong_month_file_is_found(out):
    public, release = out

    def move(rows):
        for row in rows[1:2]:
            row[CSV_COLUMNS.index("trade_date")] = "2026-02-02"

    rewrite_csv(public / MONTH_CSV, move)
    rehash(public, release)
    named(failures_of(public, release), "month", path=MONTH_CSV, text="outside 2026-01")


def test_non_door_column_differing_inside_one_transaction_is_found(out):
    public, release = out

    def change(rows):
        rows[2][CSV_COLUMNS.index("district")] = "elsewhere"

    rewrite_csv(public / MONTH_CSV, change)
    rehash(public, release)
    failures = failures_of(public, release)
    named(failures, "transaction", path=MONTH_CSV, text="differs between its rows in ['district']")


def test_all_failures_are_collected_before_raising(out):
    public, release = out
    (public / MONTH_CSV).unlink()
    (public / "stray.txt").write_text("x", encoding="utf-8")
    with pytest.raises(OutputVerificationError) as raised:
        verify_output(public, release, Path(public).parent / "spill")
    report = raised.value.report
    assert report["checks"]["catalog"] == "fail" and len([f for f in report["failures"] if f["check"] == "catalog"]) >= 2
    assert "output verification failed" in str(raised.value)


def test_files_over_the_limit_fail_and_large_files_warn(out, monkeypatch):
    public, release = out
    biggest = max((p for p in public.rglob("*") if p.is_file() and p.name != "catalog.json"), key=lambda p: p.stat().st_size)
    monkeypatch.setattr(verify_module, "FILE_LIMIT_BYTES", biggest.stat().st_size)
    failures = failures_of(public, release)
    named(failures, "resources", path=biggest.relative_to(public).as_posix(), text="limit")
    assert {f["check"] for f in failures} == {"resources"}
    monkeypatch.setattr(verify_module, "FILE_LIMIT_BYTES", biggest.stat().st_size + 1)
    monkeypatch.setattr(verify_module, "WARN_BYTES", biggest.stat().st_size)
    report = verify_output(public, release, Path(public).parent / "spill")
    assert [w["path"] for w in report["warnings"]] == [biggest.relative_to(public).as_posix()]
    assert report["passed"] is True


def test_quality_counters_count_nulls_per_category(out):
    public, release = out
    path = release / "taiwanlvr.parquet"
    table = pq.read_table(path)
    columns = {n: table[n].to_pylist() for n in table.column_names}
    sales = [i for i, c in enumerate(columns["category"]) if c == "sales"]
    rent = [i for i, c in enumerate(columns["category"]) if c == "rent"]
    columns["price_twd"][sales[0]] = None
    columns["unit_price_sqm"][sales[0]] = None
    columns["unit_price_sqm"][sales[1]] = None
    columns["area_m2_decimal"][rent[0]] = None
    columns["trade_date"][rent[1]] = None
    import pyarrow as pa
    pq.write_table(pa.Table.from_pydict(columns, schema=table.schema), path)
    rehash(public, release)
    report = failures_report(public, release)
    quality = report["quality"]
    assert quality["sales"]["null_price_twd"] == 1 and quality["sales"]["null_unit_price_sqm"] == 2
    assert quality["sales"]["null_area_m2_decimal"] == 0 and quality["sales"]["null_trade_date"] == 0
    assert quality["rent"]["null_area_m2_decimal"] == 1 and quality["rent"]["null_trade_date"] == 1
    assert quality["presale"]["null_price_twd"] == 0


def failures_report(public, release):
    with pytest.raises(OutputVerificationError) as raised:
        verify_output(public, release, Path(public).parent / "spill")
    return raised.value.report


def test_run_full_records_catalog_and_verification(tmp_path):
    inputs = setup_inputs(tmp_path)
    result = run_full(run_id="cat1", **inputs)
    assert result["completed"] is True, result["failures"]
    run_dir = inputs["runs_root"] / "cat1"
    snapshot = checkpoints(run_dir)["output"]["snapshot_id"]
    record = json.loads((run_dir / "output" / "snapshots" / snapshot / "output.json").read_text(encoding="utf-8"))
    public = run_dir / record["public_dir"]
    catalog_sha = sha256_file(public / "catalog.json")
    assert record["catalog_sha256"] == catalog_sha == record["files_sha256"][f"{record['public_dir']}/catalog.json"]
    assert record["verification"]["passed"] is True and record["verification"]["catalog_sha256"] == catalog_sha
    catalog = json.loads((public / "catalog.json").read_text(encoding="utf-8"))
    assert catalog["cutoff_yyyymm"] == "202610" and catalog["total_rows"] == record["summary"]["total_rows"] > 0
    assert set(catalog["source_zip_sha256"]) == {"101q1", "115q1", "115q2"}
    assert not (run_dir / "output" / snapshot / "verify_report.json").exists()
    report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
    assert report["output"]["catalog_sha256"] == catalog_sha
    assert report["output"]["warnings"] == [] and set(report["output"]["quality"]) == {"sales", "presale", "rent"}
    assert sum(q["rows"] for q in report["output"]["quality"].values()) == catalog["total_rows"]
    # a rerun reuses the verified output and keeps the catalog hash
    again = run_full(run_id="cat1", **inputs)
    assert again["completed"] is True
    assert json.loads(Path(again["report"]).read_text(encoding="utf-8"))["output"]["catalog_sha256"] == catalog_sha


def test_run_full_failed_verification_leaves_the_report_and_no_committed_output(tmp_path, monkeypatch):
    inputs = setup_inputs(tmp_path)
    real = verify_output

    def damaging(public, release, spill):
        next(Path(public).glob("csv/*/*.csv")).unlink()
        return real(public, release, spill)

    monkeypatch.setattr("lvr_pipeline.pipeline.verify_output", damaging)
    result = run_full(run_id="cat2", **inputs)
    assert result["completed"] is False
    [failure] = result["failures"]
    assert failure["step"] == "output" and failure["code"] == "OutputVerificationError"
    run_dir = inputs["runs_root"] / "cat2"
    [output_dir] = sorted((run_dir / "output").glob("output-*"))
    saved = json.loads((output_dir / "verify_report.json").read_text(encoding="utf-8"))
    assert saved["passed"] is False and any(f["check"] == "catalog" and "does not exist" in f["detail"] for f in saved["failures"])
    assert "output" not in checkpoints(run_dir) and "run-report" not in checkpoints(run_dir)
    assert not (run_dir / "output" / "snapshots").exists() or not any((run_dir / "output" / "snapshots").iterdir())


def test_gitignore_keeps_data_ignored_except_data_output():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "data/*" in text and "!data/output/" in text and "/data/" not in text and "data/" not in text

    def ignored(path):
        result = subprocess.run(["git", "check-ignore", "--no-index", "-q", path], cwd=ROOT, check=False)
        assert result.returncode in (0, 1)
        return result.returncode == 0

    for path in ("data/raw/x.zip", "data/runs/r/x", "data/tmp/x", "data/cache/x", "data/tgos/x"):
        assert ignored(path), path
    for path in ("data/output/catalog.json", "data/output/csv/2026/a.csv"):
        assert not ignored(path), path
