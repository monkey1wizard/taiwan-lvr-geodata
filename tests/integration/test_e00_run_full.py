"""E-00 (b15 style): run-full with a TGOS ledger whose district-fill answer fills an address of the pool.

Synthetic inputs only. The ledger comes from a first run without a ledger; its answer for the district_missing
address is read again by the address-pool stage of the second run, located by the tgos-state stage and listed
by the final-state stage with its TGOS result."""
import json
import struct
import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "acceptance"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lvr_pipeline.addresses import admin  # noqa: E402
from lvr_pipeline.addresses.identity import building_key  # noqa: E402
from lvr_pipeline.pipeline import run_full  # noqa: E402
from lvr_pipeline.storage.parquet import rows  # noqa: E402
from lvr_pipeline.tgos import import_tgos, prepare_tgos, transition_batch  # noqa: E402
from lvr_pipeline.transactions.inventory import inventory_json, write_raw_manifest  # noqa: E402
from test_d_run_full import LOG_DATE, output_record, snapshot, sources_of  # noqa: E402
from test_p1_conversion import ADDRESS, make_source, rules, verified_source  # noqa: E402
from test_p2_offline import source, use_synthetic_admin_towns  # noqa: E402
from test_p3_tgos import response  # noqa: E402

PLAIN = "臺北市測試路99號"
FILLED = "臺北市中正區測試路99號"
DATES = {ADDRESS: "1130201", PLAIN: "1150110"}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    committed = admin.ADMIN_TOWNS_PATH
    tmp = tmp_path_factory.mktemp("e00_run_full")
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
        none = run_full(run_id="none", **inputs)
        assert none["completed"] is True, none["failures"]
        none_dir = inputs["runs_root"] / "none"
        offline, index = snapshot(none_dir, "offline-state"), snapshot(none_dir, "offline-index")
        work, exchange = inputs["runs_root"] / "tgos-1", tmp / "tgos"
        prepared, folder = prepare_tgos(offline, work, exchange, index=index, exchange_date=LOG_DATE)
        queries = {r["address"]: r for r in rows(prepared / "tgos_queries.parquet")}
        assert [q["query_kind"] for q in queries.values()] == ["district_missing"]
        batch_id = next(r["batch_id"] for r in rows(prepared / "tgos_batches.parquet"))
        submitted = transition_batch(prepared, work, batch_id, "submitted")
        finish = folder / "Address_Finish.csv"
        (query,) = queries
        response(finish, [{"Address": query, "Response_Address": FILLED, "Response_X": "121.53", "Response_Y": "25.03"}])
        ledger = import_tgos(submitted, work, batch_id, finish, index=index)
        yield {"inputs": inputs, "none_dir": none_dir, "ledger": ledger, "exchange": exchange}
    finally:
        admin.ADMIN_TOWNS_PATH = committed
        admin.load_admin_towns.cache_clear()


def test_run_full_with_ledger_locates_the_filled_door_through_every_stage(world):
    inputs = {**world["inputs"], "tgos_ledger": world["ledger"], "exchange_dir": world["exchange"]}
    result = run_full(run_id="ledger", **inputs)
    assert result["completed"] is True, result["failures"]
    run_dir = world["inputs"]["runs_root"] / "ledger"
    key = building_key(FILLED)
    pool_report = json.loads((snapshot(run_dir, "address-pool") / "quality.json").read_text(encoding="utf-8"))
    fill = pool_report["tgos_district_fill"]
    assert (fill["adopted"], fill["adopted_fills"], fill["filled_occurrences"]) == (1, 1, 3)
    # Offline state: not located (no offline evidence); TGOS state: the answered coordinate.
    offline = {r["building_key"]: r for r in rows(snapshot(run_dir, "offline-state") / "address_index.parquet")}
    assert offline[key]["status"] == "unmatched"
    tgos = {r["building_key"]: r for r in rows(snapshot(run_dir, "tgos-state") / "address_index.parquet")}
    assert (tgos[key]["status"], tgos[key]["lng"], tgos[key]["lat"]) == ("located", 121.53, 25.03)
    # Final state: three observations (one per category) listed with their TGOS result.
    sources = sources_of(run_dir)
    assert len(sources) == 3 and {r["source"] for r in sources} == {"tgos"} and {r["building_key"] for r in sources} == {key}
    assert all(r["tgos_result_id"] for r in sources) and {(r["after_lng"], r["after_lat"]) for r in sources} == {(121.53, 25.03)}
    assert {r["after_address"] for r in sources} == {FILLED}
    # Output: the door of the transaction written without a town carries the TGOS coordinate.
    record = output_record(run_dir)
    parquet = run_dir / record["release_dir"] / "taiwanlvr.parquet"
    with duckdb.connect() as db:
        doors = db.execute(
            f"SELECT address, geometry FROM read_parquet('{parquet.as_posix()}') WHERE transaction_addresses = ?",
            [PLAIN]).fetchall()
    assert len(doors) == 3 and {address for address, _ in doors} == {FILLED}
    # WKB point: byte order, type, then longitude and latitude as doubles.
    assert {struct.unpack("<2d", bytes(wkb)[5:21]) for _, wkb in doors} == {(121.53, 25.03)}
    # Without the ledger the same door has no coordinate.
    none_sources = sources_of(world["none_dir"])
    assert none_sources == []
