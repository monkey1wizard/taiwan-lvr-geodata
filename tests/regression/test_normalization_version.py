# -*- coding: utf-8 -*-
"""R04-6, tightened by R07-6a: a snapshot made under an older NORMALIZATION_VERSION is rejected, not merely skipped."""
import json

import pytest

from lvr_pipeline.addresses import identity
from lvr_pipeline.transactions.normalize import normalize
from lvr_pipeline.storage.runs import SnapshotStore
from tests.integration.test_p1_conversion import CODE, ingest, make_source, rules


def test_version_constant_is_declared():
    assert isinstance(identity.NORMALIZATION_VERSION, str) and identity.NORMALIZATION_VERSION


def test_old_version_snapshot_is_rejected_under_new_version(tmp_path, monkeypatch):
    raw, manifest = make_source(tmp_path)
    work = tmp_path / "work"
    ingested = ingest(raw, manifest, ["115q1"], work, batch_rows=2, code_commit=CODE)
    monkeypatch.setattr(identity, "NORMALIZATION_VERSION", "v2.0-old")
    old = normalize(ingested, work, cutoff=202610, rules_path=rules(tmp_path), batch_rows=2, code_commit=CODE)
    old_bindings = json.loads((old / "manifest.json").read_text(encoding="utf-8"))["bindings"]
    monkeypatch.setattr(identity, "NORMALIZATION_VERSION", "v2.1-new")
    # R07-6a: the old-rule snapshot is the current pointer in `work`, so a new run there is refused too.
    with pytest.raises(ValueError, match="address rule v2.0-old"):
        normalize(ingested, work, cutoff=202610, rules_path=rules(tmp_path), batch_rows=2, code_commit=CODE)
    fresh = tmp_path / "fresh"
    new = normalize(ingested, fresh, cutoff=202610, rules_path=rules(tmp_path), batch_rows=2, code_commit=CODE)
    new_bindings = json.loads((new / "manifest.json").read_text(encoding="utf-8"))["bindings"]
    assert new != old
    assert new_bindings["config_sha256"] != old_bindings["config_sha256"]
    store = SnapshotStore(work / "normalized")
    # R07-6a: a snapshot whose rule version differs from the running code is never read.
    with pytest.raises(ValueError, match="address rule v2.0-old"):
        store.reusable(old.name, old_bindings)
    with pytest.raises(ValueError, match="address rule v2.0-old"):
        store.reusable(old.name, new_bindings)
