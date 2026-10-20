"""B-4 (b12, b13, b15): run-full with a carried TGOS ledger on synthetic inputs. Nothing here reads real data."""
import csv
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))

from lvr_pipeline.addresses import admin  # noqa: E402
from lvr_pipeline.addresses.identity import building_key  # noqa: E402
from lvr_pipeline.pipeline import RUN_FULL_STAGES, run_full  # noqa: E402
from lvr_pipeline.storage.parquet import rows  # noqa: E402
from lvr_pipeline.storage.runs import digest, sha256_file  # noqa: E402
from lvr_pipeline.tgos import (  # noqa: E402
    _write_state,
    import_tgos,
    plan_tgos_state,
    prepare_tgos,
    transition_batch,
)
from lvr_pipeline.tgos.exchange import exchange_folder_name  # noqa: E402
from lvr_pipeline.tgos.validate import KEYLESS_REASON  # noqa: E402
from lvr_pipeline.transactions.inventory import inventory_json, write_raw_manifest  # noqa: E402
from test_p1_conversion import ADDRESS, make_source, rules, verified_source  # noqa: E402
from test_p2_offline import source, use_synthetic_admin_towns  # noqa: E402
from test_p3_tgos import response  # noqa: E402
from test_run_full import checkpoints  # noqa: E402

ROAD = "臺北市中正區測試路"
ADOPT, REJECT, MISSING = ROAD + "21號", ROAD + "22號", ROAD + "23號"
UNKEYED = ROAD + "7鄰鄰21號"  # county and town parse; the door text gives no key under the current rules
LOG_DATE = "2026-10-18"


def setup_doors(tmp_path):
    """One batch whose three categories hold one located door and three doors the address source does not know."""
    assert building_key(UNKEYED) is None
    raw, _ = make_source(tmp_path, [{"address": a} for a in [ADDRESS, ADOPT, REJECT, MISSING]], batch="115q1")
    manifest = tmp_path / "raw_manifest.json"
    manifest.write_text(inventory_json(verified_source(write_raw_manifest(raw, manifest))) + "\n", encoding="utf-8")
    address_dir, descriptor = source(tmp_path)
    use_synthetic_admin_towns(tmp_path, address_dir, descriptor)
    descriptor_path = tmp_path / "address_source.json"
    descriptor_path.write_text(json.dumps(verified_source(descriptor), ensure_ascii=False), encoding="utf-8")
    return {"raw_dir": raw, "manifest_path": manifest, "address_dir": address_dir,
            "address_source": descriptor_path, "rules_path": rules(tmp_path),
            "runs_root": tmp_path / "runs", "cutoff": 202610, "batch_rows": 2}


def snapshot(run_dir, stage):
    return run_dir / stage / "snapshots" / checkpoints(run_dir)[stage]["snapshot_id"]


def output_by_address(run_dir):
    record = json.loads((snapshot(run_dir, "output") / "output.json").read_text(encoding="utf-8"))
    found = {}
    for relative in record["summary"]["files"]:
        if relative.endswith(".csv"):
            with (run_dir / record["public_dir"] / relative).open(encoding="utf-8", newline="") as stream:
                for row in csv.DictReader(stream):
                    found.setdefault(row["address"], []).append((row["longitude"], row["latitude"]))
    return found


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    # setup_doors points the town table at a synthetic one for the whole module; put the committed one back after.
    original = admin.ADMIN_TOWNS_PATH
    tmp_path = tmp_path_factory.mktemp("b4")
    inputs = setup_doors(tmp_path)
    base = run_full(run_id="base", **inputs)
    assert base["completed"] is True, base["failures"]
    base_dir = inputs["runs_root"] / "base"
    offline, index = snapshot(base_dir, "offline-state"), snapshot(base_dir, "offline-index")
    # prepare-tgos in its own run directory beside the run-full run (contract rule 3).
    work, exchange = inputs["runs_root"] / "tgos-1", tmp_path / "tgos"
    prepared, folder = prepare_tgos(offline, work, exchange, index=index, exchange_date=LOG_DATE)
    batches, queries = list(rows(prepared / "tgos_batches.parquet")), list(rows(prepared / "tgos_queries.parquet"))
    assert {q["address"] for q in queries} == {ADOPT, REJECT, MISSING}
    batch_id = batches[0]["batch_id"]
    # A query sent under an older rule set: its text has no key now (contract rule 5).
    sent = queries[0]
    old_key = sent["building_key"].replace('"door":"', '"door":"7鄰鄰')
    queries.append({**sent, "ordinal": 4, "address": UNKEYED, "building_key": old_key,
                    "query_fingerprint": digest([old_key, UNKEYED])})
    batches[0]["address_count"] = 4
    widened = _write_state(prepared, work, action="old-rules-query", batches=batches, queries=queries,
                           results=[], aliases=[], alias_events=[])
    submitted = transition_batch(widened, work, batch_id, "submitted")
    assert folder.name == exchange_folder_name(batch_id, LOG_DATE)
    finish = folder / "Address_Finish.csv"
    response(finish, [
        {"Address": ADOPT, "Response_Address": ADOPT, "Response_X": "121.51", "Response_Y": "25.01"},
        {"Address": REJECT, "Response_Address": ROAD + "99號", "Response_X": "121.52", "Response_Y": "25.02"},
        {"Address": MISSING, "Response_Address": "", "Response_X": "", "Response_Y": ""},
        {"Address": UNKEYED, "Response_Address": ROAD + "25號", "Response_X": "121.53", "Response_Y": "25.03"},
    ])
    ledger = import_tgos(submitted, work, batch_id, finish, index=index)
    yield {"tmp": tmp_path, "inputs": inputs, "offline": offline, "index": index, "ledger": ledger,
           "exchange": exchange, "finish": finish, "batch_id": batch_id, "base_dir": base_dir}
    admin.ADMIN_TOWNS_PATH = original
    admin.load_admin_towns.cache_clear()


@pytest.fixture(scope="module")
def carried(world):
    result = run_full(run_id="withledger", tgos_ledger=world["ledger"], exchange_dir=world["exchange"],
                      **world["inputs"])
    assert result["completed"] is True, result["failures"]
    return result, world["inputs"]["runs_root"] / "withledger"


def test_ledger_run_adopts_reimported_responses_and_keeps_the_rejected_coordinate(world, carried):
    result, run_dir = carried
    steps = {s["step"]: s for s in result["steps"]}
    assert [s for s in steps if s in RUN_FULL_STAGES] == RUN_FULL_STAGES
    assert steps["tgos-state"]["reused_checkpoint"] is False and steps["tgos-ledger"]["snapshot_id"] == world["ledger"].name
    tgos = snapshot(run_dir, "tgos-state")
    report = json.loads((tgos / "quality.json").read_text(encoding="utf-8"))
    finish_sha = sha256_file(world["finish"])
    assert report["tgos_reimported_batches"] == [{"batch_id": world["batch_id"], "response_sha256": finish_sha}]
    assert report["tgos_ledger_manifest_sha256"] == sha256_file(world["ledger"] / "manifest.json")
    assert report["tgos_carried_keyless_queries"] == 1
    assert report["source_state"]["run_id"] == "withledger"
    manifest = json.loads((tgos / "manifest.json").read_text(encoding="utf-8"))
    # The binding lists the offline state, the ledger manifest and every re-imported response (contract rule 4).
    assert manifest["bindings"]["input_sha256"] == [
        sha256_file(snapshot(run_dir, "offline-state") / "manifest.json"), report["tgos_ledger_manifest_sha256"], finish_sha]
    results = {r["address"]: r for r in rows(tgos / "tgos_imports.parquet")}
    assert (results[ADOPT]["status"], results[ADOPT]["lng"], results[ADOPT]["lat"]) == ("succeeded", 121.51, 25.01)
    rejected = results[REJECT]
    assert rejected["status"] == "rejected" and rejected["lng"] is None and rejected["lat"] is None
    assert (rejected["response_lng"], rejected["response_lat"]) == (121.52, 25.02)
    assert results[MISSING]["status"] == "failed" and results[MISSING]["response_lng"] is None
    # Option A: the keyless query is kept, counted, not adopted; its returned pair stays as evidence.
    keyless = results[UNKEYED]
    assert (keyless["status"], keyless["reason"], keyless["lng"]) == ("rejected", KEYLESS_REASON, None)
    assert (keyless["response_lng"], keyless["response_lat"]) == (121.53, 25.03)
    queries = {q["address"]: q for q in rows(tgos / "tgos_queries.parquet")}
    assert queries[UNKEYED]["query_kind"] == "keyless" and queries[UNKEYED]["building_key"] is None
    assert all(q["status"] != "submitted" for q in queries.values())
    located = {r["canonical_address"]: r for r in rows(tgos / "address_index.parquet")}
    assert (located[ADOPT]["status"], located[ADOPT]["lng"], located[ADOPT]["lat"]) == ("located", 121.51, 25.01)
    assert located[REJECT]["status"] == "unmatched" and located[MISSING]["status"] == "unmatched"
    report_file = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
    assert report_file["address_status_counts"]["located"] == 2
    assert report_file["tgos"]["tgos_carried_keyless_queries"] == 1
    assert "tgos-state" in report_file["stages"]
    review = json.loads((snapshot(run_dir, "review") / "quality.json").read_text(encoding="utf-8"))
    assert review["address_status_source"] == "tgos-state"


def test_adopted_door_is_in_the_output_with_the_tgos_coordinate(world, carried):
    _, run_dir = carried
    after = output_by_address(run_dir)
    before = output_by_address(world["base_dir"])
    assert set(after[ADOPT]) == {("121.51", "25.01")} and len(after[ADOPT]) == 3  # one row per category
    # The output holds located doors only: the adopted door is new, the rejected and not-found ones are not there.
    assert ADOPT not in before and REJECT not in after and MISSING not in after and UNKEYED not in after
    assert set(after[ADDRESS]) == set(before[ADDRESS])


def test_rerun_reuses_the_tgos_state_but_not_with_another_ledger(world, carried):
    _, run_dir = carried
    again = run_full(run_id="withledger", tgos_ledger=world["ledger"], exchange_dir=world["exchange"], **world["inputs"])
    assert again["completed"] is True
    assert {s["step"]: s["reused_checkpoint"] for s in again["steps"] if s["step"] in RUN_FULL_STAGES} == {
        name: True for name in RUN_FULL_STAGES}
    before = checkpoints(run_dir)
    other = run_full(run_id="withledger", exchange_dir=world["exchange"], **world["inputs"])
    assert other["completed"] is False
    # E-00: the address-pool stage also reads the ledger (district fills), so it is the first to refuse another one.
    assert [(f["step"], "another TGOS ledger" in f["detail"]) for f in other["failures"]] == [("address-pool", True)]
    assert checkpoints(run_dir) == before


def test_tampered_response_file_is_refused(world, tmp_path):
    exchange = tmp_path / "tgos"
    shutil.copytree(world["exchange"], exchange)
    finish = next(exchange.glob("*/Address_Finish.csv"))
    data = bytearray(finish.read_bytes())
    data[data.index(b"121.51") + 4] ^= 1  # one byte of the adopted coordinate
    finish.write_bytes(bytes(data))
    with pytest.raises(ValueError, match="ledger recorded"):
        plan_tgos_state(world["offline"], world["ledger"], exchange, world["inputs"]["runs_root"])
    result = run_full(run_id="tampered", tgos_ledger=world["ledger"], exchange_dir=exchange, **world["inputs"])
    assert result["completed"] is False
    [failure] = result["failures"]
    # Refused before any stage is built.
    assert failure["step"] == "tgos-ledger" and "ledger recorded" in failure["detail"]
    assert checkpoints(world["inputs"]["runs_root"] / "tampered") == {}


def test_missing_response_file_is_refused(world, tmp_path):
    with pytest.raises(ValueError, match="exchange folder"):
        plan_tgos_state(world["offline"], world["ledger"], tmp_path / "empty", world["inputs"]["runs_root"])


def test_ledger_of_another_epoch_is_refused_before_any_stage_runs(world, tmp_path):
    copy = tmp_path / "ledger" / "tgos-state"
    shutil.copytree(world["ledger"].parent.parent, copy)
    snapshot_dir = copy / "snapshots" / world["ledger"].name
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["bindings"]["rebuild_epoch"] = "2026-01-01"
    (snapshot_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = run_full(run_id="epoch", tgos_ledger=snapshot_dir, exchange_dir=world["exchange"], **world["inputs"])
    assert result["completed"] is False
    [failure] = result["failures"]
    assert failure["step"] == "tgos-ledger" and "predates" in failure["detail"]
    assert checkpoints(world["inputs"]["runs_root"] / "epoch") == {}
