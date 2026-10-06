"""V-04 failure scenarios for resumable runs. Synthetic inputs only."""
import errno
import json
import os
from pathlib import Path
import shutil

import pytest

from lvr_pipeline.storage.runs import RunStore, SnapshotStore

RUN = {"code_commit": "9fec324" + "0" * 33, "raw_manifest_sha256": "a" * 64, "address_source_sha256": "b" * 64,
       "rules_sha256": "c" * 64, "contract_version": "1.0", "key_version": "building_key_v2",
       "cutoff_yyyymm": "202609", "scope": "all"}
SNAP = {"code_commit": RUN["code_commit"], "schema_version": "1.0", "config_sha256": "d" * 64, "input_sha256": ["e" * 64]}


def write_stage(store, tmp_path, snapshot_id, parent=None, files=("a.bin", "b.bin")):
    store.begin(snapshot_id, expected_parent=parent, bindings=SNAP)
    for name in files:
        source = tmp_path / f"{snapshot_id}-{name}"
        source.write_bytes(f"{snapshot_id}:{name}".encode())
        store.add(snapshot_id, name, source, format_name="binary", row_count=None)


def published(run, tmp_path, stage="convert", snapshot_id="s1"):
    store = run.stage(stage)
    write_stage(store, tmp_path, snapshot_id)
    store.publish(snapshot_id, expected_parent=None)
    run.commit(stage, snapshot_id)
    return store


def snapshot(path):
    return sorted((p.relative_to(path).as_posix(), p.read_bytes()) for p in path.rglob("*") if p.is_file())


def test_mid_stage_failure_publishes_nothing(tmp_path, monkeypatch):
    run = RunStore.open(tmp_path / "runs", "run1", RUN)
    store = published(run, tmp_path)
    pointer, checkpoints = (store.root / "current.json").read_bytes(), (run.root / "checkpoints.json").read_bytes()
    store.begin("s2", expected_parent="s1", bindings=SNAP)
    first = tmp_path / "first.bin"
    first.write_bytes(b"1")
    store.add("s2", "a.bin", first, format_name="binary", row_count=None)
    original = shutil.copyfileobj
    monkeypatch.setattr("lvr_pipeline.storage.runs.shutil.copyfileobj", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        store.add("s2", "b.bin", first, format_name="binary", row_count=None)
    monkeypatch.setattr("lvr_pipeline.storage.runs.shutil.copyfileobj", original)
    assert not (store.root / "snapshots/s2").exists()
    assert (store.root / "current.json").read_bytes() == pointer
    assert (run.root / "checkpoints.json").read_bytes() == checkpoints
    with pytest.raises(ValueError):  # the interrupted draft cannot be promoted
        store.publish("s2", expected_parent="s1")
    assert store.current()["snapshot_id"] == "s1" and run.reusable("convert", RUN)


def test_changed_binding_is_refused(tmp_path):
    run = RunStore.open(tmp_path / "runs", "run1", RUN)
    published(run, tmp_path)
    before = snapshot(run.root)
    with pytest.raises(ValueError, match="new run_id"):
        RunStore.open(tmp_path / "runs", "run1", {**RUN, "rules_sha256": "f" * 64})
    assert snapshot(run.root) == before
    assert not run.reusable("convert", {**RUN, "rules_sha256": "f" * 64})


def test_tampered_file_not_reusable_and_unreadable(tmp_path):
    run = RunStore.open(tmp_path / "runs", "run1", RUN)
    store = published(run, tmp_path)
    target = run.root / "convert/snapshots/s1/a.bin"
    data = bytearray(target.read_bytes())
    data[0] ^= 0xFF
    target.write_bytes(bytes(data))
    assert run.reusable("convert", RUN) is False
    with pytest.raises(ValueError):
        store.verify(store.root / "snapshots/s1")
    with pytest.raises(ValueError):
        store.current()


def test_stale_writer_rejected_and_previous_snapshot_readable(tmp_path):
    run = RunStore.open(tmp_path / "runs", "run1", RUN)
    store = published(run, tmp_path)
    write_stage(store, tmp_path, "fresh", "s1")
    write_stage(store, tmp_path, "stale", "s1")
    store.publish("fresh", expected_parent="s1")
    with pytest.raises(ValueError, match="Stale"):
        store.publish("stale", expected_parent="s1")
    assert store.current()["snapshot_id"] == "fresh"
    assert store.verify(store.root / "snapshots/s1")["snapshot_id"] == "s1"
    assert not (store.root / "snapshots/stale").exists()


def test_disk_full_keeps_previous_version(tmp_path, monkeypatch):
    run = RunStore.open(tmp_path / "runs", "run1", RUN)
    store = published(run, tmp_path)
    pointer = (store.root / "current.json").read_bytes()
    write_stage(store, tmp_path, "s2", "s1")
    original = os.fsync

    def full(fd):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr("lvr_pipeline.storage.runs.os.fsync", full)
    with pytest.raises(OSError) as raised:
        store.publish("s2", expected_parent="s1")
    assert raised.value.errno == errno.ENOSPC
    monkeypatch.setattr("lvr_pipeline.storage.runs.os.fsync", original)
    assert (store.root / "current.json").read_bytes() == pointer
    assert store.current()["snapshot_id"] == "s1"
    assert not (store.root / ".publish-lock").exists()
    assert run.reusable("convert", RUN)


def test_leftover_lock_gives_clear_error_and_is_not_removed(tmp_path):
    run = RunStore.open(tmp_path / "runs", "run1", RUN)
    store = run.stage("convert")
    write_stage(store, tmp_path, "s1")
    (store.root / ".publish-lock").mkdir()
    with pytest.raises(RuntimeError, match="stale lock"):
        store.publish("s1", expected_parent=None)
    assert (store.root / ".publish-lock").is_dir()
    assert store.current() is None
    (run.root / ".run-lock").mkdir()
    with pytest.raises(RuntimeError, match="stale lock"):
        RunStore.open(tmp_path / "runs", "run1", RUN)
    assert (run.root / ".run-lock").is_dir()


def test_disk_full_attempt_can_be_retried(tmp_path, monkeypatch):
    run = RunStore.open(tmp_path / "runs", "run1", RUN)
    store = published(run, tmp_path)
    write_stage(store, tmp_path, "s2", "s1")
    original = os.fsync

    def full(fd):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr("lvr_pipeline.storage.runs.os.fsync", full)
    with pytest.raises(OSError):
        store.publish("s2", expected_parent="s1")
    monkeypatch.setattr("lvr_pipeline.storage.runs.os.fsync", original)
    store.publish("s2", expected_parent="s1")
    assert store.current()["snapshot_id"] == "s2"
