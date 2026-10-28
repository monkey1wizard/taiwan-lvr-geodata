"""b17: scripts/check_tgos_v12.py on a synthetic ledger and its merged snapshot. Nothing here reads real data."""

import importlib.util
import inspect
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lvr_pipeline.addresses.identity import building_key
from lvr_pipeline.storage.parquet import rows
from lvr_pipeline.storage.runs import digest
from lvr_pipeline.tgos import _write_state, import_tgos, merge_ledger, transition_batch
from test_b4_ledger import ADOPT, MISSING, REJECT, ROAD, _world
from test_p2_offline import index_of
from test_p3_tgos import response

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_tgos_v12.py"
UNKEYED = ROAD + "7鄰鄰21號"  # no building key under the current rules (see test_run_full_tgos_ledger)
DIFFERS = "TGOS response address differs from submitted address"
KEYLESS = "TGOS query has no building key under the current rules"
NO_REASON = "(none)"


@pytest.fixture(scope="module")
def merged(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("b17")
    _, work, prepared, batch_id = _world(tmp_path)
    queries = list(rows(prepared / "tgos_queries.parquet"))
    batches = list(rows(prepared / "tgos_batches.parquet"))
    # One query sent under an older rule set. The carry (batches._carry_ledger, contract rule 5) leaves it
    # as keyless: query_kind keyless, building_key null, and its old fingerprint.
    sent = queries[0]
    old_key = sent["building_key"].replace('"door":"', '"door":"7鄰鄰')
    queries.append({**sent, "ordinal": 4, "address": UNKEYED, "query_kind": "keyless", "building_key": None,
                    "query_fingerprint": digest([old_key, UNKEYED])})
    batches[0]["address_count"] = 4
    widened = _write_state(prepared, work, action="old-rules-query", batches=batches, queries=queries,
                           results=[], aliases=[], alias_events=[])
    submitted = transition_batch(widened, work, batch_id, "submitted")
    finish = tmp_path / "finish.csv"
    response(finish, [
        {"Address": ADOPT, "Response_Address": ADOPT, "Response_X": "121.51", "Response_Y": "25.01"},
        {"Address": REJECT, "Response_Address": ROAD + "99號", "Response_X": "121.52", "Response_Y": "25.02"},
        {"Address": MISSING, "Response_Address": "", "Response_X": "", "Response_Y": ""},
        {"Address": UNKEYED, "Response_Address": ROAD + "25號", "Response_X": "121.53", "Response_Y": "25.03"},
    ])
    ledger = import_tgos(submitted, work, batch_id, finish, index=index_of(submitted))
    return merge_ledger(ledger, work, tmp_path / "merged")


def load_script():
    spec = importlib.util.spec_from_file_location("check_tgos_v12", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_check(state, *extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--state", str(state), *extra],
        capture_output=True, text=True, encoding="utf-8",
    )


def test_merged_snapshot_passes_with_expected_counts(merged, tmp_path):
    out = tmp_path / "report.json"
    proc = run_check(merged, "--out", str(out))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(out.read_text(encoding="utf-8"))
    assert json.loads(proc.stdout) == report
    assert report["pass"] is True and report["snapshot_kind"] == "merged"
    [batch] = report["batches"]
    assert batch["counts_by_status"] == {"failed": 1, "rejected": 2, "succeeded": 1}
    # The adopted row and the not-found row carry no reason.
    assert batch["counts_by_reason"] == {NO_REASON: 2, DIFFERS: 1, KEYLESS: 1}
    assert batch["differs_fields"] == {"door": 1}
    assert batch["keyless_queries"] == 1
    assert all(check["pass"] for check in batch["checks"].values())
    assert batch["checks"]["7_address_index"]["located_matching"] == 1
    assert batch["checks"]["4_rejected_rows"]["base_check_candidates"] == {}
    assert report["checks"]["0_orphan_results"]["pass"] is True


def test_differs_row_on_a_base_route_is_a_candidate_not_a_failure(merged, tmp_path):
    # e15: a response that adds a road section needs the base group, which this script does not read.
    copy = tmp_path / "base_route"
    shutil.copytree(merged, copy)
    path = copy / "tgos_imports.parquet"
    table = pq.read_table(path)
    data = table.to_pylist()
    rejected = next(r for r in data if r["status"] == "rejected" and r["reason"] == DIFFERS)
    rejected["response_address"] = ROAD.replace("測試路", "測試路一段") + "22號"
    pq.write_table(pa.Table.from_pylist(data, schema=table.schema), path)
    out = tmp_path / "report.json"
    proc = run_check(copy, "--out", str(out))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    [batch] = json.loads(out.read_text(encoding="utf-8"))["batches"]
    check = batch["checks"]["4_rejected_rows"]
    assert check["pass"] is True and check["base_check_candidates"] == {"road_section": 1}


def test_stored_query_key_that_differs_from_current_rules_fails_check_8(merged, tmp_path):
    copy = tmp_path / "stale_key"
    shutil.copytree(merged, copy)
    path = copy / "tgos_queries.parquet"
    table = pq.read_table(path)
    data = table.to_pylist()
    query = next(q for q in data if q["query_kind"] == "building_key" and q["address"] == ADOPT)
    query["building_key"] = building_key(ROAD + "29號")
    pq.write_table(pa.Table.from_pylist(data, schema=table.schema), path)
    out = tmp_path / "report.json"
    proc = run_check(copy, "--out", str(out))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    [batch] = json.loads(out.read_text(encoding="utf-8"))["batches"]
    check = batch["checks"]["8_query_key_current"]
    assert check["pass"] is False and check["failure_counts"] == {"stored building_key differs from building_key(address)": 1}
    assert check["samples"][0]["address"] == ADOPT


def test_copied_rule_strings_match_their_source():
    from lvr_pipeline.results.review import TGOS_REJECTION_CODES
    from lvr_pipeline.tgos import validate as tgos_validate

    script = load_script()
    source = inspect.getsource(tgos_validate)
    # Named constants in tgos/validate.py: compare the values.
    assert script.DISTRICT_FILL_REASON == tgos_validate.DISTRICT_FILL_REASON
    assert script.KEYLESS_REASON == tgos_validate.KEYLESS_REASON
    # Literal strings in tgos/validate.py (no named constant): the text must appear in its source.
    assert script.DIFFERS_REASON in source
    assert script.OUT_OF_BOUNDS_REASON in source
    # The rejection reasons used by check 4 are keys of TGOS_REJECTION_CODES.
    assert script.DIFFERS_REASON in TGOS_REJECTION_CODES
    assert script.OUT_OF_BOUNDS_REASON in TGOS_REJECTION_CODES


def test_corrupted_copy_fails_and_names_the_check(merged, tmp_path):
    copy = tmp_path / "corrupted"
    shutil.copytree(merged, copy)
    path = copy / "tgos_imports.parquet"
    table = pq.read_table(path)
    data = table.to_pylist()
    rejected = next(r for r in data if r["status"] == "rejected" and r["reason"] == DIFFERS)
    rejected["status"] = "succeeded"
    pq.write_table(pa.Table.from_pylist(data, schema=table.schema), path)
    out = tmp_path / "report.json"
    proc = run_check(copy, "--out", str(out))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["pass"] is False
    [batch] = report["batches"]
    failing = sorted(name for name, check in batch["checks"].items() if not check["pass"])
    assert "3_succeeded_rows" in failing
    sample = batch["checks"]["3_succeeded_rows"]["samples"][0]
    assert sample["address"] == rejected["address"] and sample["status"] == "succeeded"
