"""P3 TGOS quota, exchange, strict import, and backfill acceptance."""

import csv
from datetime import timedelta
import json

import pytest

from lvr_pipeline.backfill import backfill_output
from lvr_pipeline.packaging import package_output, verify_output
from lvr_pipeline.parquet_io import rows
from lvr_pipeline.tgos import (
    _round_robin,
    _service_today,
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
    day = _service_today().isoformat()
    prepared, exchange = prepare_tgos(
        state,
        tmp_path / "work",
        tmp_path / "exchange",
        service_date=day,
        external_used=9_999,
    )
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
    assert batch["status"] == "prepared" and batch["quota_consumed"] is True
    with pytest.raises(ValueError, match="latest snapshot"):
        prepare_tgos(
            state,
            tmp_path / "work",
            tmp_path / "exchange",
            service_date=day,
            external_used=0,
        )


def test_prepared_exchange_can_be_repaired_without_new_reservation(tmp_path):
    _, state, address = fixture_state(tmp_path)
    prepared, exchange = prepare_tgos(
        state,
        tmp_path / "work",
        tmp_path / "exchange",
        service_date=_service_today().isoformat(),
        external_used=0,
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


def test_future_dates_and_exhausted_shared_quota_fail(tmp_path):
    _, state, _ = fixture_state(tmp_path)
    with pytest.raises(ValueError, match="Future"):
        prepare_tgos(
            state,
            tmp_path / "work",
            tmp_path / "exchange",
            service_date=(_service_today() + timedelta(days=1)).isoformat(),
            external_used=0,
        )
    with pytest.raises(ValueError, match="No TGOS quota"):
        prepare_tgos(
            state,
            tmp_path / "work",
            tmp_path / "exchange",
            service_date=_service_today().isoformat(),
            external_used=10_000,
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
        service_date=_service_today().isoformat(),
        external_used=0,
        limit=1,
    )
    batch_id = list(rows(prepared / "tgos_batches.parquet"))[0]["batch_id"]
    cancelled = transition_batch(
        prepared, tmp_path / "work", batch_id, "cancelled", reason="not uploaded"
    )
    batch = list(rows(cancelled / "tgos_batches.parquet"))[0]
    query = list(rows(cancelled / "tgos_queries.parquet"))[0]
    assert batch["quota_consumed"] is False and query["status"] == "cancelled"
    with pytest.raises(ValueError, match="No eligible"):
        prepare_tgos(
            cancelled,
            tmp_path / "work",
            tmp_path / "exchange",
            service_date=_service_today().isoformat(),
            external_used=0,
        )

    fingerprint = query["query_fingerprint"]
    retried, _ = prepare_tgos(
        cancelled,
        tmp_path / "work",
        tmp_path / "exchange",
        service_date=_service_today().isoformat(),
        external_used=0,
        retry_fingerprints=[fingerprint],
        retry_reason="operator approved corrected retry",
    )
    batches = list(rows(retried / "tgos_batches.parquet"))
    assert batches[-1]["predecessor_batch_id"] == batch_id
    assert batches[-1]["reason"] == "operator approved corrected retry"


def test_external_shared_quota_cannot_decrease(tmp_path):
    _, state, _ = fixture_state(tmp_path)
    prepared, _ = prepare_tgos(
        state,
        tmp_path / "work",
        tmp_path / "exchange",
        service_date=_service_today().isoformat(),
        external_used=5,
        limit=1,
    )
    with pytest.raises(ValueError, match="cannot decrease"):
        prepare_tgos(
            prepared,
            tmp_path / "work",
            tmp_path / "exchange",
            service_date=_service_today().isoformat(),
            external_used=4,
        )


def test_strict_import_is_idempotent_and_rejects_axis_guessing(tmp_path):
    _, state, address = fixture_state(tmp_path)
    prepared, _ = prepare_tgos(
        state,
        tmp_path / "work",
        tmp_path / "exchange",
        service_date=_service_today().isoformat(),
        external_used=0,
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
        service_date=_service_today().isoformat(),
        external_used=0,
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


def test_alias_cycle_is_rejected_and_verified_alias_can_be_revoked(tmp_path):
    _, state, addresses = fixture_two_unmatched(tmp_path)
    day = _service_today().isoformat()
    prepared, _ = prepare_tgos(
        state,
        tmp_path / "work",
        tmp_path / "exchange",
        service_date=day,
        external_used=0,
        limit=1,
    )
    first_query = list(rows(prepared / "tgos_queries.parquet"))[0]
    first_response_address = next(
        address for address in addresses if address != first_query["address"]
    )
    batch_id = first_query["batch_id"]
    submitted = transition_batch(prepared, tmp_path / "work", batch_id, "submitted")
    result = tmp_path / "first.csv"
    response(
        result,
        [{"Address": first_query["address"], "Response_Address": first_response_address, "Response_X": "121.51", "Response_Y": "25.01"}],
    )
    imported = import_tgos(submitted, tmp_path / "work", batch_id, result)
    aliases = list(rows(imported / "verified_aliases.parquet"))
    assert len(aliases) == 1

    prepared, _ = prepare_tgos(
        imported,
        tmp_path / "work",
        tmp_path / "exchange",
        service_date=day,
        external_used=0,
        limit=1,
    )
    second_query = list(rows(prepared / "tgos_queries.parquet"))[-1]
    batch_id = second_query["batch_id"]
    submitted = transition_batch(prepared, tmp_path / "work", batch_id, "submitted")
    result = tmp_path / "second.csv"
    response(
        result,
        [{"Address": second_query["address"], "Response_Address": first_query["address"], "Response_X": "121.52", "Response_Y": "25.02"}],
    )
    imported = import_tgos(submitted, tmp_path / "work", batch_id, result)
    imports = list(rows(imported / "tgos_imports.parquet"))
    assert imports[-1]["status"] == "rejected"
    assert imports[-1]["reason"] == "TGOS alias would create a cycle"

    revoked = revoke_alias(
        imported,
        tmp_path / "work",
        aliases[0]["alias_key"],
        reason="operator rejected equivalence",
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
        service_date=_service_today().isoformat(),
        external_used=0,
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
