"""B-4 (b14): ledger-only TGOS snapshots, the source state record and response coordinates. Synthetic inputs only."""

import json
import shutil

import pytest

from lvr_pipeline.addresses import identity
from lvr_pipeline.storage.parquet import rows
from lvr_pipeline.storage.runs import SnapshotStore, sha256_file
from lvr_pipeline.tgos import (
    import_tgos,
    load_state,
    prepare_tgos,
    read_ledger,
    resolve_source_state,
    transition_batch,
)
from test_p1_conversion import ADDRESS
from test_p2_offline import index_of, run
from test_p3_tgos import response

LEDGER_DATASETS = {"tgos-batch", "tgos-query", "tgos-result", "alias-event", "verified-alias"}
FORBIDDEN_FILES = {
    "address_index.parquet", "address_observations.parquet", "address_occurrences.parquet",
    "unique_addresses.parquet", "unmatched_addresses.parquet", "coordinate_resolutions.parquet",
    "district_candidates.parquet", "tgos_results.parquet",
}
ROAD = "臺北市中正區測試路"
ADOPT, REJECT, MISSING = ROAD + "21號", ROAD + "22號", ROAD + "23號"


def _world(tmp_path):
    converted, *_, state = run(
        tmp_path,
        coordinates=[(ADDRESS, "63", "6300100", 121.5, 25.0)],
        records=[{"address": a, "date": "1150102"} for a in [ADDRESS, ADOPT, REJECT, MISSING]],
    )
    work = tmp_path / "work"
    prepared, _ = prepare_tgos(state, work, tmp_path / "exchange", index=index_of(state))
    batch_id = list(rows(prepared / "tgos_batches.parquet"))[0]["batch_id"]
    return state, work, prepared, batch_id


def _imported(tmp_path):
    state, work, prepared, batch_id = _world(tmp_path)
    submitted = transition_batch(prepared, work, batch_id, "submitted")
    path = tmp_path / "finish.csv"
    response(path, [
        {"Address": ADOPT, "Response_Address": ADOPT, "Response_X": "121.51", "Response_Y": "25.01"},
        # A numeric coordinate for another door: rejected, but the returned pair is kept.
        {"Address": REJECT, "Response_Address": ROAD + "99號", "Response_X": "121.52", "Response_Y": "25.02"},
        {"Address": MISSING, "Response_Address": "", "Response_X": "", "Response_Y": ""},
    ])
    imported = import_tgos(submitted, work, batch_id, path, index=index_of(submitted))
    return state, work, prepared, submitted, imported, batch_id


def test_every_command_writes_a_ledger_only_snapshot_that_records_its_source(tmp_path):
    state, work, prepared, submitted, imported, _ = _imported(tmp_path)
    for snapshot in (prepared, submitted, imported):
        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
        assert {item["schema"] for item in manifest["artifacts"] if item["schema"]} == LEDGER_DATASETS
        assert {p.name for p in snapshot.iterdir()} & FORBIDDEN_FILES == set()
        report = json.loads((snapshot / "quality.json").read_text(encoding="utf-8"))
        assert set(report["dataset_counts"]) == LEDGER_DATASETS
        assert report["source_state"] == {
            "run_id": "work", "stage": "offline-state", "snapshot_id": state.name,
            "manifest_sha256": sha256_file(state / "manifest.json"),
            "index_manifest_sha256": json.loads((state / "quality.json").read_text(encoding="utf-8"))["index_manifest_sha256"],
        }
        # The source manifest hash is a binding input of the ledger snapshot.
        assert sha256_file(state / "manifest.json") in manifest["bindings"]["input_sha256"]
        assert resolve_source_state(snapshot, tmp_path) == state
    assert load_state(imported)[1]["status_counts"]["located"] == 2
    # The first snapshot has the offline state as parent; later ones chain by expected_parent.
    parents = [json.loads((s / "manifest.json").read_text(encoding="utf-8"))["parent_snapshot"]
               for s in (prepared, submitted, imported)]
    assert parents == [None, prepared.name, submitted.name]


def test_result_rows_keep_the_returned_coordinate_even_when_rejected(tmp_path):
    *_, imported, _ = _imported(tmp_path)
    results = {r["address"]: r for r in rows(imported / "tgos_imports.parquet")}
    adopted, rejected, missing = results[ADOPT], results[REJECT], results[MISSING]
    assert (adopted["status"], adopted["lng"], adopted["lat"]) == ("succeeded", 121.51, 25.01)
    assert (adopted["response_lng"], adopted["response_lat"]) == (121.51, 25.01)
    assert rejected["status"] == "rejected" and rejected["lng"] is None and rejected["lat"] is None
    assert (rejected["response_lng"], rejected["response_lat"]) == (121.52, 25.02)
    assert missing["status"] == "failed" and missing["response_lng"] is None and missing["response_lat"] is None


def test_source_state_with_another_manifest_hash_is_refused(tmp_path):
    state, work, _, _, imported, _ = _imported(tmp_path)
    assert resolve_source_state(imported, tmp_path) == state
    manifest = state / "manifest.json"
    original = manifest.read_bytes()
    manifest.write_bytes(original + b"\n")
    try:
        with pytest.raises(ValueError, match="manifest hash differs"):
            resolve_source_state(imported, tmp_path)
        with pytest.raises(ValueError, match="manifest hash differs"):
            transition_batch(imported, work, "none", "cancelled")
    finally:
        manifest.write_bytes(original)
    assert resolve_source_state(imported, tmp_path) == state


def test_missing_source_state_is_refused(tmp_path):
    _, _, _, _, imported, _ = _imported(tmp_path)
    with pytest.raises(ValueError, match="source state is missing"):
        resolve_source_state(imported, tmp_path / "elsewhere")


def test_ledger_made_under_another_rule_version_is_refused_by_commands_but_still_readable_for_carry(tmp_path, monkeypatch):
    _, _, _, _, imported, _ = _imported(tmp_path)
    monkeypatch.setattr(identity, "NORMALIZATION_VERSION", "v0.0-test")
    # The ledger records the rule version it was written under; a standalone command refuses it (R07-6a).
    with pytest.raises(ValueError, match="address rule"):
        load_state(imported)
    with pytest.raises(ValueError, match="address rule"):
        transition_batch(imported, tmp_path / "work", "none", "cancelled")
    # Rule 4: batches and query texts are evidence and can be read across rule versions.
    _, _, batches, queries = read_ledger(imported)
    assert len(batches) == 1 and len(queries) == 3


def test_ledger_of_another_epoch_is_refused(tmp_path):
    *_, imported, _ = _imported(tmp_path)
    copy = tmp_path / "copy" / "tgos-state"
    shutil.copytree(imported.parent.parent, copy)
    snapshot = copy / "snapshots" / imported.name
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    manifest["bindings"]["rebuild_epoch"] = "2026-01-01"
    (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="predates"):
        read_ledger(snapshot)
    assert SnapshotStore(copy).root == copy


def test_prepare_skips_keys_already_adopted_from_the_ledger_without_copying_tables(tmp_path):
    state, work, _, _, imported, _ = _imported(tmp_path)
    # The next batch leaves the adopted key and the two doors that were already queried alone.
    with pytest.raises(ValueError, match="No eligible TGOS candidates"):
        prepare_tgos(imported, work, tmp_path / "exchange", index=index_of(state))
    assert len(list((work / "tgos-state" / "snapshots").iterdir())) == 3


# B-4 follow-up: a pool address whose district text does not give back its door must not stop review or prepare.
TWO_TOWNS = "臺東縣太麻里鄉卑南鄉溫泉村龍泉路53巷3號8樓之20"  # from the real b16 run; two town names, no town found


def test_district_text_that_does_not_round_trip_is_none_for_a_real_pool_address():
    from lvr_pipeline.addresses.pool import district_query
    from lvr_pipeline.tgos.batches import district_query_text, district_text_or_none

    parsed = district_query(TWO_TOWNS)
    assert parsed is not None
    with pytest.raises(ValueError, match="does not give back its door"):
        district_query_text(parsed[0], parsed)
    assert district_text_or_none(parsed[0], parsed) is None


def test_prepare_counts_and_skips_a_district_address_whose_text_does_not_round_trip(tmp_path, monkeypatch):
    from lvr_pipeline.tgos import batches

    odd, fine = "臺北市測試路98號", "臺北市測試路99號"
    *_, state = run(
        tmp_path,
        coordinates=[(ADDRESS, "63", "6300100", 121.5, 25.0),
                     ("臺北市文山區測試路11號", "63", "6300800", 121.55, 25.02)],
        records=[{"address": a, "date": "1150102"} for a in (ADDRESS, odd, fine)],
    )
    real = batches.district_query_text

    def text(county, parsed):
        if parsed[3] == "98":
            raise ValueError("TGOS district query text does not give back its door: x")
        return real(county, parsed)

    monkeypatch.setattr(batches, "district_query_text", text)
    prepared, _ = prepare_tgos(state, tmp_path / "work", tmp_path / "exchange", index=index_of(state))
    assert [q["address"] for q in rows(prepared / "tgos_queries.parquet")] == [fine]
    assert load_state(prepared)[1]["tgos_district_text_not_round_trip"] == 1
