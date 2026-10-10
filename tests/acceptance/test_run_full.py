"""R06-4 / V-08: the run-full command on synthetic inputs only. Nothing here reads real data."""
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))

from lvr_pipeline.cli import main  # noqa: E402
from lvr_pipeline.pipeline import RUN_FULL_STAGES, coverage_failures, run_full  # noqa: E402
from lvr_pipeline.results.reconcile import DEFAULT_CONFIG  # noqa: E402
from lvr_pipeline.storage.parquet import rows  # noqa: E402
from lvr_pipeline.storage.runs import Stage  # noqa: E402
from lvr_pipeline.transactions.inventory import write_raw_manifest  # noqa: E402
from test_p1_conversion import ADDRESS, dataset, make_source, rules  # noqa: E402
from test_p2_offline import source  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "p0"


def setup_inputs(tmp_path):
    """Two batches in the same transaction month, three categories each, plus the known-empty 101q1."""
    # 115q1 holds two identical rows with the same serial in every member: two source observations.
    raw, _ = make_source(tmp_path, [{}, {}], batch="115q1")
    make_source(tmp_path, [{}], batch="115q2")
    shutil.copyfile(FIXTURES / "101q1_lvr_landcsv.zip", raw / "101q1_lvr_landcsv.zip")
    manifest = tmp_path / "raw_manifest.json"
    write_raw_manifest(raw, manifest)
    address_dir, descriptor = source(tmp_path)
    descriptor_path = tmp_path / "address_source.json"
    descriptor_path.write_text(json.dumps(descriptor, ensure_ascii=False), encoding="utf-8")
    notices = tmp_path / "notices.json"
    notices.write_text(json.dumps({"publication_authorized": True, "record_grain": "source_observation",
                                   "sources": [{"provider": "synthetic", "title": "synthetic source"}]}), encoding="utf-8")
    return {"notices_path": notices, "raw_dir": raw, "manifest_path": manifest, "address_dir": address_dir,
            "address_source": descriptor_path, "rules_path": rules(tmp_path),
            "runs_root": tmp_path / "runs", "cutoff": 202610, "batch_rows": 2}


def files(directory):
    return {p.relative_to(directory).as_posix(): p.read_bytes() for p in sorted(Path(directory).rglob("*")) if p.is_file()}


def checkpoints(run_dir):
    path = Path(run_dir) / "checkpoints.json"
    return json.loads(path.read_text(encoding="utf-8"))["stages"] if path.exists() else {}


def test_full_run_cross_batch_three_categories_known_empty_and_duplicate_serials(tmp_path, capsys):
    inputs = setup_inputs(tmp_path)
    code = main(["run-full", "--run-id", "full1", "--cutoff", "202610", "--address-dir", str(inputs["address_dir"]),
                 "--runs-root", str(inputs["runs_root"]), "--raw-dir", str(inputs["raw_dir"]),
                 "--manifest", str(inputs["manifest_path"]), "--address-source", str(inputs["address_source"]),
                 "--garbled-rules", str(inputs["rules_path"]), "--batch-rows", "2",
                 "--notices", str(inputs["notices_path"])])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["completed"] is True and result["failures"] == []
    run_dir = inputs["runs_root"] / "full1"
    assert set(checkpoints(run_dir)) == set(RUN_FULL_STAGES)
    report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
    assert report["completed"] is True
    assert report["batches"] == ["101q1", "115q1", "115q2"] and report["known_empty_batches"] == ["101q1"]
    assert report["source_verification"] == {"batch_count": 3, "passed_count": 3, "known_empty_count": 1}
    # Every input row has a destination: 3 rows x 3 categories, none excluded or failed.
    assert report["rows"]["input_rows"] == 9 == report["rows"]["retained_rows"]
    assert report["rows"]["excluded_rows"] == 0 and report["rows"]["failed_rows"] == 0
    converted = run_dir / "converted" / "snapshots" / checkpoints(run_dir)["converted"]["snapshot_id"]
    observations = dataset(converted, "observation")
    assert {row["category"] for row in observations} == {"sales", "presale", "rent"}
    assert {row["src_batch"] for row in observations} == {"115q1", "115q2"}
    assert {row["tx_yyyymm"] for row in observations} == {202601}
    # Duplicate serials: both rows stay as separate source observations.
    duplicated = [row for row in observations if row["src_batch"] == "115q1" and row["category"] == "sales"]
    assert len(duplicated) == 2 and {row["source_serial"] for row in duplicated} == {"same"}
    assert len({row["raw_record_id"] for row in duplicated}) == 2
    assert len({row["raw_record_id"] for row in observations}) == 9
    assert all(row["transaction_key"] is None for row in observations)
    # One global address is reused across batches and categories; the observations are not merged.
    pool = run_dir / "address-pool" / "snapshots" / checkpoints(run_dir)["address-pool"]["snapshot_id"]
    occurrences = list(rows(pool / "address_occurrences.parquet"))
    assert len(occurrences) == 9 and len({row["raw_record_id"] for row in occurrences}) == 9
    assert len({row["building_key"] for row in occurrences}) == 1
    assert {row["normalized_address"] for row in occurrences} == {ADDRESS}
    assert report["address_status_counts"]["located"] == 1
    attempts = sorted((run_dir / "attempts").glob("*.json"))
    assert len(attempts) == 1 and json.loads(attempts[0].read_text(encoding="utf-8"))["completed"] is True
    assert not (run_dir / ".run-full-lock").exists()

    # A second start reuses every checkpoint and writes nothing new.
    before = {name: files(run_dir / name / "snapshots") for name in RUN_FULL_STAGES}
    again = run_full(run_id="full1", **inputs)
    assert again["completed"] is True
    assert all(step["reused_checkpoint"] for step in again["steps"] if step["step"] in RUN_FULL_STAGES)
    assert {name: files(run_dir / name / "snapshots") for name in RUN_FULL_STAGES} == before


@pytest.mark.parametrize("missing", ["115q2_lvr_landcsv.zip", "101q1_lvr_landcsv.zip"])
def test_missing_zip_is_incomplete_and_runs_no_stage(tmp_path, missing, capsys):
    inputs = setup_inputs(tmp_path)
    (inputs["raw_dir"] / missing).unlink()
    code = main(["run-full", "--run-id", "miss", "--cutoff", "202610", "--address-dir", str(inputs["address_dir"]),
                 "--runs-root", str(inputs["runs_root"]), "--raw-dir", str(inputs["raw_dir"]),
                 "--manifest", str(inputs["manifest_path"]), "--address-source", str(inputs["address_source"]),
                 "--garbled-rules", str(inputs["rules_path"]),
                 "--notices", str(inputs["notices_path"])])
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["completed"] is False
    # A missing file is never an empty batch, even for the known-empty 101q1.
    assert [(f["batch"], f["code"]) for f in result["failures"]] == [(missing[:5], "missing_file")]
    run_dir = inputs["runs_root"] / "miss"
    assert checkpoints(run_dir) == {}
    assert not any((run_dir / name).exists() for name in RUN_FULL_STAGES)
    assert json.loads(next((run_dir / "attempts").glob("*.json")).read_text(encoding="utf-8"))["completed"] is False


def test_failed_stage_leaves_no_completion_and_residue_is_quarantined_on_resume(tmp_path, monkeypatch):
    inputs = setup_inputs(tmp_path)
    original = Stage.finish

    def interrupted(self, artifacts, report):
        if self.kind == "review":
            raise RuntimeError("interrupted review")
        return original(self, artifacts, report)

    monkeypatch.setattr(Stage, "finish", interrupted)
    first = run_full(run_id="resume", **inputs)
    assert first["completed"] is False
    assert first["failures"] == [{"step": "review", "code": "RuntimeError", "detail": "interrupted review"}]
    run_dir = inputs["runs_root"] / "resume"
    assert set(checkpoints(run_dir)) == set(RUN_FULL_STAGES) - {"review", "output", "run-report"}
    assert not (run_dir / "run-report").exists()
    review = run_dir / "review"
    [staged] = list((review / "staging").iterdir())
    [built] = list((review / "build").iterdir())
    assert staged.name == built.name
    residue = {"staging": files(staged), "build": files(built)}
    assert residue["build"]  # the interrupted build wrote files
    earlier = {name: checkpoints(run_dir)[name] for name in RUN_FULL_STAGES[:6]}

    monkeypatch.setattr(Stage, "finish", original)
    second = run_full(run_id="resume", **inputs)
    assert second["completed"] is True, second["failures"]
    moved = second["quarantined"]
    assert {(item["kind"], item["from"]) for item in moved} == {
        ("staging", f"review/staging/{staged.name}"), ("build", f"review/build/{staged.name}")}
    targets = {item["kind"]: run_dir / item["to"] for item in moved}
    assert all(Path(item["to"]).parent.parent.as_posix() == "review/quarantine" for item in moved)
    assert Path(moved[0]["to"]).parent.name.startswith(staged.name + "-")
    # Nothing deleted: the residue is byte-for-byte where the record says it went.
    assert {kind: files(path) for kind, path in targets.items()} == residue
    # Earlier stages are reused; the review is rebuilt under the same id, not from the residue.
    assert {name: checkpoints(run_dir)[name] for name in RUN_FULL_STAGES[:6]} == earlier
    reused = {step["step"]: step["reused_checkpoint"] for step in second["steps"] if step["step"] in RUN_FULL_STAGES}
    assert reused == {**{name: True for name in RUN_FULL_STAGES[:6]}, "review": False, "output": False, "run-report": False}
    assert checkpoints(run_dir)["review"]["snapshot_id"] == staged.name
    report = json.loads(Path(second["report"]).read_text(encoding="utf-8"))
    assert report["quarantined"] == moved
    attempts = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((run_dir / "attempts").glob("*.json"))]
    assert [a["completed"] for a in attempts] == [False, True] and attempts[1]["quarantined"] == moved


def test_changed_binding_needs_a_new_run_id(tmp_path):
    inputs = setup_inputs(tmp_path)
    (inputs["raw_dir"] / "115q2_lvr_landcsv.zip").unlink()
    assert run_full(run_id="bind", **inputs)["completed"] is False
    config = tmp_path / "pipeline.toml"
    config.write_text(DEFAULT_CONFIG.read_text(encoding="utf-8").replace(
        "coordinate_tolerance_m = 30.0", "coordinate_tolerance_m = 31.0"), encoding="utf-8")
    with pytest.raises(ValueError, match="different bindings"):
        run_full(run_id="bind", config=config, **inputs)


def test_coverage_failures_report_missing_batches_and_unbalanced_members():
    manifest = {"inputs": [{"batch": "101q1", "known_empty": True}, {"batch": "115q1", "known_empty": False},
                           {"batch": "115q2", "known_empty": False}]}
    report = {"batches": ["101q1", "115q1"], "known_empty_batches": [],
              "member_dispositions": [{"batch": "115q1", "path": "a_lvr_land_a.csv", "equation": "not_checkable_legacy_ingest"}]}
    assert [f["code"] for f in coverage_failures(manifest, report)] == [
        "batch_coverage", "known_empty_mismatch", "batch_without_members", "member_accounting"]
    good = {"batches": ["101q1", "115q1", "115q2"], "known_empty_batches": ["101q1"],
            "member_dispositions": [{"batch": b, "path": "a_lvr_land_a.csv", "equation": "holds"} for b in ["115q1", "115q2"]]}
    assert coverage_failures(manifest, good) == []


def test_coverage_gate_failure_blocks_completion(tmp_path, monkeypatch, capsys):
    inputs = setup_inputs(tmp_path)
    argv = ["run-full", "--run-id", "gate", "--cutoff", "202610", "--address-dir", str(inputs["address_dir"]),
            "--runs-root", str(inputs["runs_root"]), "--raw-dir", str(inputs["raw_dir"]),
            "--manifest", str(inputs["manifest_path"]), "--address-source", str(inputs["address_source"]),
            "--garbled-rules", str(inputs["rules_path"]), "--batch-rows", "2",
            "--notices", str(inputs["notices_path"])]
    monkeypatch.setattr("lvr_pipeline.pipeline.coverage_failures",
                        lambda manifest, report: [{"code": "member_accounting", "detail": "synthetic"}])
    code = main(argv)
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["completed"] is False
    assert result["failures"] == [{"step": "coverage", "code": "member_accounting", "detail": "synthetic"}]
    run_dir = inputs["runs_root"] / "gate"
    assert set(checkpoints(run_dir)) == {"offline-index", "ingested", "normalized", "converted"}
    # The gate stops the run before any later stage: no report, pool, state or review exists.
    assert not any((run_dir / name).exists() for name in ["run-report", "address-pool", "offline-state", "review"])
    assert "report" not in result or not result["report"]
    [attempt] = (run_dir / "attempts").glob("*.json")
    assert json.loads(attempt.read_text(encoding="utf-8"))["completed"] is False
    assert not (run_dir / ".run-full-lock").exists()

    monkeypatch.undo()
    code = main(argv)
    again = json.loads(capsys.readouterr().out)
    assert code == 0 and again["completed"] is True and again["failures"] == []
    reused = {step["step"]: step["reused_checkpoint"] for step in again["steps"] if step["step"] in RUN_FULL_STAGES}
    assert all(reused[name] for name in ["offline-index", "ingested", "normalized", "converted"])
    assert not any(reused[name] for name in ["address-pool", "offline-state", "review", "run-report"])
    assert set(checkpoints(run_dir)) == set(RUN_FULL_STAGES)
    assert (run_dir / "run-report").exists() and Path(again["report"]).exists()


def test_same_serial_in_two_batches_stays_two_observations(tmp_path):
    # setup_inputs already puts source serial "same" in both 115q1 and 115q2 (and twice in 115q1).
    inputs = setup_inputs(tmp_path)
    result = run_full(run_id="serials", **inputs)
    assert result["completed"] is True, result["failures"]
    run_dir = inputs["runs_root"] / "serials"
    converted = run_dir / "converted" / "snapshots" / checkpoints(run_dir)["converted"]["snapshot_id"]
    sales = [row for row in dataset(converted, "observation") if row["category"] == "sales"]
    assert {row["source_serial"] for row in sales} == {"same"}
    by_batch = {batch: [row for row in sales if row["src_batch"] == batch] for batch in ["115q1", "115q2"]}
    assert len(by_batch["115q1"]) == 2 and len(by_batch["115q2"]) == 1
    # One serial across both batches: three distinct source observations, none merged or keyed.
    assert len({row["raw_record_id"] for row in sales}) == 3
    cross = [by_batch["115q1"][0], by_batch["115q2"][0]]
    assert cross[0]["raw_record_id"] != cross[1]["raw_record_id"]
    assert all(row["transaction_key"] is None for row in sales)
