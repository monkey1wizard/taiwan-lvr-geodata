"""c33: approved TGOS retries from a list file (c32 option A) and the read-only retry list script. Synthetic data only."""

import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lvr_pipeline.addresses.identity import building_key  # noqa: E402
from lvr_pipeline.storage.parquet import rows  # noqa: E402
from lvr_pipeline.storage.runs import SnapshotStore, digest  # noqa: E402
from lvr_pipeline.tgos import _write_state, load_state, prepare_tgos  # noqa: E402
from lvr_pipeline.tgos.batches import read_retry_file  # noqa: E402
from test_d3_edge_cases import batch_of, send, world  # noqa: E402
from test_p2_offline import index_of  # noqa: E402
from test_p3_tgos import retry_csv  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "tgos_retry_list.py"
T = "臺北市中正區測試路"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_csv(path, header, values):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(values)
    return path


@pytest.mark.parametrize(
    ("header", "values", "message"),
    [
        (["fingerprint"], [["a"]], "no query_fingerprint column"),
        (["query_fingerprint"], [], "lists no fingerprint"),
        (["query_fingerprint"], [[""]], "empty one"),
        (["query_fingerprint"], [["a"], ["a"]], "repeats a fingerprint"),
    ],
)
def test_retry_file_is_refused_when_malformed(tmp_path, header, values, message):
    with pytest.raises(ValueError, match=message):
        read_retry_file(write_csv(tmp_path / "retry.csv", header, values))


def test_retry_file_reads_fingerprints_and_ignores_other_columns(tmp_path):
    path = write_csv(tmp_path / "retry.csv", ["review_key", "query_fingerprint", "text_changed"],
                     [["k1", "f1", "no"], ["k2", "f2", "yes"]])
    assert read_retry_file(path) == (["f1", "f2"], sha256(path))


def test_retry_from_a_file_records_its_hash_in_the_batch(tmp_path):
    address = T + "31號"
    w = world(tmp_path, sales=[{"address": address, "date": "1150102"}])
    first = send(w["prepared"], w["work"], w["tmp"], batch_of(w["prepared"]), {}, name="batch1")
    query = next(q for q in rows(first / "tgos_queries.parquet") if q["address"] == address)
    retry = retry_csv(w["tmp"], [query["query_fingerprint"]])
    second, _ = prepare_tgos(first, w["work"], w["tmp"] / "exchange-retry", retry_file=retry,
                             retry_reason="owner approved not found", index=index_of(w["state"]))
    batch = list(rows(second / "tgos_batches.parquet"))[-1]
    assert batch["reason"] == f"owner approved not found (retry file sha256 {sha256(retry)})"
    assert batch["predecessor_batch_id"] == query["batch_id"]
    report = load_state(second)[1]
    assert report["tgos_retry_file_sha256"] == sha256(retry) and report["tgos_retry_count"] == 1
    assert [q["address"] for q in rows(second / "tgos_queries.parquet") if q["batch_id"] == batch["batch_id"]] == [address]


def test_retry_file_with_a_state_that_cannot_be_retried_refuses_the_whole_batch(tmp_path):
    found, missing = T + "36號", T + "37號"
    w = world(tmp_path, sales=[{"address": found, "date": "1150102"}, {"address": missing, "date": "1150103"}])
    first = send(w["prepared"], w["work"], w["tmp"], batch_of(w["prepared"]), {found: (found, "121.57", "25.07")})
    statuses = {q["address"]: q for q in rows(first / "tgos_queries.parquet")}
    assert statuses[found]["status"] == "succeeded" and statuses[missing]["status"] == "failed"
    current = SnapshotStore(w["work"] / "tgos-state").current()["snapshot_id"]
    retry = retry_csv(w["tmp"], [statuses[missing]["query_fingerprint"], statuses[found]["query_fingerprint"]])
    with pytest.raises(ValueError, match="not in a retryable state"):
        prepare_tgos(first, w["work"], w["tmp"] / "exchange-refused", retry_file=retry, retry_reason="x",
                     index=index_of(w["state"]))
    assert SnapshotStore(w["work"] / "tgos-state").current()["snapshot_id"] == current
    assert not (w["tmp"] / "exchange-refused").exists()


def test_retried_door_whose_text_changed_is_sent_with_the_current_text(tmp_path):
    """c32: the list names the fingerprint sent last time; the door goes out with the text of the current rules."""
    address, old = T + "35號", "台北市中正區測試路35號"
    key = building_key(address)
    assert building_key(old) == key
    w = world(tmp_path, sales=[{"address": address, "date": "1150102"}])
    batch = batch_of(w["prepared"])
    queries = list(rows(w["prepared"] / "tgos_queries.parquet"))
    [query] = [q for q in queries if q["batch_id"] == batch]
    query.update(address=old, query_fingerprint=digest([key, old]))
    sent_old = _write_state(w["prepared"], w["work"], action="old-text", batches=list(rows(w["prepared"] / "tgos_batches.parquet")),
                            queries=queries, results=[], aliases=[], alias_events=[])
    first = send(sent_old, w["work"], w["tmp"], batch, {}, name="old")
    with pytest.raises(ValueError, match="No eligible TGOS candidates"):
        prepare_tgos(first, w["work"], w["tmp"] / "exchange-none", index=index_of(w["state"]))
    second, _ = prepare_tgos(first, w["work"], w["tmp"] / "exchange-retry", retry_reason="owner approved",
                             retry_file=retry_csv(w["tmp"], [digest([key, old])]), index=index_of(w["state"]))
    batch_row = list(rows(second / "tgos_batches.parquet"))[-1]
    [new] = [q for q in rows(second / "tgos_queries.parquet") if q["batch_id"] == batch_row["batch_id"]]
    assert new["address"] == address and new["query_fingerprint"] == digest([key, address])
    assert batch_row["predecessor_batch_id"] == batch


def _review_area(tmp_path):
    """A review snapshot and a TGOS state with only the columns the script reads."""
    k = {n: building_key(f"{T}{n}號") for n in range(1, 7)}
    queries = [
        {"query_fingerprint": "f1", "query_kind": "building_key", "building_key": k[1], "address": f"{T}1號", "status": "failed"},
        {"query_fingerprint": "f2", "query_kind": "building_key", "building_key": k[2], "address": "台北市中正區測試路2號", "status": "failed"},
        {"query_fingerprint": "f3", "query_kind": "building_key", "building_key": k[3], "address": f"{T}3號", "status": "failed"},
        {"query_fingerprint": "f4", "query_kind": "building_key", "building_key": k[4], "address": f"{T}4號", "status": "failed"},
        {"query_fingerprint": "f5", "query_kind": "building_key", "building_key": k[5], "address": f"{T}5號", "status": "failed"},
        {"query_fingerprint": "f5b", "query_kind": "building_key", "building_key": k[5], "address": f"{T}5號 ", "status": "submission_unknown"},
        {"query_fingerprint": "f6", "query_kind": "building_key", "building_key": k[6], "address": f"{T}6號", "status": "succeeded"},
    ]
    review = [
        (1, "pending", "tgos_not_found"), (2, "deferred", "tgos_not_found"), (3, "rejected", "tgos_not_found"),
        (4, "pending", "tgos_isolated"), (5, "pending", "tgos_not_found"), (6, "pending", "tgos_not_found"),
    ]
    review_rows = [{"review_key": k[n], "key_kind": "building_key", "building_key": k[n], "query_fingerprint": f"f{n}",
                    "county_code": "63", "canonical_address": f"{T}{n}號", "entry_reason": reason, "status": status}
                   for n, status, reason in review]
    for name, values, file in [("review", review_rows, "second_review.parquet"), ("state", queries, "tgos_queries.parquet")]:
        (tmp_path / name).mkdir()
        pq.write_table(pa.Table.from_pylist(values), tmp_path / name / file)
        (tmp_path / name / "manifest.json").write_text("{}", encoding="utf-8")
    return tmp_path / "review", tmp_path / "state"


def test_retry_list_leaves_out_rejected_and_waiting_keys(tmp_path):
    review, state = _review_area(tmp_path)
    out = tmp_path / "list.csv"
    proc = subprocess.run([sys.executable, str(SCRIPT), "--review", str(review), "--state", str(state), "--out", str(out)],
                          capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    listed = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [(r["query_fingerprint"], r["review_status"], r["text_changed"]) for r in listed] == [
        ("f1", "pending", "no"), ("f2", "deferred", "yes")]
    assert listed[1]["sent_text"] == "台北市中正區測試路2號" and listed[1]["current_text"] == f"{T}2號"
    # 3: a rejected decision; 5: a submission_unknown query of the same key; 6: a succeeded query.
    assert report["counts"] == {"left_out_rejected": 1, "left_out_waiting": 1, "left_out_not_retryable": 1,
                                "text_changed": 1, "text_same": 1}
    assert report["listed"] == 2 and report["out_sha256"] == sha256(out)
    assert read_retry_file(out) == (["f1", "f2"], sha256(out))
