"""R07-3b: the output stage of run-full. Synthetic inputs only."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))

from lvr_pipeline.cli import main  # noqa: E402
from lvr_pipeline.output.verify import verify_output  # noqa: E402
from lvr_pipeline.pipeline import RUN_FULL_STAGES, run_full  # noqa: E402
from lvr_pipeline.storage.runs import sha256_file  # noqa: E402
from test_run_full import checkpoints, setup_inputs  # noqa: E402

EARLIER = RUN_FULL_STAGES[:6]


def release_of(run_dir):
    snapshot = checkpoints(run_dir)["output"]["snapshot_id"]
    record = json.loads((run_dir / "output" / "snapshots" / snapshot / "release.json").read_text(encoding="utf-8"))
    return record, run_dir / record["release_dir"]


def release_dirs(run_dir):
    return sorted(p.name for p in (run_dir / "output-release").iterdir() if not p.name.startswith("."))


def reused(result):
    return {s["step"]: s["reused_checkpoint"] for s in result["steps"] if s["step"] in RUN_FULL_STAGES}


def test_full_run_produces_verified_release_and_report_summary(tmp_path):
    inputs = setup_inputs(tmp_path)
    result = run_full(run_id="out1", **inputs)
    assert result["completed"] is True, result["failures"]
    run_dir = inputs["runs_root"] / "out1"
    assert "output" in checkpoints(run_dir)
    record, release = release_of(run_dir)
    manifest = verify_output(release)
    kinds = {a["kind"] for a in manifest["assets"]}
    assert {"monthly", "annual", "annual_points", "maintenance", "size-report"} <= kinds
    monthly = [a for a in manifest["assets"] if a["kind"] == "monthly"]
    assert {a["format"] for a in monthly} == {"ndjson", "geojson", "geoparquet"}
    assert all(a["path"].endswith(".zip") for a in manifest["assets"] if a["kind"] == "annual")
    assert all(a["path"].endswith(".csv") for a in manifest["assets"] if a["kind"] == "annual_points")
    assert record["release_manifest_sha256"] == sha256_file(release / "manifest.json")
    assert record["size_report"] is not None
    report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
    assert report["output"]["release_dir"] == record["release_dir"]
    assert report["output"]["release_manifest_sha256"] == sha256_file(release / "manifest.json")
    assert report["output"]["asset_count"] == len(manifest["assets"])
    assert report["output"]["largest_asset_bytes"] == max(a["bytes"] for a in manifest["assets"])
    assert report["output"]["release_asset_bytes"] == sum(a["bytes"] for a in manifest["assets"])
    print(json.dumps(report["output"], indent=2))


def test_verify_failure_stops_at_output_and_resume_reuses_earlier_stages(tmp_path, monkeypatch):
    inputs = setup_inputs(tmp_path)

    def fail(root):
        raise ValueError("synthetic verify failure")

    monkeypatch.setattr("lvr_pipeline.pipeline.verify_output", fail)
    first = run_full(run_id="out2", **inputs)
    assert first["completed"] is False
    assert [f["step"] for f in first["failures"]] == ["output"]
    run_dir = inputs["runs_root"] / "out2"
    assert "output" not in checkpoints(run_dir) and "run-report" not in checkpoints(run_dir)
    assert not (run_dir / "run-report").exists()

    monkeypatch.undo()
    second = run_full(run_id="out2", **inputs)
    assert second["completed"] is True, second["failures"]
    assert reused(second) == {**{name: True for name in EARLIER}, "review": True, "output": False, "run-report": False}
    assert set(checkpoints(run_dir)) == set(RUN_FULL_STAGES)


def test_rerun_reuses_output_without_a_new_release(tmp_path):
    inputs = setup_inputs(tmp_path)
    assert run_full(run_id="out3", **inputs)["completed"] is True
    run_dir = inputs["runs_root"] / "out3"
    before = release_dirs(run_dir)
    again = run_full(run_id="out3", **inputs)
    assert again["completed"] is True
    assert reused(again)["output"] is True
    assert release_dirs(run_dir) == before and len(before) == 1


def test_changed_notices_rebuild_output(tmp_path):
    inputs = setup_inputs(tmp_path)
    assert run_full(run_id="out4", **inputs)["completed"] is True
    run_dir = inputs["runs_root"] / "out4"
    old = checkpoints(run_dir)["output"]["snapshot_id"]
    notices = json.loads(inputs["notices_path"].read_text(encoding="utf-8"))
    notices["sources"].append({"provider": "synthetic two", "title": "another synthetic source"})
    inputs["notices_path"].write_text(json.dumps(notices), encoding="utf-8")
    again = run_full(run_id="out4", **inputs)
    assert again["completed"] is True, again["failures"]
    assert reused(again)["output"] is False
    assert checkpoints(run_dir)["output"]["snapshot_id"] != old
    assert len(release_dirs(run_dir)) == 2


def test_tampered_release_manifest_is_not_reused_and_fails_without_touching_the_directory(tmp_path):
    inputs = setup_inputs(tmp_path)
    assert run_full(run_id="out5", **inputs)["completed"] is True
    run_dir = inputs["runs_root"] / "out5"
    _, release = release_of(run_dir)
    path = release / "manifest.json"
    path.write_bytes(path.read_bytes() + b"\n")
    tampered = path.read_bytes()
    again = run_full(run_id="out5", **inputs)
    assert again["completed"] is False
    [failure] = again["failures"]
    assert failure["step"] == "output" and str(release) in failure["detail"]
    assert path.read_bytes() == tampered
    assert release_dirs(run_dir) == [release.name]


def test_cli_requires_notices(tmp_path, capsys):
    with pytest.raises(SystemExit) as stop:
        main(["run-full", "--run-id", "x", "--cutoff", "202610", "--address-dir", str(tmp_path)])
    assert stop.value.code == 2
    assert "--notices" in capsys.readouterr().err
