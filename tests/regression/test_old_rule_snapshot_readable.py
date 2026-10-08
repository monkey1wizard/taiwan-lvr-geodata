# -*- coding: utf-8 -*-
"""R03-12: a snapshot made under an older NORMALIZATION_VERSION stays readable but is never reusable."""
import json

import pytest

from lvr_pipeline.addresses import identity
from lvr_pipeline.transactions import normalize as normalize_module
from lvr_pipeline.transactions.normalize import normalize
from lvr_pipeline.storage.runs import OLD_RULE_NOTE, SnapshotStore
from tests.integration.test_p1_conversion import CODE, ingest, make_source, rules


def _change_key_rule(monkeypatch, version):
    """Simulate a rule change: new version string and a building_key_v2 that returns different keys."""
    original = identity.building_key_v2
    monkeypatch.setattr(identity, "NORMALIZATION_VERSION", version)
    changed = lambda address: None if original(address) is None else original(address).replace("door", "gate")
    monkeypatch.setattr(identity, "building_key_v2", changed)
    monkeypatch.setattr(normalize_module, "building_key_v2", changed)


@pytest.fixture
def old_snapshot(tmp_path, monkeypatch):
    raw, manifest = make_source(tmp_path)
    work = tmp_path / "work"
    ingested = ingest(raw, manifest, ["115q1"], work, batch_rows=2, code_commit=CODE)
    monkeypatch.setattr(identity, "NORMALIZATION_VERSION", "v2.0-old")
    old = normalize(ingested, work, cutoff=202610, rules_path=rules(tmp_path), batch_rows=2, code_commit=CODE)
    return tmp_path, work, ingested, old


def test_old_rule_snapshot_is_readable_and_can_be_previous(old_snapshot, monkeypatch):
    tmp_path, work, ingested, old = old_snapshot
    store = SnapshotStore(work / "normalized")
    assert store.current()["snapshot_id"] == old.name
    _change_key_rule(monkeypatch, "v2.9-new")
    assert store.current()["snapshot_id"] == old.name  # was: Component key does not match v2 rule
    assert store.key_rule_status(old) == OLD_RULE_NOTE
    new = normalize(ingested, work, cutoff=202610, rules_path=rules(tmp_path), batch_rows=2, code_commit=CODE)
    assert new != old
    manifest = json.loads((new / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["parent_snapshot"] == old.name


def test_old_rule_snapshot_is_not_reusable(old_snapshot, monkeypatch):
    _, work, _, old = old_snapshot
    store = SnapshotStore(work / "normalized")
    old_bindings = json.loads((old / "manifest.json").read_text(encoding="utf-8"))["bindings"]
    assert store.reusable(old.name, old_bindings)
    _change_key_rule(monkeypatch, "v2.9-new")
    assert not store.reusable(old.name, old_bindings)


def test_old_rule_snapshot_still_fails_on_hash_mismatch(old_snapshot, monkeypatch):
    _, work, _, old = old_snapshot
    _change_key_rule(monkeypatch, "v2.9-new")
    target = next(path for path in sorted(old.glob("*.parquet")) + sorted(old.rglob("*.parquet")))
    with target.open("ab") as stream:
        stream.write(b"x")
    with pytest.raises(ValueError):
        SnapshotStore(work / "normalized").verify(old)


def test_same_version_with_wrong_key_still_fails(old_snapshot, monkeypatch):
    _, work, _, old = old_snapshot
    store = SnapshotStore(work / "normalized")
    store.verify(old)
    original = identity.building_key_v2
    # Same version string, but the stored keys no longer match the rule: must still fail.
    monkeypatch.setattr(identity, "building_key_v2", lambda address: None if original(address) is None else original(address).replace("door", "gate"))
    assert store.key_rule_status(old) == "recomputed"
    with pytest.raises(ValueError, match="Component key does not match v2 rule"):
        store.verify(old)


from lvr_pipeline.converted import export_converted


def _converted(old, work):
    return export_converted(old, work, code_commit=CODE)


def test_converted_snapshot_records_input_normalization_version(old_snapshot):
    _, work, _, old = old_snapshot
    converted = _converted(old, work)
    store = SnapshotStore(work / "converted")
    manifest = json.loads((converted / "manifest.json").read_text(encoding="utf-8"))
    assert store.snapshot_normalization_version(converted, manifest) == "v2.0-old"


def test_converted_old_rule_is_readable_and_not_reusable(old_snapshot, monkeypatch):
    _, work, _, old = old_snapshot
    converted = _converted(old, work)
    store = SnapshotStore(work / "converted")
    bindings = json.loads((converted / "manifest.json").read_text(encoding="utf-8"))["bindings"]
    assert store.reusable(converted.name, bindings)
    _change_key_rule(monkeypatch, "v2.9-new")
    assert store.verify(converted)
    assert store.key_rule_status(converted) == OLD_RULE_NOTE
    assert not store.reusable(converted.name, bindings)


def test_converted_same_version_with_wrong_key_still_fails(old_snapshot, monkeypatch):
    _, work, _, old = old_snapshot
    converted = _converted(old, work)
    store = SnapshotStore(work / "converted")
    original = identity.building_key_v2
    monkeypatch.setattr(identity, "building_key_v2", lambda address: None if original(address) is None else original(address).replace("door", "gate"))
    assert store.key_rule_status(converted) == "recomputed"
    with pytest.raises(ValueError, match="Component key does not match v2 rule"):
        store.verify(converted)
