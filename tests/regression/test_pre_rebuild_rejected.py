# -*- coding: utf-8 -*-
"""R07-6a: snapshots, runs and source inventories from before the 2026-10-12 rebuild are never read.

Replaces R03-12, which kept older-rule snapshots readable. Within one rebuild run, a snapshot
made under the current rule and epoch stays reusable.
"""
import json

import pytest

from lvr_pipeline.addresses import identity
from lvr_pipeline.converted import export_converted
from lvr_pipeline.pipeline import run_full
from lvr_pipeline.storage.runs import (
    REBUILD_EPOCH,
    SOURCE_CHECK_PENDING,
    RunStore,
    SnapshotStore,
    Stage,
    load_snapshot,
    rebuild_source_failures,
)
from lvr_pipeline.transactions.normalize import normalize
from tests.integration.test_p1_conversion import CODE, ingest, make_source, rules


def _break_key_rule(monkeypatch):
    """Same version string, but the rule now gives different keys."""
    original = identity.building_key
    monkeypatch.setattr(identity, "building_key",
                        lambda address: None if original(address) is None else original(address).replace("door", "gate"))


@pytest.fixture
def current_snapshot(tmp_path):
    raw, manifest = make_source(tmp_path)
    work = tmp_path / "work"
    ingested = ingest(raw, manifest, ["115q1"], work, batch_rows=2, code_commit=CODE)
    normalized = normalize(ingested, work, cutoff=202610, rules_path=rules(tmp_path), batch_rows=2, code_commit=CODE)
    return work, normalized


@pytest.fixture
def old_rule_snapshot(tmp_path, monkeypatch):
    raw, manifest = make_source(tmp_path)
    work = tmp_path / "work"
    ingested = ingest(raw, manifest, ["115q1"], work, batch_rows=2, code_commit=CODE)
    monkeypatch.setattr(identity, "NORMALIZATION_VERSION", "v2.0-old")
    old = normalize(ingested, work, cutoff=202610, rules_path=rules(tmp_path), batch_rows=2, code_commit=CODE)
    monkeypatch.undo()
    return work, old


def _manifest(path):
    return json.loads((path / "manifest.json").read_text(encoding="utf-8"))


def _drop_epoch(snapshot):
    manifest = _manifest(snapshot)
    del manifest["bindings"]["rebuild_epoch"]
    (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest["bindings"]


def test_current_snapshot_carries_epoch_and_is_reusable(current_snapshot):
    work, normalized = current_snapshot
    bindings = _manifest(normalized)["bindings"]
    assert bindings["rebuild_epoch"] == REBUILD_EPOCH
    assert SnapshotStore(work / "normalized").reusable(normalized.name, bindings)


def test_snapshot_without_epoch_is_rejected_everywhere(current_snapshot):
    work, normalized = current_snapshot
    bindings = _drop_epoch(normalized)
    store = SnapshotStore(work / "normalized")
    with pytest.raises(ValueError, match="predates the 2026-10-12 rebuild"):
        store.verify(normalized)
    with pytest.raises(ValueError, match="predates the 2026-10-12 rebuild"):
        load_snapshot(normalized, "normalize")
    with pytest.raises(ValueError, match="predates the 2026-10-12 rebuild"):
        store.reusable(normalized.name, bindings)
    with pytest.raises(ValueError, match="predates the 2026-10-12 rebuild"):
        export_converted(normalized, work, code_commit=CODE)


def test_old_rule_snapshot_is_rejected_not_tolerated(old_rule_snapshot):
    work, old = old_rule_snapshot
    store = SnapshotStore(work / "normalized")
    with pytest.raises(ValueError, match="address rule v2.0-old"):
        store.verify(old)
    with pytest.raises(ValueError, match="address rule v2.0-old"):
        load_snapshot(old, "normalize")
    with pytest.raises(ValueError, match="address rule v2.0-old"):
        store.reusable(old.name, _manifest(old)["bindings"])


def test_existing_old_rule_snapshot_blocks_stage_reuse(old_rule_snapshot):
    work, old = old_rule_snapshot
    bindings = _manifest(old)["bindings"]
    with pytest.raises(ValueError, match="address rule v2.0-old"):
        Stage(work / "normalized", "normalize", bindings, old.name)


def test_same_version_with_wrong_key_still_fails(current_snapshot, monkeypatch):
    work, normalized = current_snapshot
    store = SnapshotStore(work / "normalized")
    store.verify(normalized)
    _break_key_rule(monkeypatch)
    with pytest.raises(ValueError, match="Component key does not match v2 rule"):
        store.verify(normalized)


def test_converted_snapshot_records_input_normalization_version(current_snapshot):
    work, normalized = current_snapshot
    converted = export_converted(normalized, work, code_commit=CODE)
    manifest = _manifest(converted)
    assert SnapshotStore.snapshot_normalization_version(converted, manifest) == identity.NORMALIZATION_VERSION
    assert manifest["bindings"]["rebuild_epoch"] == REBUILD_EPOCH


def _run_bindings(**changes):
    bindings = {"code_commit": CODE, "raw_manifest_sha256": "a" * 64, "address_source_sha256": "b" * 64,
                "rules_sha256": "c" * 64, "contract_version": "1.0", "key_version": "building_key/v2.5",
                "cutoff_yyyymm": "202610", "scope": "full", "rebuild_epoch": REBUILD_EPOCH}
    bindings.update(changes)
    return {key: value for key, value in bindings.items() if value is not None}


def test_run_without_epoch_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Run predates the 2026-10-12 rebuild"):
        RunStore.open(tmp_path, "r1", _run_bindings(rebuild_epoch=None))
    with pytest.raises(ValueError, match="Run predates the 2026-10-12 rebuild"):
        RunStore.open(tmp_path, "r1", _run_bindings(rebuild_epoch="2026-10-01"))


def test_pre_rebuild_run_directory_cannot_be_resumed(tmp_path):
    old = _run_bindings(rebuild_epoch=None)
    run_dir = tmp_path / "r1"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(json.dumps({"run_id": "r1", "bindings": old}), encoding="utf-8")
    with pytest.raises(ValueError, match="different bindings"):
        RunStore.open(tmp_path, "r1", _run_bindings())


VERIFIED = {"status": "verified", "checked_by": "owner", "checked_at": "2026-10-12", "evidence": "source page check"}


@pytest.mark.parametrize("document,codes", [
    ({"license_status": "legacy_documentation_only_pending_current_source_check"},
     ["source_predates_rebuild", "source_not_verified"]),
    ({"rebuild_epoch": REBUILD_EPOCH, "source_check": dict(SOURCE_CHECK_PENDING)}, ["source_not_verified"]),
    ({"rebuild_epoch": REBUILD_EPOCH, "source_check": {**VERIFIED, "evidence": ""}}, ["source_check_incomplete"]),
    ({"rebuild_epoch": "2026-10-01", "source_check": VERIFIED}, ["source_predates_rebuild"]),
    ({"rebuild_epoch": REBUILD_EPOCH, "source_check": VERIFIED}, []),
])
def test_rebuild_source_failures(document, codes):
    assert [failure["code"] for failure in rebuild_source_failures(document, "raw manifest")] == codes


def test_run_full_refuses_unverified_sources_before_writing(tmp_path):
    manifest = tmp_path / "raw_manifest.json"
    manifest.write_text(json.dumps({"license_status": "legacy_documentation_only_pending_current_source_check"}),
                        encoding="utf-8")
    address = tmp_path / "address_source.json"
    address.write_text(json.dumps({"rebuild_epoch": REBUILD_EPOCH, "source_check": dict(SOURCE_CHECK_PENDING)}),
                       encoding="utf-8")
    runs = tmp_path / "runs"
    result = run_full(run_id="r1", cutoff=202610, address_dir=tmp_path, notices_path=tmp_path / "notice.json",
                      runs_root=runs, raw_dir=tmp_path, manifest_path=manifest, address_source=address)
    assert result["completed"] is False
    assert [(f["detail"].split(" ")[0] + " " + f["detail"].split(" ")[1], f["code"]) for f in result["failures"]] == [
        ("raw manifest", "source_predates_rebuild"), ("raw manifest", "source_not_verified"),
        ("address source", "source_not_verified")]
    assert not runs.exists()
