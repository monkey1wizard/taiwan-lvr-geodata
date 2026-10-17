"""R08-2a: export the private maintenance package and rebuild the output from it. Synthetic inputs only."""
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "acceptance"))

from lvr_pipeline.addresses import admin  # noqa: E402
from lvr_pipeline.pipeline import run_full  # noqa: E402
from lvr_pipeline.storage.runs import ROOT, digest  # noqa: E402
from test_run_full import setup_inputs  # noqa: E402

SCRIPT = REPO / "scripts" / "maintenance.py"
_spec = importlib.util.spec_from_file_location("r08_2a_maintenance_script", SCRIPT)
maintenance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(maintenance)


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    """One completed synthetic run with its maintenance package, built once for the module."""
    committed = admin.ADMIN_TOWNS_PATH
    try:
        work = tmp_path_factory.mktemp("r08_2a")
        inputs = setup_inputs(work)
        result = run_full(run_id="base", **inputs)
        assert result["completed"] is True, result["failures"]
        summary = maintenance.export_maintenance("base", inputs["runs_root"])
        yield {"runs_root": inputs["runs_root"], "package": Path(summary["path"]), "summary": summary,
               "towns": admin.ADMIN_TOWNS_PATH}
    finally:
        admin.ADMIN_TOWNS_PATH = committed
        admin.load_admin_towns.cache_clear()


@pytest.fixture(autouse=True)
def synthetic_towns(request, monkeypatch):
    """The catalog records the hash of config/rules/admin_towns.json; the baseline used a synthetic town table."""
    if "baseline" in request.fixturenames:
        monkeypatch.setattr(admin, "ADMIN_TOWNS_PATH", request.getfixturevalue("baseline")["towns"])
        admin.load_admin_towns.cache_clear()
        yield
        admin.load_admin_towns.cache_clear()
    else:
        yield


@pytest.fixture
def package(baseline, tmp_path):
    """A private copy of the package that a test may tamper with."""
    target = tmp_path / "package"
    shutil.copytree(baseline["package"], target)
    return target


def _json_write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def package_json(package):
    return json.loads((package / "package.json").read_text(encoding="utf-8"))


def cli(*args, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8")


def test_current_producer_hashes_match_what_the_run_recorded(baseline):
    """The script's producer_file_hashes agrees with the pipeline's bindings: package value digests to config_sha256."""
    document = package_json(baseline["package"])
    for stage in ("converted", "tgos-state"):
        directory = maintenance._snapshot_dir(baseline["package"], stage, document["baseline"][stage]["snapshot_id"])
        bindings, recorded = maintenance._snapshot_code(directory)
        assert recorded == maintenance.producer_file_hashes()
        assert digest(recorded) == document["config_sha256"]
        assert digest(json.loads((directory / "quality.json").read_text(encoding="utf-8"))["producer_config"]) \
            == bindings["config_sha256"]


def test_export_then_rebuild_elsewhere_passes_with_every_file_same(baseline, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    document = package_json(baseline["package"])
    assert document["run_id"] == "base" and document["output"]["files_sha256"]
    assert set(document["baseline"]) == {"converted", "tgos-state", "output"}
    paths = {item["path"] for item in document["files"]}
    assert {"run/manifest.json", "run/checkpoints.json", "run/run_report.json"} <= paths
    assert not any(path.startswith(("snapshots/ingested", "snapshots/normalized")) for path in paths)
    assert baseline["summary"]["file_count"] == len(paths) + 1

    out = tmp_path / "rebuilt"
    report = maintenance.rebuild_from_maintenance(baseline["package"], out)
    assert report["pass"] is True, report
    comparison = report["comparison"]
    assert comparison["same"] == len(document["output"]["files_sha256"]) > 0
    assert comparison["different"] == comparison["missing"] == comparison["extra"] == []
    assert report["catalog_sha256_match"] is True
    for key in ["platform", "python", "duckdb_version", "pyarrow_version", "process_peak_rss_bytes", "spill_peak_bytes"]:
        assert report[key] is not None
    assert set(report["timings_seconds"]) >= {"verify_package_files", "verify_full", "write_output_tree", "total"}
    assert json.loads((out / "rebuild_report.json").read_text(encoding="utf-8"))["pass"] is True
    assert (out / "output" / document["output"]["output_id"] / "public").is_dir()
    # The package is untouched by a rebuild, so it can be rebuilt again; main() exits 0 on a pass.
    assert maintenance.main(["rebuild", "--package", str(baseline["package"]), "--out", str(tmp_path / "again")]) == 0


def test_script_exits_1_and_reports_the_file_that_differs(baseline, tmp_path):
    """A fresh process reads the committed town table, not the synthetic one: catalog.json differs, exit code 1."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = cli("rebuild", "--package", baseline["package"], "--out", tmp_path / "cli", cwd=elsewhere)
    assert result.returncode == 1, result.stderr
    report = json.loads((tmp_path / "cli" / "rebuild_report.json").read_text(encoding="utf-8"))
    assert report["pass"] is False and report["comparison"]["same"] > 0
    assert [Path(item).name for item in report["comparison"]["different"]] == ["catalog.json"]


def test_tampered_snapshot_byte_is_refused_before_any_output(package, tmp_path):
    victim = next(item for item in sorted((package / "snapshots" / "converted").rglob("*.parquet")))
    data = bytearray(victim.read_bytes())
    data[len(data) // 2] ^= 0x01
    victim.write_bytes(bytes(data))
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="does not match package.json"):
        maintenance.rebuild_from_maintenance(package, out)
    assert not (out / "output").exists() and not (out / "rebuild_report.json").exists()
    result = cli("rebuild", "--package", package, "--out", tmp_path / "out2")
    assert result.returncode == 1 and "does not match package.json" in result.stderr


def test_package_json_tampering_is_refused(package, tmp_path):
    original = (package / "package.json").read_text(encoding="utf-8")
    document = package_json(package)
    document["files"][0]["sha256"] = "f" * 64
    _json_write(package / "package.json", document)
    with pytest.raises(ValueError, match="does not match package.json"):
        maintenance.rebuild_from_maintenance(package, tmp_path / "a")

    document = json.loads(original)
    document["baseline"]["converted"]["manifest_sha256"] = "e" * 64
    _json_write(package / "package.json", document)
    with pytest.raises(ValueError, match="baseline"):
        maintenance.rebuild_from_maintenance(package, tmp_path / "b")

    document = json.loads(original)
    document["config_sha256"] = "0" * 64
    _json_write(package / "package.json", document)
    with pytest.raises(ValueError, match="config_sha256"):
        maintenance.rebuild_from_maintenance(package, tmp_path / "c")

    (package / "package.json").write_text(original, encoding="utf-8")
    (package / "run" / "stray.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="extra"):
        maintenance.rebuild_from_maintenance(package, tmp_path / "d")
    for name in "abcd":
        assert not (tmp_path / name / "output").exists()


def test_changed_producer_file_is_refused_and_named(package, tmp_path, monkeypatch):
    real = maintenance.producer_file_hashes()
    path = "lvr_pipeline/storage/runs.py"
    monkeypatch.setattr(maintenance, "producer_file_hashes", lambda: {**real, path: "0" * 64})
    with pytest.raises(ValueError, match="rebuild refused") as caught:
        maintenance.rebuild_from_maintenance(package, tmp_path / "out")
    assert path in str(caught.value)
    assert not (tmp_path / "out" / "output").exists() and not (tmp_path / "out" / "rebuild_report.json").exists()


def test_existing_targets_are_never_overwritten(baseline, package, tmp_path):
    with pytest.raises(FileExistsError, match="never overwritten"):
        maintenance.export_maintenance("base", baseline["runs_root"])
    assert cli("export", "--run-id", "base", "--runs-root", baseline["runs_root"]).returncode == 1
    out = tmp_path / "busy"
    out.mkdir()
    (out / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        maintenance.rebuild_from_maintenance(package, out)
    assert (out / "keep.txt").read_text(encoding="utf-8") == "keep" and not (out / "output").exists()
    assert cli("rebuild", "--package", package, "--out", out).returncode == 1


def test_export_requires_a_completed_run(baseline, tmp_path):
    partial = tmp_path / "runs" / "partial"
    shutil.copytree(baseline["runs_root"] / "base", partial, ignore=shutil.ignore_patterns("maintenance", "run-report"))
    checkpoints = json.loads((partial / "checkpoints.json").read_text(encoding="utf-8"))
    del checkpoints["stages"]["run-report"]
    _json_write(partial / "checkpoints.json", checkpoints)
    with pytest.raises(ValueError, match="not completed"):
        maintenance.export_maintenance("partial", tmp_path / "runs")
    assert not (partial / "maintenance").exists() and not list(partial.glob(".maintenance-staging-*"))


def test_rebuild_reads_only_the_package_and_out(package, tmp_path, monkeypatch):
    forbidden = [(ROOT / "data" / "raw").resolve(), (ROOT / "config" / "sources").resolve(),
                 (ROOT / "data" / "runs").resolve()]
    opened, active = [], {"on": False}

    def hook(event, args):
        if active["on"] and event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            opened.append(Path(os.fsdecode(args[0])))

    sys.addaudithook(hook)
    monkeypatch.chdir(tmp_path)
    active["on"] = True
    try:
        report = maintenance.rebuild_from_maintenance(package, tmp_path / "out")
    finally:
        active["on"] = False
    assert report["pass"] is True and opened
    resolved = [item.resolve() for item in opened]
    assert not [item for item in resolved if any(item.is_relative_to(root) for root in forbidden)]


def _listing(root):
    return sorted((item.relative_to(root).as_posix(), item.is_dir(), item.stat().st_size, item.stat().st_mtime_ns)
                  for item in root.rglob("*"))


def test_rebuild_treats_the_package_as_read_only(package, tmp_path):
    for item in package.rglob("*"):
        if item.is_file():
            os.chmod(item, stat.S_IREAD)
    before = _listing(package)
    try:
        report = maintenance.rebuild_from_maintenance(package, tmp_path / "out")
    finally:
        for item in package.rglob("*"):
            if item.is_file():
                os.chmod(item, stat.S_IREAD | stat.S_IWRITE)
    assert report["pass"] is True, report
    assert _listing(package) == before
    assert not list(package.rglob("spill"))
    assert report["timings_seconds"]["copy_snapshots"] >= 0 and report["copied_snapshot_bytes"] > 0
    assert (tmp_path / "out" / "store" / "converted" / "snapshots").is_dir()
