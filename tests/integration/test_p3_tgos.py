"""P3 TGOS quota, exchange, strict import, and backfill acceptance."""

import csv
import json

import pytest

from lvr_pipeline.backfill import backfill_output
from lvr_pipeline.tgos import tgos_log_date as _tgos_log_date
from lvr_pipeline.packaging import package_output, verify_output
from lvr_pipeline.storage.parquet import rows
from lvr_pipeline.tgos import (
    _round_robin,
    import_tgos,
    load_state,
    prepare_tgos,
    repair_prepared_exchange,
    revoke_alias,
    transition_batch,
)
from test_p1_conversion import ADDRESS
from test_p2_offline import run


NOTICE = {
    "publication_authorized": True,
    "legacy_coordinates_authorized": True,
    "sources": ["synthetic"],
}


def test_tgos_date_file_is_log_only(tmp_path):
    date_file = tmp_path / "date.json"
    date_file.write_text('{"date":"2026-10-07"}', encoding="utf-8")
    assert _tgos_log_date(date_file) == "2026-10-07"

    date_file.write_text(
        '{"date":"2026-10-07","limit":10000}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="only the date field"):
        _tgos_log_date(date_file)


def fixture_state(tmp_path):
    other = "臺北市中正區測試路11號"
    converted, *_, state = run(
        tmp_path,
        coordinates=[(ADDRESS, "63", "6300100", 121.5, 25.0)],
        records=[
            {"address": ADDRESS, "date": "1141102"},
            {"address": other, "date": "1150102"},
        ],
    )
    return converted, state, other


def fixture_two_unmatched(tmp_path):
    addresses = ["臺北市中正區測試路21號", "臺北市中正區測試路22號"]
    converted, *_, state = run(
        tmp_path,
        coordinates=[(ADDRESS, "63", "6300100", 121.5, 25.0)],
        records=[
            {"address": ADDRESS, "date": "1141102"},
            {"address": addresses[0], "date": "1150102"},
            {"address": addresses[1], "date": "1150103"},
        ],
    )
    return converted, state, addresses


def response(path, values):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["Address", "Response_Address", "Response_X", "Response_Y"],
        )
        writer.writeheader()
        writer.writerows(values)


def test_quota_is_reserved_before_utf8_sig_handoff(tmp_path):
    _, state, address = fixture_state(tmp_path)
    prepared, exchange = prepare_tgos(
        state,
        tmp_path / "work",
        tmp_path / "exchange",
        exchange_date="2026-10-07",
    )
    batch_id = list(rows(prepared / "tgos_batches.parquet"))[0]["batch_id"]
    assert exchange.name == f"20261007-{batch_id.removeprefix('tgos-')}"
    assert (exchange / "addresses.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    assert (exchange / "addresses.csv").read_text(encoding="utf-8-sig").splitlines() == [
        "id,Address,Response_Address,Response_X,Response_Y",
        f"1,{address},,,",
    ]
    with (exchange / "addresses.csv").open(
        encoding="utf-8-sig", newline=""
    ) as stream:
        submitted = list(csv.DictReader(stream))
    assert list(submitted[0]) == [
        "id",
        "Address",
        "Response_Address",
        "Response_X",
        "Response_Y",
    ]
    assert submitted == [
        {
            "id": "1",
            "Address": address,
            "Response_Address": "",
            "Response_X": "",
            "Response_Y": "",
        }
    ]
    manifest = json.loads((exchange / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["address_count"] == 1 and manifest["coordinate_system"] == "WGS84"
    _, report, stage = load_state(prepared)
    assert stage == "tgos-state" and report["tgos_batch_count"] == 1
    batch = list(rows(prepared / "tgos_batches.parquet"))[0]
    assert batch["status"] == "prepared"
    with pytest.raises(ValueError, match="latest snapshot"):
        prepare_tgos(
            state,
            tmp_path / "work",
            tmp_path / "exchange",
        )


def test_prepared_exchange_can_be_repaired_without_new_reservation(tmp_path):
    _, state, address = fixture_state(tmp_path)
    prepared, exchange = prepare_tgos(
        state,
        tmp_path / "work",
        tmp_path / "exchange",
        limit=1,
    )
    batch_id = list(rows(prepared / "tgos_batches.parquet"))[0]["batch_id"]
    (exchange / "addresses.csv").write_text(
        f"Address\r\n{address}\r\n", encoding="utf-8-sig", newline=""
    )
    repaired, repaired_exchange = repair_prepared_exchange(
        prepared,
        tmp_path / "work",
        tmp_path / "exchange",
        batch_id,
    )
    assert repaired != prepared
    assert (repaired_exchange / "addresses.csv").read_text(
        encoding="utf-8-sig"
    ).splitlines() == [
        "id,Address,Response_Address,Response_X,Response_Y",
        f"1,{address},,,",
    ]
    batches = list(rows(repaired / "tgos_batches.parquet"))
    assert len(batches) == 1
    assert batches[0]["status"] == "prepared"
    assert batches[0]["address_count"] == 1


def test_invalid_batch_limit_fails(tmp_path):
    _, state, _ = fixture_state(tmp_path)
    with pytest.raises(ValueError, match="file limit"):
        prepare_tgos(
            state,
            tmp_path / "quota-work",
            tmp_path / "exchange",
            limit=0,
        )


def test_ten_thousand_cap_fairness_and_cancellation_state(tmp_path):
    candidates = [
        {"address_family": "large", "building_key": f"v2:{i:05d}"}
        for i in range(10_000)
    ] + [{"address_family": "small", "building_key": "v2:small"}]
    ordered = _round_robin(candidates)
    assert len(ordered[:10_000]) == 10_000
    assert ordered[1]["address_family"] == "small"
    assert ordered[-1]["address_family"] == "large"

    _, state, _ = fixture_state(tmp_path)
    prepared, _ = prepare_tgos(
        state,
        tmp_path / "work",
        tmp_path / "exchange",
        limit=1,
    )
    batch_id = list(rows(prepared / "tgos_batches.parquet"))[0]["batch_id"]
    cancelled = transition_batch(
        prepared, tmp_path / "work", batch_id, "cancelled", reason="not uploaded"
    )
    batch = list(rows(cancelled / "tgos_batches.parquet"))[0]
    query = list(rows(cancelled / "tgos_queries.parquet"))[0]
    assert batch["status"] == "cancelled" and query["status"] == "cancelled"
    with pytest.raises(ValueError, match="No eligible"):
        prepare_tgos(
            cancelled,
            tmp_path / "work",
            tmp_path / "exchange",
        )

    fingerprint = query["query_fingerprint"]
    retried, _ = prepare_tgos(
        cancelled,
        tmp_path / "work",
        tmp_path / "exchange",
        retry_fingerprints=[fingerprint],
        retry_reason="operator approved corrected retry",
    )
    batches = list(rows(retried / "tgos_batches.parquet"))
    assert batches[-1]["predecessor_batch_id"] == batch_id
    assert batches[-1]["reason"] == "operator approved corrected retry"


def test_strict_import_is_idempotent_and_rejects_axis_guessing(tmp_path):
    _, state, address = fixture_state(tmp_path)
    prepared, _ = prepare_tgos(
        state,
        tmp_path / "work",
        tmp_path / "exchange",
        limit=1,
    )
    batch_id = list(rows(prepared / "tgos_batches.parquet"))[0]["batch_id"]
    submitted = transition_batch(
        prepared, tmp_path / "work", batch_id, "submitted"
    )
    result = tmp_path / "result.csv"
    response(
        result,
        [
            {
                "Address": address,
                "Response_Address": address,
                "Response_X": "121.51",
                "Response_Y": "25.01",
            }
        ],
    )
    imported = import_tgos(submitted, tmp_path / "work", batch_id, result)
    _, report, _ = load_state(imported)
    assert report["status_counts"]["located"] == 2
    assert import_tgos(imported, tmp_path / "work", batch_id, result) == imported

    other = tmp_path / "axis"
    other.mkdir()
    _, state, address = fixture_state(other)
    prepared, _ = prepare_tgos(
        state,
        other / "work",
        other / "exchange",
        limit=1,
    )
    batch_id = list(rows(prepared / "tgos_batches.parquet"))[0]["batch_id"]
    unknown = transition_batch(
        prepared, other / "work", batch_id, "submission_unknown"
    )
    bad = other / "axis.csv"
    response(
        bad,
        [{"Address": address, "Response_Address": address, "Response_X": "25.01", "Response_Y": "121.51"}],
    )
    imported = import_tgos(unknown, other / "work", batch_id, bad)
    assert list(rows(imported / "tgos_imports.parquet"))[0]["status"] == "rejected"
    assert load_state(imported)[1]["status_counts"]["unmatched"] == 1


def _submit_first(tmp_path, state, work):
    prepared, _ = prepare_tgos(
        state,
        work,
        tmp_path / "exchange",
        limit=1,
    )
    query = list(rows(prepared / "tgos_queries.parquet"))[0]
    submitted = transition_batch(prepared, work, query["batch_id"], "submitted")
    return submitted, query


@pytest.mark.parametrize("returned", ["臺北市中正區測試路22號", "臺北市中正區測試路21號之1", "臺北市中正區別路21號"])
def test_response_for_another_door_is_isolated(tmp_path, returned):
    _, state, _ = fixture_two_unmatched(tmp_path)
    submitted, query = _submit_first(tmp_path, state, tmp_path / "work")
    if returned.endswith("22號") and query["address"].endswith("22號"):
        returned = returned.replace("22號", "21號")
    result = tmp_path / "result.csv"
    response(
        result,
        [{"Address": query["address"], "Response_Address": returned, "Response_X": "121.51", "Response_Y": "25.01"}],
    )
    imported = import_tgos(submitted, tmp_path / "work", query["batch_id"], result)
    imports = list(rows(imported / "tgos_imports.parquet"))
    assert imports[0]["status"] == "rejected"
    assert imports[0]["reason"] == "TGOS response address differs from submitted address"
    assert list(rows(imported / "verified_aliases.parquet")) == []
    assert load_state(imported)[1]["status_counts"]["located"] == 1


def test_ledger_prevents_resend_and_reopens_results_for_revalidation(tmp_path):
    _, state, _ = fixture_two_unmatched(tmp_path)
    submitted, first = _submit_first(tmp_path, state, tmp_path / "old")
    result = tmp_path / "result.csv"
    response(
        result,
        [{"Address": first["address"], "Response_Address": first["address"], "Response_X": "121.51", "Response_Y": "25.01"}],
    )
    old = import_tgos(submitted, tmp_path / "old", first["batch_id"], result)

    seeded, _ = prepare_tgos(
        state,
        tmp_path / "new",
        tmp_path / "exchange",
        ledger=old,
    )
    batches = list(rows(seeded / "tgos_batches.parquet"))
    queries = list(rows(seeded / "tgos_queries.parquet"))
    assert [row["status"] for row in batches] == ["submitted", "prepared"]
    assert batches[0]["response_sha256"] is None
    assert [row["address"] for row in queries].count(first["address"]) == 1
    assert list(rows(seeded / "tgos_imports.parquet")) == []
    assert load_state(seeded)[1]["status_counts"]["located"] == 1

    reimported = import_tgos(seeded, tmp_path / "new", first["batch_id"], result)
    assert load_state(reimported)[1]["status_counts"]["located"] == 2


def test_verified_alias_can_be_revoked(tmp_path):
    _, state, _ = fixture_two_unmatched(tmp_path)
    submitted, _ = _submit_first(tmp_path, state, tmp_path / "work")
    alias = {"key_version": "v2", "alias_key": "v2:a", "target_key": "v2:b", "evidence_ref": "manual"}
    from lvr_pipeline.tgos import _artifact_paths, _write_state

    manifest, _, _ = load_state(submitted)
    paths = _artifact_paths(submitted, manifest)
    seeded = _write_state(
        submitted,
        tmp_path / "work",
        action="seed-alias",
        batches=list(rows(paths["tgos-batch"])),
        queries=list(rows(paths["tgos-query"])),
        results=[],
        aliases=[alias],
        alias_events=[{"event_id": "seed", "key_version": "v2", "alias_key": "v2:a", "target_key": "v2:b", "action": "verified", "evidence_ref": "manual"}],
    )
    revoked = revoke_alias(
        seeded, tmp_path / "work", "v2:a", reason="operator rejected equivalence"
    )
    assert list(rows(revoked / "verified_aliases.parquet")) == []
    actions = [row["action"] for row in rows(revoked / "alias_events.parquet")]
    assert actions == ["verified", "revoked"]


def test_backfill_changes_only_affected_month_and_keeps_previous_readable(tmp_path):
    converted, state, address = fixture_state(tmp_path)
    previous = package_output(
        converted, state, tmp_path / "output", notices=NOTICE, run_id="before-tgos"
    )
    prepared, _ = prepare_tgos(
        state,
        tmp_path / "work",
        tmp_path / "exchange",
        limit=1,
    )
    batch_id = list(rows(prepared / "tgos_batches.parquet"))[0]["batch_id"]
    submitted = transition_batch(prepared, tmp_path / "work", batch_id, "submitted")
    result = tmp_path / "result.csv"
    response(
        result,
        [{"Address": address, "Response_Address": address, "Response_X": "121.51", "Response_Y": "25.01"}],
    )
    imported = import_tgos(submitted, tmp_path / "work", batch_id, result)
    output, report = backfill_output(
        converted,
        imported,
        previous,
        tmp_path / "output",
        notices=NOTICE,
        run_id="after-tgos",
    )
    assert report["changed_months"] == [202601]
    assert report["unchanged_month_artifacts"] == 9
    assert verify_output(previous)["snapshot_id"] == "before-tgos"
    assert verify_output(output)["tgos_started"] is True
