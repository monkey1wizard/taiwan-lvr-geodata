"""R07-12i: every DuckDB spill lives inside the run, and each stage records its peak spill bytes."""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "acceptance"))

from lvr_pipeline.pipeline import run_full  # noqa: E402
from lvr_pipeline.storage.spill import measured_spill, peak_bytes, reset_peak  # noqa: E402
from test_run_full import setup_inputs  # noqa: E402


def test_measured_spill_creates_the_directory_records_the_peak_and_removes_it(tmp_path):
    parent = tmp_path / "area"
    reset_peak("build")
    with measured_spill(parent, "probe", interval=0.05) as spill:
        assert spill.path.is_dir() and spill.path.parent == parent and spill.path.name.startswith("probe-")
        (spill.path / "a.bin").write_bytes(b"x" * 4096)
        (spill.path / "sub").mkdir()
        (spill.path / "sub" / "b.bin").write_bytes(b"y" * 1000)
        time.sleep(0.2)
        assert spill.interval == 0.05
    assert spill.peak_bytes >= 5096
    assert not spill.path.exists() and parent.is_dir()
    assert peak_bytes("build") >= 5096


def test_run_full_never_touches_the_system_temp(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("system temp used")

    inputs = setup_inputs(tmp_path)
    original = tempfile.TemporaryDirectory

    def guarded(*args, **kwargs):
        if kwargs.get("dir") is None and len(args) < 3:
            forbidden()
        return original(*args, **kwargs)

    monkeypatch.setattr(tempfile, "gettempdir", forbidden)
    monkeypatch.setattr(tempfile, "mkdtemp", forbidden)
    monkeypatch.setattr(tempfile, "TemporaryDirectory", guarded)
    result = run_full(run_id="spill", **inputs)
    assert result["completed"], result


def test_the_run_report_and_quality_json_record_spill_peaks(tmp_path):
    result = run_full(run_id="spill", **setup_inputs(tmp_path))
    assert result["completed"], result
    built = [step for step in result["steps"] if "snapshot_bytes" in step]
    assert built and all(isinstance(step["spill_peak_bytes"], int) and step["spill_peak_bytes"] >= 0 for step in built)
    resources = result["resources"]
    assert resources["spill_peak_bytes"] == max(step["spill_peak_bytes"] for step in built)
    assert resources["spill_sample_interval_seconds"] == 2.0
    assert [step["spill_peak_bytes"] for step in resources["steps"]] == [step["spill_peak_bytes"] for step in built]
    run_dir = Path(result["run_dir"])
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in run_dir.glob("*/snapshots/*/quality.json")]
    staged = [report for report in reports if "stage" in report]
    assert staged
    for report in staged:
        assert isinstance(report["build_spill_peak_bytes"], int) and report["build_spill_peak_bytes"] >= 0
        assert report["spill_sample_interval_seconds"] == 2.0
