import copy
import json
import os
from pathlib import Path

import pytest

from lvr_pipeline.storage import runs
from lvr_pipeline.storage.runs import RunStore, SnapshotStore

RUN = {"code_commit": "9fec324" + "0" * 33, "raw_manifest_sha256": "a" * 64, "address_source_sha256": "b" * 64,
       "rules_sha256": "c" * 64, "contract_version": "1.0", "key_version": "building_key_v2",
       "cutoff_yyyymm": "202609", "scope": "all"}
SNAP = {"code_commit": RUN["code_commit"], "schema_version": "1.0", "config_sha256": "d" * 64, "input_sha256": ["e" * 64]}


def build(run, tmp_path, stage, snapshot_id, parent=None, payload=b"data"):
    store = run.stage(stage)
    store.begin(snapshot_id, expected_parent=parent, bindings=SNAP)
    source = tmp_path / f"{stage}-{snapshot_id}.bin"
    source.write_bytes(payload)
    store.add(snapshot_id, "out.bin", source, format_name="binary", row_count=None)
    store.publish(snapshot_id, expected_parent=parent)
    return store


def test_default_root_is_data_runs():
    assert runs.DEFAULT_RUNS_ROOT.parts[-2:] == ("data", "runs")


def test_open_writes_manifest_once_and_reopen_with_same_bindings(tmp_path):
    run = RunStore.open(tmp_path, "run1", RUN)
    manifest = run.root / "manifest.json"
    before = manifest.read_bytes()
    assert json.loads(before)["bindings"] == RUN
    again = RunStore.open(tmp_path, "run1", copy.deepcopy(RUN))
    assert again.root == run.root and manifest.read_bytes() == before


def test_different_bindings_rejected_and_manifest_unchanged(tmp_path):
    run = RunStore.open(tmp_path, "run1", RUN)
    before = (run.root / "manifest.json").read_bytes()
    with pytest.raises(ValueError, match="new run_id"):
        RunStore.open(tmp_path, "run1", {**RUN, "rules_sha256": "f" * 64})
    assert (run.root / "manifest.json").read_bytes() == before


def test_incomplete_bindings_rejected(tmp_path):
    with pytest.raises(ValueError):
        RunStore.open(tmp_path, "run1", {k: v for k, v in RUN.items() if k != "scope"})


def test_stage_uses_snapshot_store_under_stage_directory(tmp_path):
    run = RunStore.open(tmp_path, "run1", RUN)
    store = run.stage("ingest")
    assert isinstance(store, SnapshotStore) and store.root == run.root / "ingest"


def test_commit_records_checkpoint_and_stage_reusable(tmp_path):
    run = RunStore.open(tmp_path, "run1", RUN)
    assert not run.reusable("ingest", RUN)
    store = build(run, tmp_path, "ingest", "s1")
    assert not run.reusable("ingest", RUN)  # published but not checkpointed
    entry = run.commit("ingest", "s1")
    assert entry == {"snapshot_id": "s1", "manifest_sha256": store.current()["manifest_sha256"]}
    assert run.checkpoints() == {"ingest": entry}
    assert run.reusable("ingest", RUN)
    assert not run.reusable("ingest", {**RUN, "cutoff_yyyymm": "202608"})
    assert not run.reusable("other", RUN)


def test_checkpoint_replace_is_atomic(tmp_path, monkeypatch):
    run = RunStore.open(tmp_path, "run1", RUN)
    build(run, tmp_path, "ingest", "s1")
    run.commit("ingest", "s1")
    before = (run.root / "checkpoints.json").read_bytes()
    build(run, tmp_path, "convert", "s2")
    original = os.replace

    def fail(source, target):
        if Path(target).name == "checkpoints.json":
            raise OSError("simulated interruption")
        return original(source, target)
    monkeypatch.setattr("lvr_pipeline.storage.runs.os.replace", fail)
    with pytest.raises(OSError):
        run.commit("convert", "s2")
    assert (run.root / "checkpoints.json").read_bytes() == before
    assert not (run.root / ".run-lock").exists()
    monkeypatch.undo()
    run.commit("convert", "s2")
    assert set(run.checkpoints()) == {"ingest", "convert"}


def test_commit_rejects_unverified_snapshot_and_requires_lock(tmp_path):
    run = RunStore.open(tmp_path, "run1", RUN)
    build(run, tmp_path, "ingest", "s1")
    (run.root / "ingest/snapshots/s1/out.bin").write_bytes(b"tampered")
    with pytest.raises(ValueError):
        run.commit("ingest", "s1")
    assert not (run.root / "checkpoints.json").exists()
    build(run, tmp_path, "convert", "s2")
    (run.root / ".run-lock").mkdir()
    with pytest.raises(RuntimeError, match="lock"):
        run.commit("convert", "s2")
    assert (run.root / ".run-lock").exists() and not (run.root / "checkpoints.json").exists()


def test_tampered_file_makes_stage_not_reusable(tmp_path):
    run = RunStore.open(tmp_path, "run1", RUN)
    build(run, tmp_path, "ingest", "s1")
    run.commit("ingest", "s1")
    (run.root / "ingest/snapshots/s1/out.bin").write_bytes(b"tampered")
    assert not run.reusable("ingest", RUN)


def test_legacy_work_snapshots_untouched(tmp_path):
    legacy = SnapshotStore(tmp_path / "work")
    legacy.begin("old", expected_parent=None, bindings=SNAP)
    source = tmp_path / "x.bin"
    source.write_bytes(b"x")
    legacy.add("old", "out.bin", source, format_name="binary", row_count=None)
    pointer = legacy.publish("old", expected_parent=None)
    RunStore.open(tmp_path / "runs", "run1", RUN)
    assert SnapshotStore(tmp_path / "work").current() == pointer
