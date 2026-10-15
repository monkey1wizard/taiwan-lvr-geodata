"""R07-8b: the output stage of run-full writes the 22-column output. Synthetic inputs only."""
import json
import sys
from pathlib import Path

import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))

from lvr_pipeline.cli import main  # noqa: E402
from lvr_pipeline.pipeline import RUN_FULL_STAGES, run_full  # noqa: E402
from lvr_pipeline.storage.runs import sha256_file  # noqa: E402
from test_run_full import checkpoints, setup_inputs  # noqa: E402

EARLIER = RUN_FULL_STAGES[:6]


def record_of(run_dir):
    snapshot = checkpoints(run_dir)["output"]["snapshot_id"]
    record = json.loads((run_dir / "output" / "snapshots" / snapshot / "output.json").read_text(encoding="utf-8"))
    assert record["output_id"] == snapshot
    return record


def output_files(run_dir):
    return {p.relative_to(run_dir).as_posix(): p for p in sorted((run_dir / "output").glob("output-*/**/*")) if p.is_file()}


def reused(result):
    return {s["step"]: s["reused_checkpoint"] for s in result["steps"] if s["step"] in RUN_FULL_STAGES}


def test_full_run_writes_public_files_release_parquet_and_report_section(tmp_path):
    inputs = setup_inputs(tmp_path)
    result = run_full(run_id="out1", **inputs)
    assert result["completed"] is True, result["failures"]
    run_dir = inputs["runs_root"] / "out1"
    record = record_of(run_dir)
    public, release = run_dir / record["public_dir"], run_dir / record["release_dir"]
    assert public == run_dir / "output" / record["output_id"] / "public"
    assert release == run_dir / "output" / record["output_id"] / "release"
    formats = {item["format"] for item in record["summary"]["files"].values()}
    assert formats == {"geoparquet", "csv"}
    parquet = release / "taiwanlvr.parquet"
    assert parquet.is_file()
    table = pq.read_table(parquet)
    assert len(table.column_names) == 22
    total = record["summary"]["total_rows"]
    assert total > 0 and table.num_rows == total
    for relative, item in record["summary"]["files"].items():
        assert ((release if item["format"] == "geoparquet" else public) / relative).is_file()
    # Every written file is recorded with its sha256.
    assert {p for p in record["files_sha256"]} == set(output_files(run_dir))
    assert all(sha256_file(run_dir / p) == h for p, h in record["files_sha256"].items())
    report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
    assert report["output"] == {"output_id": record["output_id"], "public_dir": record["public_dir"],
                                "release_dir": record["release_dir"], "total_rows": total,
                                "file_count": len(record["summary"]["files"]), "parquet_rows": table.num_rows,
                                "catalog_sha256": sha256_file(public / "catalog.json"),
                                "warnings": [], "quality": record["verification"]["quality"]}
    print(json.dumps(report["output"], indent=2))


def test_write_failure_stops_at_output_and_resume_reuses_earlier_stages(tmp_path, monkeypatch):
    inputs = setup_inputs(tmp_path)

    def fail(*args, **kwargs):
        raise ValueError("synthetic write failure")

    monkeypatch.setattr("lvr_pipeline.pipeline.write_output", fail)
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


def test_rerun_reuses_output_without_rewriting_any_file(tmp_path):
    inputs = setup_inputs(tmp_path)
    assert run_full(run_id="out3", **inputs)["completed"] is True
    run_dir = inputs["runs_root"] / "out3"
    before = {name: path.stat().st_mtime_ns for name, path in output_files(run_dir).items()}
    assert before
    again = run_full(run_id="out3", **inputs)
    assert again["completed"] is True
    assert reused(again)["output"] is True
    assert {name: path.stat().st_mtime_ns for name, path in output_files(run_dir).items()} == before
    assert len(list((run_dir / "output").glob("output-*"))) == 1


def test_unrecorded_existing_output_directory_is_refused_and_left_untouched(tmp_path, monkeypatch):
    inputs = setup_inputs(tmp_path)
    from lvr_pipeline.output.build import write_output as real

    def partial(converted, state, public_root, release_root, **kwargs):
        real(converted, state, public_root, release_root, **kwargs)
        raise ValueError("synthetic failure after writing")

    monkeypatch.setattr("lvr_pipeline.pipeline.write_output", partial)
    assert run_full(run_id="out6", **inputs)["completed"] is False
    monkeypatch.undo()
    run_dir = inputs["runs_root"] / "out6"
    before = {name: path.read_bytes() for name, path in output_files(run_dir).items()}
    assert before and "output" not in checkpoints(run_dir)
    again = run_full(run_id="out6", **inputs)
    assert again["completed"] is False
    [failure] = again["failures"]
    assert failure["step"] == "output" and "already exists" in failure["detail"]
    assert {name: path.read_bytes() for name, path in output_files(run_dir).items()} == before


def test_tampered_output_file_is_not_reused_and_fails_without_touching_the_directory(tmp_path):
    inputs = setup_inputs(tmp_path)
    assert run_full(run_id="out5", **inputs)["completed"] is True
    run_dir = inputs["runs_root"] / "out5"
    record = record_of(run_dir)
    target = run_dir / record["release_dir"] / "taiwanlvr.parquet"
    target.write_bytes(target.read_bytes() + b"x")
    before = {name: path.read_bytes() for name, path in output_files(run_dir).items()}
    again = run_full(run_id="out5", **inputs)
    assert again["completed"] is False
    [failure] = again["failures"]
    assert failure["step"] == "output" and str(target) in failure["detail"]
    assert {name: path.read_bytes() for name, path in output_files(run_dir).items()} == before


def test_new_run_id_with_unchanged_inputs_produces_a_new_run_directory(tmp_path):
    inputs = setup_inputs(tmp_path)
    assert run_full(run_id="out7a", **inputs)["completed"] is True
    assert run_full(run_id="out7b", **inputs)["completed"] is True
    first, second = inputs["runs_root"] / "out7a", inputs["runs_root"] / "out7b"
    assert first != second and first.is_dir() and second.is_dir()
    assert output_files(first) and output_files(second)
    assert record_of(first)["public_dir"].startswith("output/") and record_of(second)["public_dir"].startswith("output/")
    assert all(path.is_relative_to(second) for path in output_files(second).values())


def test_cli_no_longer_accepts_notices(tmp_path, capsys):
    with pytest.raises(SystemExit) as stop:
        main(["run-full", "--run-id", "x", "--cutoff", "202610", "--address-dir", str(tmp_path),
              "--notices", str(tmp_path / "notices.json")])
    assert stop.value.code == 2
    assert "--notices" in capsys.readouterr().err
