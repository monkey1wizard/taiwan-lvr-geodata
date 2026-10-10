"""R06-5 / V-10: resource fields and the disk headroom stop in run-full. Synthetic inputs only."""
import json
import shutil
import sys
from collections import namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lvr_pipeline import pipeline  # noqa: E402
from lvr_pipeline.pipeline import DISK_HEADROOM_RATIO, RUN_FULL_STAGES, disk_headroom, run_full  # noqa: E402
from test_run_full import checkpoints, setup_inputs  # noqa: E402

Usage = namedtuple("Usage", "total used free")
TOTAL = 10**13  # large enough for the unrelated workspace preflights in ingest and normalize


def fake_usage(monkeypatch, free_for):
    """Replace shutil.disk_usage; ``free_for(path)`` gives the free bytes the disk holding ``path`` reports."""
    monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda path: Usage(TOTAL, TOTAL - free_for(Path(path)), free_for(Path(path))))


def test_result_and_report_carry_resource_fields(tmp_path):
    inputs = setup_inputs(tmp_path)
    result = run_full(run_id="res1", **inputs)
    assert result["completed"] is True, result["failures"]
    stages = [step for step in result["steps"] if step["step"] in RUN_FULL_STAGES]
    assert [step["step"] for step in stages] == list(RUN_FULL_STAGES)
    for step in stages:
        assert isinstance(step["elapsed_seconds"], float)
        assert isinstance(step["process_peak_rss_bytes"], int) and step["process_peak_rss_bytes"] > 0
        assert isinstance(step["snapshot_bytes"], int) and step["snapshot_bytes"] > 0
        assert isinstance(step["disk_free_bytes"], int) and step["disk_free_bytes"] > 0
    report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
    for resources in (result["resources"], report["resources"]):
        assert set(resources) == {"disk_headroom_start", "disk_headroom_end", "run_dir_bytes",
                                  "duckdb_memory_limit", "steps"}
        for key in ("disk_headroom_start", "disk_headroom_end"):
            assert set(resources[key]) == {"path", "free_bytes", "total_bytes", "free_ratio", "required_ratio", "ok"}
            assert resources[key]["required_ratio"] == DISK_HEADROOM_RATIO
        assert resources["run_dir_bytes"] > 0
        assert resources["steps"] and all(
            set(step) == {"step", "elapsed_seconds", "process_peak_rss_bytes", "snapshot_bytes"}
            for step in resources["steps"])
    assert result["resources"]["run_dir_bytes"] >= report["resources"]["run_dir_bytes"]
    print(json.dumps(report["resources"], ensure_ascii=False, indent=2))


def test_duckdb_memory_limit_in_effect_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("LVR_DUCKDB_MEMORY_LIMIT", "2GB")
    result = run_full(run_id="res2", **setup_inputs(tmp_path))
    assert result["completed"] is True and result["resources"]["duckdb_memory_limit"] == "2GB"


def test_low_disk_stops_before_any_write(tmp_path, monkeypatch):
    inputs = setup_inputs(tmp_path)
    fake_usage(monkeypatch, lambda path: TOTAL * 19 // 100)
    result = run_full(run_id="low", **inputs)
    assert result["completed"] is False
    assert [(f["step"], f["code"]) for f in result["failures"]] == [("disk-headroom", "disk_headroom")]
    assert result["disk_headroom"]["ok"] is False
    assert not inputs["runs_root"].exists()


def test_low_disk_in_existing_runs_root_adds_nothing(tmp_path, monkeypatch):
    inputs = setup_inputs(tmp_path)
    inputs["runs_root"].mkdir()
    fake_usage(monkeypatch, lambda path: 0)
    result = run_full(run_id="low2", **inputs)
    assert result["completed"] is False and result["failures"][0]["code"] == "disk_headroom"
    assert list(inputs["runs_root"].iterdir()) == []


def test_low_disk_before_a_later_stage_stops_that_stage(tmp_path, monkeypatch):
    inputs = setup_inputs(tmp_path)
    run_dir = inputs["runs_root"] / "late"

    def free(path):  # the start check passes; once "ingested" has begun, the run directory reports a full disk
        return 0 if path == run_dir and (run_dir / "ingested").exists() else TOTAL // 2

    fake_usage(monkeypatch, free)
    result = run_full(run_id="late", **inputs)
    assert result["completed"] is False
    assert [(f["step"], f["code"]) for f in result["failures"]] == [("normalized", "DiskHeadroomError")]
    assert set(checkpoints(run_dir)) == {"offline-index", "ingested"}
    assert not (run_dir / "normalized").exists() and not (run_dir / "run-report").exists()
    committed = [step["step"] for step in result["steps"] if "snapshot_bytes" in step]
    assert committed == ["offline-index", "ingested"]


def test_exactly_twenty_percent_free_passes(tmp_path, monkeypatch):
    assert disk_headroom(tmp_path / "missing" / "dir")["ok"] is True
    fake_usage(monkeypatch, lambda path: TOTAL // 5)
    assert disk_headroom(tmp_path)["ok"] is True and disk_headroom(tmp_path)["free_ratio"] == 0.2
    result = run_full(run_id="edge", **setup_inputs(tmp_path))
    assert result["completed"] is True, result["failures"]
    fake_usage(monkeypatch, lambda path: TOTAL // 5 - 1)
    assert disk_headroom(tmp_path)["ok"] is False
