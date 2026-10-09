"""R09-0: rebuild the TGOS state under the current address rules (owner decisions 2026-10-09, option A).

1. A carried ledger query with no key under the current rules is not carried into the
   TGOS state's queries; it is kept in the state report and listed in the review table.
2. prepare-tgos does not select keys of suspended counties.
"""

import json

import pytest

from lvr_pipeline.addresses.identity import building_key_v2
from lvr_pipeline.results.review import build_review
from lvr_pipeline.storage.parquet import rows, verify_relations
from lvr_pipeline.storage.runs import digest
from lvr_pipeline.tgos import (
    UNCARRIED,
    _artifact_paths,
    _write_state,
    import_tgos,
    load_state,
    prepare_tgos,
    transition_batch,
)
from test_p1_conversion import ADDRESS
from test_p2_offline import run
from test_p3_tgos import response
from test_r05_9_suspension import KINMEN_NONE, TAIPEI_TGOS, built  # noqa: F401  built is a fixture

UNKEYED = "臺北市中正區測試路7鄰鄰21號"  # county and town parse, the door text yields no key
OTHERS = ["臺北市中正區測試路21號", "臺北市中正區測試路22號", "臺北市中正區測試路23號"]


@pytest.fixture(scope="module")
def ledger(tmp_path_factory):
    """An old TGOS state whose submitted batch holds a query that has no key under the current rules."""
    assert building_key_v2(UNKEYED) is None
    tmp_path = tmp_path_factory.mktemp("r09-0")
    converted, _, descriptor, _, _, state = run(
        tmp_path,
        coordinates=[(ADDRESS, "63", "6300100", 121.5, 25.0)],
        records=[{"address": a, "date": "1150102"} for a in [ADDRESS, *OTHERS, UNKEYED]],
    )
    old = tmp_path / "old"
    prepared, _ = prepare_tgos(state, old, tmp_path / "exchange", limit=1)
    manifest, _, _ = load_state(prepared)
    paths = _artifact_paths(prepared, manifest)
    batches = list(rows(paths["tgos-batch"]))
    queries = list(rows(paths["tgos-query"]))
    sent = queries[0]
    # The key an older rule set stored for this text; it no longer exists.
    old_key = sent["building_key"].replace('"door":"', '"door":"7鄰鄰')
    queries.append({**sent, "ordinal": 2, "address": UNKEYED, "building_key": old_key,
                    "query_fingerprint": digest([old_key, UNKEYED])})
    batches[0]["address_count"] = 2
    widened = _write_state(prepared, old, action="old-rules-batch", batches=batches, queries=queries,
                           results=[], aliases=[], alias_events=[])
    submitted = transition_batch(widened, old, sent["batch_id"], "submitted")
    return {"tmp": tmp_path, "converted": converted, "descriptor": descriptor, "state": state,
            "ledger": submitted, "batch_id": sent["batch_id"], "sent": sent["address"], "old_key": old_key}


@pytest.fixture(scope="module")
def seeded(ledger):
    path, _ = prepare_tgos(ledger["state"], ledger["tmp"] / "new", ledger["tmp"] / "exchange",
                           ledger=ledger["ledger"], limit=1)
    return path


def test_unkeyed_query_is_kept_in_the_report_not_in_queries(ledger, seeded):
    before = list(rows(ledger["ledger"] / "tgos_queries.parquet"))
    queries = list(rows(seeded / "tgos_queries.parquet"))
    assert UNKEYED not in {q["address"] for q in queries}
    carried = [q for q in queries if q["batch_id"] == ledger["batch_id"]]
    assert [q["address"] for q in carried] == [ledger["sent"]]
    assert carried[0]["building_key"] == building_key_v2(ledger["sent"])
    _, report, _ = load_state(seeded)  # relations verified, including the batch count
    assert report[UNCARRIED] == [{
        "batch_id": ledger["batch_id"], "ordinal": 2,
        "query_fingerprint": digest([ledger["old_key"], UNKEYED]), "building_key": ledger["old_key"],
        "address": UNKEYED, "county_code": "63", "address_family": carried[0]["address_family"],
        "status": "submitted", "reason": "no_building_key_current_rules",
    }]
    assert report["tgos_uncarried_query_count"] == 1
    assert report["carried_query_keys_recomputed"] == 0
    # the old snapshot is not changed
    assert list(rows(ledger["ledger"] / "tgos_queries.parquet")) == before
    batch = next(b for b in rows(seeded / "tgos_batches.parquet") if b["batch_id"] == ledger["batch_id"])
    assert batch["address_count"] == 2
    # the new batch holds neither sent text
    new = [q["address"] for q in queries if q["batch_id"] != ledger["batch_id"]]
    assert new and not {UNKEYED, ledger["sent"]} & set(new)


def test_relations_check_the_uncarried_queries(seeded):
    manifest, report, _ = load_state(seeded)
    groups = {}
    for item in manifest["artifacts"]:
        if item["schema"] and item["path"].endswith(".parquet"):
            groups.setdefault(item["schema"], []).append(seeded / item["path"])
    listed = report[UNCARRIED]
    verify_relations(groups, uncarried_queries=listed)
    with pytest.raises(ValueError, match="TGOS batch count differs from queries"):
        verify_relations(groups, uncarried_queries=[])
    with pytest.raises(ValueError, match="TGOS batch Address is ambiguous"):
        verify_relations(groups, uncarried_queries=listed * 2)
    with pytest.raises(ValueError, match="TGOS query lacks batch"):
        verify_relations(groups, uncarried_queries=[{**listed[0], "batch_id": "tgos-unknown"}])
    with pytest.raises(ValueError, match="Invalid uncarried TGOS queries"):
        verify_relations(groups, uncarried_queries=[{**listed[0], "ordinal": "2"}])


def test_import_accepts_the_unkeyed_row_without_using_it(ledger, seeded):
    work = ledger["tmp"] / "new"
    answer = ledger["tmp"] / "answer.csv"
    response(answer, [
        {"Address": ledger["sent"], "Response_Address": ledger["sent"], "Response_X": "121.51", "Response_Y": "25.01"},
        {"Address": UNKEYED, "Response_Address": UNKEYED, "Response_X": "121.52", "Response_Y": "25.02"},
    ])
    missing = ledger["tmp"] / "missing.csv"
    response(missing, [
        {"Address": ledger["sent"], "Response_Address": ledger["sent"], "Response_X": "121.51", "Response_Y": "25.01"},
    ])
    with pytest.raises(ValueError, match="Address set differs"):
        import_tgos(seeded, work, ledger["batch_id"], missing)
    imported = import_tgos(seeded, work, ledger["batch_id"], answer)
    results = list(rows(imported / "tgos_imports.parquet"))
    assert [(r["address"], r["status"]) for r in results] == [(ledger["sent"], "succeeded")]
    evidence = [e for e in rows(imported / "address_observations.parquet") if e["source_kind"] == "tgos_result"]
    assert [e["normalized_address"] for e in evidence] == [ledger["sent"]]
    _, report, _ = load_state(imported)
    assert report["tgos_uncarried_response_rows"] == {ledger["batch_id"]: 1}
    assert len(report[UNCARRIED]) == 1
    assert report["status_counts"]["located"] == 2


def test_reseeding_from_the_new_state_keeps_the_list(ledger, seeded):
    again, _ = prepare_tgos(ledger["state"], ledger["tmp"] / "again", ledger["tmp"] / "exchange",
                            ledger=seeded, limit=1)
    _, first, _ = load_state(seeded)
    _, second, _ = load_state(again)
    assert second[UNCARRIED] == first[UNCARRIED]
    assert UNKEYED not in {q["address"] for q in rows(again / "tgos_queries.parquet")}


def test_review_lists_the_unkeyed_query(ledger, seeded):
    review = build_review(ledger["converted"], ledger["state"], ledger["tmp"] / "review",
                          descriptor=ledger["descriptor"], tgos_state=seeded, run_id="review-r09-0")
    values = list(rows(review / "exception_addresses.parquet"))
    picked = [r for r in values if r["reason_code"] == "tgos_query_unkeyed"]
    assert len(picked) == 3  # one transaction address per category
    assert all(r["raw_address"] == UNKEYED and r["building_key"] is None for r in picked)
    related = json.loads(picked[0]["related_json"])
    assert related["batch_id"] == ledger["batch_id"] and related["ordinal"] == 2
    assert related["ledger_building_key"] == ledger["old_key"] and related["status"] == "submitted"
    # the member's own reason stays
    assert any(r["reason_code"] == "address_review" and r["raw_address"] == UNKEYED for r in values)
    report = json.loads((review / "quality.json").read_text(encoding="utf-8"))
    assert report["reason_counts"]["tgos_query_unkeyed"] == 3
    assert report["tgos_uncarried_query_count"] == 1 and report["tgos_uncarried_queries_unlisted"] == 0


def test_review_without_tgos_state_has_no_unkeyed_rows(ledger):
    review = build_review(ledger["converted"], ledger["state"], ledger["tmp"] / "review-offline",
                          descriptor=ledger["descriptor"], run_id="review-offline")
    assert not [r for r in rows(review / "exception_addresses.parquet") if r["reason_code"] == "tgos_query_unkeyed"]


def test_ledger_without_unkeyed_queries_is_carried_as_before(ledger):
    """A ledger with only keyed queries gives an empty list and the same queries as before R09-0."""
    clean, _ = prepare_tgos(ledger["state"], ledger["tmp"] / "clean-old", ledger["tmp"] / "exchange", limit=1)
    clean = transition_batch(clean, ledger["tmp"] / "clean-old", list(rows(clean / "tgos_batches.parquet"))[0]["batch_id"],
                             "submitted")
    reseeded, _ = prepare_tgos(ledger["state"], ledger["tmp"] / "clean-new", ledger["tmp"] / "exchange",
                               ledger=clean, limit=1)
    _, report, _ = load_state(reseeded)
    assert report[UNCARRIED] == [] and report["tgos_uncarried_query_count"] == 0
    old = {(q["batch_id"], q["address"], q["building_key"]) for q in rows(clean / "tgos_queries.parquet")}
    new = {(q["batch_id"], q["address"], q["building_key"]) for q in rows(reseeded / "tgos_queries.parquet")}
    assert old < new


def test_suspended_county_keys_are_not_selected(built):  # noqa: F811
    tmp_path = built["tmp"]
    prepared, _ = prepare_tgos(built["state"], tmp_path / "r09-suspended", tmp_path / "r09-exchange", limit=10)
    assert {q["address"] for q in rows(prepared / "tgos_queries.parquet")} == {TAIPEI_TGOS}
    _, report, _ = load_state(prepared)
    assert report["tgos_suspended_candidates_excluded"] == {"09020": 1, "10016": 0}


def test_counties_outside_the_list_are_still_selected(built):  # noqa: F811
    tmp_path = built["tmp"]
    prepared, _ = prepare_tgos(built["plain"], tmp_path / "r09-plain", tmp_path / "r09-exchange", limit=10)
    assert {q["address"] for q in rows(prepared / "tgos_queries.parquet")} == {KINMEN_NONE, TAIPEI_TGOS}
    _, report, _ = load_state(prepared)
    assert report.get("tgos_suspended_candidates_excluded", {}) == {}


def test_approved_retry_of_a_suspended_key_is_refused(built):  # noqa: F811
    tmp_path = built["tmp"]
    old = tmp_path / "r09-retry-old"
    prepared, _ = prepare_tgos(built["plain"], old, tmp_path / "r09-retry-exchange", limit=10)
    kinmen = next(q for q in rows(prepared / "tgos_queries.parquet") if q["address"] == KINMEN_NONE)
    cancelled = transition_batch(prepared, old, kinmen["batch_id"], "cancelled", reason="x")
    with pytest.raises(ValueError, match="no longer an eligible candidate"):
        prepare_tgos(built["state"], tmp_path / "r09-retry-new", tmp_path / "r09-retry-exchange",
                     ledger=cancelled, retry_fingerprints=[kinmen["query_fingerprint"]], retry_reason="x")
