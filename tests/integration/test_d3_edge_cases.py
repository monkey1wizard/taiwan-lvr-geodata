"""D-3 edge cases on synthetic data: cross-period adoption, door renumbering, alias revocation and late TGOS returns."""

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lvr_pipeline.addresses.identity import building_key  # noqa: E402
from lvr_pipeline.addresses.pool import build_pool  # noqa: E402
from lvr_pipeline.converted import export_converted  # noqa: E402
from lvr_pipeline.offline.index import build_index  # noqa: E402
from lvr_pipeline.results.final_state import build_final_state  # noqa: E402
from lvr_pipeline.results.reconcile import resolve_offline  # noqa: E402
from lvr_pipeline.results.review import build_review  # noqa: E402
from lvr_pipeline.storage.parquet import rows  # noqa: E402
from lvr_pipeline.storage.runs import SnapshotStore  # noqa: E402
from lvr_pipeline.tgos import (  # noqa: E402
    _artifact_paths,
    _write_state,
    import_tgos,
    load_state,
    merge_ledger,
    prepare_tgos,
    revoke_alias,
    transition_batch,
)
from lvr_pipeline.transactions.normalize import normalize  # noqa: E402
from lvr_pipeline.transactions.read import ingest  # noqa: E402
from test_final_state import affected_of, decisions_dir, fresh, manual_decision, sources_of  # noqa: E402
from test_p1_conversion import CODE, add_members, make_source, no_char_fixes, rules  # noqa: E402
from test_p2_offline import index_of, register_index, source as offline_source  # noqa: E402
from test_p3_tgos import response, retry_csv  # noqa: E402
from test_second_review import by_address, second  # noqa: E402

RENT_HEADER = ["土地位置建物門牌", "交易標的", "租賃年月日", "總額元", "建物總面積平方公尺", "編號", "new_field"]
MISMATCH = "TGOS response address differs from submitted address"


def world(tmp, sales, rent=(), *, limit=10000):
    """Converted data (sales members, plus a rent member when ``rent`` is given), offline state and a prepared batch.

    ``sales`` and ``rent`` are lists of record dicts with ``address`` and ``date`` (ROC yyymmdd).
    """
    raw, manifest = make_source(tmp, sales, categories=("sales",))
    if rent:
        manifest = add_members(tmp, {"a_lvr_land_c.csv": (RENT_HEADER, [
            [r["address"], "房地(土地+建物)", r["date"], "1234.56", "10.25", "same", "keep"] for r in rent])})
    work = tmp / "work"
    ingested = ingest(raw, manifest, ["115q1"], work, batch_rows=2, code_commit=CODE)
    normalized = normalize(ingested, work, cutoff=202610, rules_path=rules(tmp), char_fixes=no_char_fixes(),
                           batch_rows=2, code_commit=CODE)
    converted = export_converted(normalized, work, code_commit=CODE)
    root, descriptor = offline_source(tmp, None)
    index = register_index(build_index(root, descriptor, work, counties=["63"]))
    pool = build_pool(converted, root, descriptor, work, index_path=index)
    state = resolve_offline(pool, index, work)
    prepared, _ = prepare_tgos(state, work, tmp / "exchange-0", limit=limit, index=index_of(state))
    return {"tmp": tmp, "work": work, "converted": converted, "descriptor": descriptor,
            "index": index, "state": state, "prepared": prepared}


def batch_of(state):
    """The batch id of the prepared batch in a TGOS state."""
    return next(r["batch_id"] for r in rows(state / "tgos_batches.parquet") if r["status"] == "prepared")


def send(state, work, tmp, batch_id, answers, *, mode="submitted", name="response"):
    """Answer one batch as the TGOS exchange would and import it.

    ``answers`` maps an address to (response address, X, Y); an address without an answer gets an empty
    response. ``mode`` is the status the batch moves to before the import (submitted or submission_unknown).
    """
    submitted = transition_batch(state, work, batch_id, mode)
    path = tmp / f"{name}.csv"
    queries = [q for q in rows(state / "tgos_queries.parquet") if q["batch_id"] == batch_id]
    response(path, [{"Address": q["address"], "Response_Address": answers.get(q["address"], ("", "", ""))[0],
                     "Response_X": answers.get(q["address"], ("", "", ""))[1],
                     "Response_Y": answers.get(q["address"], ("", "", ""))[2]} for q in queries])
    return import_tgos(submitted, work, batch_id, path, index=index_of(submitted))


def merged(w, tgos):
    """B-4 (b14): an import snapshot holds the ledger only; review and final state read its merged state."""
    return None if tgos is None else merge_ledger(tgos, w["work"], w["tmp"] / "merged" / tgos.name)


def review(w, tgos=None, *, decisions=None):
    return build_review(w["converted"], w["state"], fresh(w, "review"), descriptor=w["descriptor"],
                        tgos_state=merged(w, tgos), index=w["index"], review_decisions=decisions)


def final(w, review_dir, tgos=None, *, baseline=None, work=None):
    return build_final_state(w["converted"], w["state"], work or fresh(w, "final"), review=review_dir,
                             tgos_state=merged(w, tgos), baseline=baseline)


def test_cross_year_and_category_adoption_lists_both_months(tmp_path):
    """One key with a 2015 sales door and a 2024 rent door, adopted by hand: both months and both CSVs are listed."""
    address, manual = "臺北市中正區測試路40號", "臺北市中正區測試路40號(人工判定)"
    w = world(tmp_path, sales=[{"address": address, "date": "1040315"}],
              rent=[{"address": address, "date": "1130110"}])
    tgos = send(w["prepared"], w["work"], w["tmp"], batch_of(w["prepared"]), {})
    decisions = decisions_dir(w, manual_decision(w, address, 121.5, 25.0, manual))
    rev = review(w, tgos, decisions=decisions)
    assert by_address(second(rev), address)["status"] == "adopted"
    out = final(w, rev, tgos)
    affected = affected_of(out)
    assert [(m["year"], m["month"], m["category"], m["added"], m["updated"], m["removed"]) for m in affected["months"]] == [
        (2015, 3, "sales", 1, 0, 0), (2024, 1, "rent", 1, 0, 0)]
    csv_paths = ["csv/2015/taiwanlvr_201503_sales.csv", "csv/2024/taiwanlvr_202401_rent.csv"]
    assert affected["files"] == csv_paths + ["taiwanlvr.parquet", "catalog.json"]
    assert affected["removed_total"] == 0
    doors = sources_of(out)
    assert sorted((r["period_year"], r["period_month"], r["category"]) for r in doors) == [
        (2015, 3, "sales"), (2024, 1, "rent")]
    assert {r["source"] for r in doors} == {"manual"}


def test_door_renumbering_is_isolated_and_never_linked(tmp_path):
    """Old door 20號 is not in the address source; TGOS answers door 22號 with a valid coordinate: nothing links them."""
    old, new = "臺北市中正區測試路20號", "臺北市中正區測試路22號"
    w = world(tmp_path, sales=[{"address": old, "date": "1150102"}])
    tgos = send(w["prepared"], w["work"], w["tmp"], batch_of(w["prepared"]), {old: (new, "121.56", "25.06")})
    imports = [r for r in rows(tgos / "tgos_imports.parquet") if r["address"] == old]
    assert [(r["status"], r["reason"]) for r in imports] == [("rejected", MISMATCH)]
    rev = review(w, tgos)
    entry = by_address(second(rev), old)
    assert entry["entry_reason"] == "tgos_isolated" and json.loads(entry["mismatch_fields"]) == ["door"]
    assert entry["response_address"] == new and entry["response_building_key"] == building_key(new)
    out = final(w, rev, tgos)
    located = {r["building_key"]: r for r in rows(out / "address_index.parquet")}
    assert located[building_key(old)]["status"] != "located" and located[building_key(old)]["lng"] is None
    assert building_key(new) not in located
    assert [r for r in sources_of(out) if r["building_key"] == building_key(old) or r["after_address"] == new] == []


def test_alias_revocation_leaves_final_state_unchanged(tmp_path):
    """A verified alias does not locate doors: revoking it leaves the address and final rows as they were."""
    a21, a22 = "臺北市中正區測試路21號", "臺北市中正區測試路22號"
    w = world(tmp_path, sales=[{"address": a21, "date": "1150102"}, {"address": a22, "date": "1150103"}])
    tgos = send(w["prepared"], w["work"], w["tmp"], batch_of(w["prepared"]), {a21: (a21, "121.51", "25.01")})
    manifest, _, _ = load_state(tgos)
    paths = _artifact_paths(tgos, manifest)
    alias_key, target_key = building_key(a22), building_key(a21)
    seeded = _write_state(
        tgos, w["work"], action="seed-alias", batches=list(rows(paths["tgos-batch"])),
        queries=list(rows(paths["tgos-query"])), results=list(rows(paths["tgos-result"])),
        aliases=[{"key_version": "v2", "alias_key": alias_key, "target_key": target_key, "evidence_ref": "manual"}],
        alias_events=[{"event_id": "seed", "key_version": "v2", "alias_key": alias_key, "target_key": target_key,
                       "action": "verified", "evidence_ref": "manual"}])
    first_rev = review(w, seeded)
    first = final(w, first_rev, seeded)
    revoked = revoke_alias(seeded, w["work"], alias_key, reason="operator rejected equivalence")
    assert list(rows(revoked / "verified_aliases.parquet")) == []
    assert [r["action"] for r in rows(revoked / "alias_events.parquet")] == ["verified", "revoked"]
    assert list(rows(merged(w, seeded) / "address_index.parquet")) == list(rows(merged(w, revoked) / "address_index.parquet"))
    second_final = final(w, review(w, revoked), revoked, baseline=first)
    assert list(rows(first / "address_index.parquet")) == list(rows(second_final / "address_index.parquet"))
    assert affected_of(second_final)["removed_total"] == 0


def test_removed_door_stops_with_month_detail_and_keeps_the_current_snapshot(tmp_path):
    """A baseline where a TGOS door is located and a new state where it is not: the stage stops, nothing is published."""
    a21, a22 = "臺北市中正區測試路21號", "臺北市中正區測試路22號"
    w = world(tmp_path, sales=[{"address": a21, "date": "1150102"}, {"address": a22, "date": "1150103"}])
    tgos = send(w["prepared"], w["work"], w["tmp"], batch_of(w["prepared"]), {a21: (a21, "121.51", "25.01")})
    baseline = final(w, review(w, tgos), tgos)
    offline_review = review(w, None)
    work = w["tmp"] / "guarded"
    build_final_state(w["converted"], w["state"], work, review=offline_review)
    current = SnapshotStore(work / "final-state").current()["snapshot_id"]
    with pytest.raises(ValueError, match="would disappear") as error:
        build_final_state(w["converted"], w["state"], work, review=offline_review, baseline=baseline)
    message = str(error.value)
    assert re.search(r"\d{6} \w+ \d+", message) and "202601 sales 1" in message
    assert SnapshotStore(work / "final-state").current()["snapshot_id"] == current


def test_not_found_then_approved_retry_locates_the_key(tmp_path):
    """A key answered 'not found' in batch 1 is locally located after an approved retry answers it in batch 2."""
    address = "臺北市中正區測試路31號"
    w = world(tmp_path, sales=[{"address": address, "date": "1150102"}])
    first = send(w["prepared"], w["work"], w["tmp"], batch_of(w["prepared"]), {}, name="batch1")
    assert by_address(second(review(w, first)), address)["entry_reason"] == "tgos_not_found"
    query = next(q for q in rows(first / "tgos_queries.parquet") if q["address"] == address)
    assert query["status"] == "failed"
    retry, _ = prepare_tgos(first, w["work"], w["tmp"] / "exchange-retry",
                            retry_file=retry_csv(w["tmp"], [query["query_fingerprint"]]),
                            retry_reason="operator approved retry", index=index_of(w["state"]))
    tgos = send(retry, w["work"], w["tmp"], batch_of(retry), {address: (address, "121.57", "25.07")}, name="batch2")
    assert not [r for r in second(review(w, tgos)) if r["canonical_address"] == address]
    out = final(w, review(w, tgos), tgos)
    located = {r["building_key"]: r for r in rows(out / "address_index.parquet")}
    assert located[building_key(address)]["status"] == "located"
    doors = [r for r in sources_of(out) if r["building_key"] == building_key(address)]
    assert doors and {r["source"] for r in doors} == {"tgos"}


def test_submission_unknown_batch_imports_later_and_leaves_the_waiting_state(tmp_path):
    """A batch marked submission_unknown is not in the review; its response imported later locates the key."""
    address = "臺北市中正區測試路32號"
    w = world(tmp_path, sales=[{"address": address, "date": "1150102"}])
    batch = batch_of(w["prepared"])
    waiting = transition_batch(w["prepared"], w["work"], batch, "submission_unknown")
    assert not [r for r in second(review(w, waiting)) if r["canonical_address"] == address]
    imported = send(waiting, w["work"], w["tmp"], batch, {address: (address, "121.58", "25.08")},
                    mode="submission_unknown", name="late")
    assert [r["status"] for r in rows(imported / "tgos_imports.parquet")] == ["succeeded"]
    assert [r["status"] for r in rows(imported / "tgos_batches.parquet")] == ["completed"]
    assert [r["status"] for r in rows(imported / "tgos_queries.parquet")] == ["succeeded"]
    assert not [r for r in second(review(w, imported)) if r["canonical_address"] == address]
    located = {r["building_key"]: r for r in rows(merged(w, imported) / "address_index.parquet")}
    assert located[building_key(address)]["status"] == "located"


def test_a_succeeded_key_never_gets_a_second_result(tmp_path):
    """Late return with a different coordinate: a located key cannot be queried again.

    The conflict branch of import_tgos for a later different coordinate needs a second result for a
    key that already succeeded. prepare_tgos never reselects a queried key, and an approved retry of a
    succeeded query is refused, so the ledger holds one result per key and the case cannot arise.
    """
    k1, k2 = "臺北市中正區測試路33號", "臺北市中正區測試路34號"
    w = world(tmp_path, sales=[{"address": k1, "date": "1150102"}, {"address": k2, "date": "1150103"}], limit=1)
    batch = batch_of(w["prepared"])
    query = next(q for q in rows(w["prepared"] / "tgos_queries.parquet") if q["batch_id"] == batch)
    first = send(w["prepared"], w["work"], w["tmp"], batch, {query["address"]: (query["address"], "121.57", "25.07")},
                 name="first")
    assert {r["building_key"]: r for r in rows(merged(w, first) / "address_index.parquet")}[query["building_key"]]["status"] == "located"
    with pytest.raises(ValueError, match="not in a retryable state"):
        prepare_tgos(first, w["work"], w["tmp"] / "exchange-retry", index=index_of(w["state"]),
                     retry_file=retry_csv(w["tmp"], [query["query_fingerprint"]]), retry_reason="late different coordinate")
    second_batch, _ = prepare_tgos(first, w["work"], w["tmp"] / "exchange-next", index=index_of(w["state"]))
    chosen = [q["building_key"] for q in rows(second_batch / "tgos_queries.parquet") if q["batch_id"] != batch]
    assert query["building_key"] not in chosen
