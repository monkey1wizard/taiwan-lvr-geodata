"""D-2 (d12): run-full with the final-state stage on synthetic inputs: no decision, one manual decision, one new
TGOS adoption. Nothing here reads real data."""
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "acceptance"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lvr_pipeline.addresses import admin  # noqa: E402
from lvr_pipeline.addresses.identity import building_key  # noqa: E402
from lvr_pipeline.output.backfill import compare_catalogs  # noqa: E402
from lvr_pipeline.pipeline import RUN_FULL_STAGES, run_full  # noqa: E402
from lvr_pipeline.storage.parquet import rows  # noqa: E402
from lvr_pipeline.tgos import import_tgos, prepare_tgos, transition_batch  # noqa: E402
from lvr_pipeline.transactions.inventory import inventory_json, write_raw_manifest  # noqa: E402
from test_p1_conversion import ADDRESS, make_source, rules, verified_source  # noqa: E402
from test_p2_offline import source, use_synthetic_admin_towns  # noqa: E402
from test_p3_tgos import response  # noqa: E402
from test_run_full import checkpoints  # noqa: E402
from test_second_review import _decision_file  # noqa: E402

ROAD = "臺北市中正區測試路"
ADOPT, REJECT, MISSING = ROAD + "21號", ROAD + "22號", ROAD + "23號"
MANUAL_ADDRESS = MISSING + "(人工判定)"
LOG_DATE = "2026-10-18"
# One door per category and key. Only ADDRESS is located by the address source: its month stays unaffected.
DATES = {ADDRESS: "1130201", ADOPT: "1150102", REJECT: "1140601", MISSING: "1140315"}
CATEGORIES = ("presale", "rent", "sales")


def snapshot(run_dir, stage):
    return run_dir / stage / "snapshots" / checkpoints(run_dir)[stage]["snapshot_id"]


def affected_of(run_dir):
    return json.loads((snapshot(run_dir, "final-state") / "affected_months.json").read_text(encoding="utf-8"))


def sources_of(run_dir):
    return list(rows(snapshot(run_dir, "final-state") / "coordinate_sources.parquet"))


def output_record(run_dir):
    return json.loads((snapshot(run_dir, "output") / "output.json").read_text(encoding="utf-8"))


def catalog_of(run_dir):
    record = output_record(run_dir)
    return json.loads((run_dir / record["public_dir"] / "catalog.json").read_text(encoding="utf-8"))


def file_hashes(run_dir):
    """The produced files by path inside the output directory (the output id is not part of the key)."""
    record = output_record(run_dir)
    return {Path(*Path(path).parts[2:]).as_posix(): sha for path, sha in record["files_sha256"].items()}


def decision(address, lng, lat, new_address):
    return {"review_key": building_key(address), "decision": "adopted", "mode": "manual_coordinate",
            "lng": str(lng), "lat": str(lat), "address": new_address}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    committed = admin.ADMIN_TOWNS_PATH
    tmp = tmp_path_factory.mktemp("d12")
    try:
        raw, _ = make_source(tmp, [{"address": a, "date": d} for a, d in DATES.items()], batch="115q1")
        manifest = tmp / "raw_manifest.json"
        manifest.write_text(inventory_json(verified_source(write_raw_manifest(raw, manifest))) + "\n", encoding="utf-8")
        address_dir, descriptor = source(tmp)
        use_synthetic_admin_towns(tmp, address_dir, descriptor)
        descriptor_path = tmp / "address_source.json"
        descriptor_path.write_text(json.dumps(verified_source(descriptor), ensure_ascii=False), encoding="utf-8")
        inputs = {"raw_dir": raw, "manifest_path": manifest, "address_dir": address_dir,
                  "address_source": descriptor_path, "rules_path": rules(tmp), "runs_root": tmp / "runs",
                  "cutoff": 202610, "batch_rows": 2, "review_decisions": None}
        # Case 1: no decision and no TGOS ledger.
        none = run_full(run_id="none", **inputs)
        assert none["completed"] is True, none["failures"]
        none_dir = inputs["runs_root"] / "none"
        # One TGOS batch for the three doors the address source does not know (its own run directory).
        offline, index = snapshot(none_dir, "offline-state"), snapshot(none_dir, "offline-index")
        work, exchange = inputs["runs_root"] / "tgos-1", tmp / "tgos"
        prepared, folder = prepare_tgos(offline, work, exchange, index=index, exchange_date=LOG_DATE)
        batch_id = next(r["batch_id"] for r in rows(prepared / "tgos_batches.parquet"))
        submitted = transition_batch(prepared, work, batch_id, "submitted")
        finish = folder / "Address_Finish.csv"
        response(finish, [
            {"Address": ADOPT, "Response_Address": ADOPT, "Response_X": "121.51", "Response_Y": "25.01"},
            {"Address": REJECT, "Response_Address": ROAD + "99號", "Response_X": "121.52", "Response_Y": "25.02"},
            {"Address": MISSING, "Response_Address": "", "Response_X": "", "Response_Y": ""},
        ])
        ledger = import_tgos(submitted, work, batch_id, finish)
        yield {"tmp": tmp, "inputs": inputs, "none_dir": none_dir, "ledger": ledger, "exchange": exchange}
    finally:
        admin.ADMIN_TOWNS_PATH = committed
        admin.load_admin_towns.cache_clear()


def case(world, run_id, *, ledger=True, decisions=None, baseline=True, **extra):
    """One run-full of the shared inputs; the baseline is the final-state of the no-decision run."""
    inputs = {**world["inputs"], "review_decisions": decisions}
    if ledger:
        inputs.update(tgos_ledger=world["ledger"], exchange_dir=world["exchange"])
    if baseline:
        inputs["backfill_baseline"] = snapshot(world["none_dir"], "final-state")
    result = run_full(run_id=run_id, **inputs, **extra)
    assert result["completed"] is True, result["failures"]
    return result, world["inputs"]["runs_root"] / run_id


def months(affected):
    return [(m["year"], m["month"], m["category"], m["added"], m["updated"], m["removed"]) for m in affected["months"]]


def proof(world, run_dir):
    """The catalog of the run against the no-decision run, judged with the run's affected list."""
    return compare_catalogs(catalog_of(world["none_dir"]), catalog_of(run_dir), affected_of(run_dir))


def test_no_decision_run_lists_nothing_and_reports_an_absent_decision_directory(world):
    none_dir = world["none_dir"]
    report = json.loads((snapshot(none_dir, "run-report") / "run_report.json").read_text(encoding="utf-8"))
    assert set(checkpoints(none_dir)) == set(RUN_FULL_STAGES)
    assert report["review_decisions"] == {"dir": None, "exists": False, "files": []}
    assert report["second_review"]["decision_totals"] == {"rows": 0, "applied": 0, "superseded": 0, "orphaned": 0, "stale": 0}
    final = report["final_state"]
    assert final["baseline_kind"] == "offline-state" and final["affected"] == {"months": 0, "added": 0, "updated": 0, "removed": 0}
    assert final["coordinate_source_rows"] == 0 and final["source_doors"] == {"tgos": 0, "manual": 0}
    affected = affected_of(none_dir)
    assert affected["months"] == [] and affected["files"] == [] and affected["removed_total"] == 0
    assert sources_of(none_dir) == []
    result = proof(world, none_dir)
    assert result["ok"] and result["errors"] == [] and result["warnings"] == [] and result["checked"] > 0
    assert report["output"]["output_id"] == output_record(none_dir)["output_id"]


def test_new_tgos_adoption_lists_its_month_and_leaves_the_other_files_alone(world):
    result, run_dir = case(world, "ledger")
    affected = affected_of(run_dir)
    assert affected["baseline_kind"] == "final-state"
    assert months(affected) == [(2026, 1, category, 1, 0, 0) for category in CATEGORIES]
    assert affected["files"] == [f"csv/2026/taiwanlvr_202601_{c}.csv" for c in CATEGORIES] + [
        "taiwanlvr.parquet", "catalog.json"]
    sources = sources_of(run_dir)
    # One row per observation of the updated key: one door in each of the three categories.
    assert len(sources) == 3 and {r["source"] for r in sources} == {"tgos"}
    assert {r["building_key"] for r in sources} == {building_key(ADOPT)}
    assert {(r["after_lng"], r["after_lat"]) for r in sources} == {(121.51, 25.01)}
    check = proof(world, run_dir)
    assert check["ok"] and check["warnings"] == [], check
    # The files outside the list are byte-identical to the no-decision run, in the output directory too.
    before, after = file_hashes(world["none_dir"]), file_hashes(run_dir)
    changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
    assert changed == sorted(
        [f"public/csv/2026/taiwanlvr_202601_{c}.csv" for c in CATEGORIES]
        + ["public/catalog.json", "release/taiwanlvr.parquet"])
    unchanged = [path for path in before if path.startswith("public/csv/2024/")]
    assert unchanged and all(before[path] == after[path] for path in unchanged)
    report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
    assert report["final_state"]["affected"] == {"months": 3, "added": 3, "updated": 0, "removed": 0}
    assert report["final_state"]["source_doors"] == {"tgos": 3, "manual": 0}
    assert report["final_state"]["coordinate_source_rows"] == 3
    assert report["second_review"]["entry_reason_counts"]["tgos_isolated"] == 1
    assert report["second_review"]["entry_reason_counts"]["tgos_not_found"] == 1


def test_adopted_manual_decision_lists_its_months_and_counts_its_doors(world, tmp_path):
    directory = tmp_path / "decisions"
    path = _decision_file(directory, "20261018", 1, [decision(MISSING, 121.5, 25.0, MANUAL_ADDRESS)])
    before_files = sorted(p.name for p in directory.iterdir())
    result, run_dir = case(world, "manual", decisions=directory)
    # The program never writes into the decision directory.
    assert sorted(p.name for p in directory.iterdir()) == before_files == [path.name]
    affected = affected_of(run_dir)
    assert months(affected) == [(y, m, c, 1, 0, 0) for y, m in ((2025, 3), (2026, 1)) for c in CATEGORIES]
    assert affected["removed_total"] == 0 and affected["baseline_kind"] == "final-state"
    sources = sources_of(run_dir)
    manual = [r for r in sources if r["source"] == "manual"]
    second = next(r for r in rows(snapshot(run_dir, "review") / "second_review.parquet")
                  if r["review_key"] == building_key(MISSING))
    assert second["status"] == "adopted" and second["observation_count"] == 3
    # coordinate_sources: one row per observation of each updated key (the manual key and the TGOS key).
    assert len(manual) == second["observation_count"] == 3
    assert len(sources) == 6 and {r["after_address"] for r in manual} == {MANUAL_ADDRESS}
    assert {(r["after_lng"], r["after_lat"]) for r in manual} == {(121.5, 25.0)}
    check = proof(world, run_dir)
    assert check["ok"] and check["warnings"] == [], check
    before, after = file_hashes(world["none_dir"]), file_hashes(run_dir)
    # Month 2024-02 is outside the list: its files are the same as in the no-decision run.
    outside = [p for p in before if p.startswith("public/csv/2024/")]
    assert len(outside) == 3 and all(before[p] == after[p] for p in outside)
    report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
    assert report["review_decisions"] == {"dir": directory.as_posix(), "exists": True, "files": [
        [path.name, report["second_review"]["decision_files"][0]["sha256"]]]}
    assert report["second_review"]["decision_totals"]["applied"] == 1
    assert report["final_state"]["source_doors"] == {"tgos": 3, "manual": 3}
    assert report["final_state"]["decisions"]["manual_keys_applied"] == 1

    # A rerun of the same case in the same run id reuses every stage and keeps the output id.
    again, _ = case(world, "manual", decisions=directory)
    assert all(s["reused_checkpoint"] for s in again["steps"] if s["step"] in RUN_FULL_STAGES)
    assert output_record(run_dir)["output_id"] == report["output"]["output_id"]
    # Another run id with the same inputs gives the same output files.
    _, other_dir = case(world, "manual-again", decisions=directory)
    assert file_hashes(other_dir) == after
    assert output_record(other_dir)["catalog_sha256"] == output_record(run_dir)["catalog_sha256"]

    # A new decision file in the directory rebuilds the review and everything after it, nothing before it.
    _decision_file(directory, "20261018", 2, [{"review_key": building_key(REJECT), "decision": "rejected"}])
    rerun, _ = case(world, "manual", decisions=directory)
    reused = {s["step"]: s["reused_checkpoint"] for s in rerun["steps"] if s["step"] in RUN_FULL_STAGES}
    assert reused == {**{name: True for name in RUN_FULL_STAGES[:7]}, "review": False, "final-state": False,
                      "output": False, "run-report": False}
    assert output_record(run_dir)["output_id"] != report["output"]["output_id"]
    assert months(affected_of(run_dir)) == months(affected)


def test_cli_default_decision_directory_is_used_only_when_it_exists(tmp_path, monkeypatch):
    import argparse

    from lvr_pipeline import pipeline

    seen = []
    monkeypatch.setattr(pipeline, "run_full", lambda **kwargs: seen.append(kwargs["review_decisions"]) or {"completed": True})
    args = argparse.Namespace(
        run_id="x", cutoff=202610, address_dir=tmp_path, runs_root=None, raw_dir=tmp_path, manifest=tmp_path,
        address_source=tmp_path, garbled_rules=tmp_path, config=None, batch_rows=2, tgos_ledger=None,
        exchange_dir=tmp_path, review_decisions=None, backfill_baseline=None)
    monkeypatch.setattr(pipeline, "DEFAULT_REVIEW_DECISIONS", tmp_path / "absent")
    assert pipeline._run_full_command(args) == 0 and seen == [None]
    (tmp_path / "default").mkdir()
    monkeypatch.setattr(pipeline, "DEFAULT_REVIEW_DECISIONS", tmp_path / "default")
    pipeline._run_full_command(args)
    assert seen[-1] == tmp_path / "default"
    args.review_decisions = tmp_path / "given"
    pipeline._run_full_command(args)
    assert seen[-1] == tmp_path / "given"


def test_api_default_applies_no_decision_even_when_the_default_directory_exists(world, tmp_path, monkeypatch):
    (tmp_path / "data" / "review-decisions").mkdir(parents=True)
    _decision_file(tmp_path / "data" / "review-decisions", "20261018", 1,
                   [decision(MISSING, 121.5, 25.0, MANUAL_ADDRESS)])
    monkeypatch.chdir(tmp_path)
    result, run_dir = case(world, "api-default", baseline=False)
    report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
    assert report["review_decisions"] == {"dir": None, "exists": False, "files": []}
    assert report["second_review"]["decision_totals"]["rows"] == 0


def test_the_three_cases_have_three_output_ids(world):
    ids = {name: output_record(world["inputs"]["runs_root"] / name)["output_id"] for name in ("none", "ledger", "manual")}
    assert len(set(ids.values())) == len(ids)


def test_a_missing_decision_directory_given_explicitly_fails_before_anything_is_written(world, tmp_path):
    inputs = {**world["inputs"], "review_decisions": tmp_path / "missing", "runs_root": tmp_path / "runs"}
    result = run_full(run_id="nodir", **inputs)
    assert result["completed"] is False
    assert [f["code"] for f in result["failures"]] == ["review_decisions_missing"]
    assert not (tmp_path / "runs").exists()
    shutil.rmtree(tmp_path / "runs", ignore_errors=True)
