"""c55: a key located by a TGOS district fill counts as located for prepare-tgos and import-tgos. Synthetic data only.

The E-00 world answers two district_missing addresses; the fills locate their filled keys in the merged
tgos-state. Before c55, current_address_rows read only door-level results, so prepare-tgos selected the
filled keys again and an import did not see their coordinate.
"""

import shutil

import pytest

from lvr_pipeline.addresses.identity import building_key
from lvr_pipeline.results.reconcile import resolve_offline
from lvr_pipeline.storage.parquet import rows
from lvr_pipeline.tgos import _artifact_paths, import_tgos, load_state, prepare_tgos, transition_batch
from lvr_pipeline.tgos.batches import open_input
from lvr_pipeline.tgos.resolution import current_address_rows, evidence_from_results, ledger_evidence, rebuild
from lvr_pipeline.tgos.validate import build_tgos_state, plan_tgos_state
from test_e00_tgos_district_fill import FILLED_PLAIN, FILLED_VILLAGE, GOOD, OTHER, VILLAGE, adopted, make_ledger, world  # noqa: F401
from test_p3_tgos import response

FILLED = {building_key(FILLED_PLAIN), building_key(FILLED_VILLAGE)}


@pytest.fixture(scope="module")
def merged(world, adopted):  # noqa: F811
    """The offline state of the filled pool and its merged tgos-state, as run-full writes them."""
    work = adopted["work"]
    offline = resolve_offline(adopted["pool"], world["index"], work)
    plan = plan_tgos_state(offline, adopted["ledger"], adopted["exchange"], work.parent)
    return offline, build_tgos_state(offline, work, plan, index=world["index"])


def statuses(address_rows):
    return {r["building_key"]: r["status"] for r in address_rows if r["building_key"] in FILLED}


def test_current_rows_count_the_fill_and_door_results_alone_do_not(merged):
    offline, state = merged
    inp = open_input(state, state.parent.parent.parent, writing=False)
    batches, queries, results, _, _ = inp.ledger
    paths = _artifact_paths(inp.offline, inp.offline_manifest)
    assert statuses(rows(paths["address-result"])) == dict.fromkeys(FILLED, "unmatched")
    # Before c55: door-level results only, so both filled keys stay unmatched.
    door_only = rebuild(paths, inp.offline_report, list(rows(paths["offline-row"])) + evidence_from_results(results, queries))[0]
    assert statuses(door_only) == dict.fromkeys(FILLED, "unmatched")
    assert statuses(current_address_rows(paths, inp.offline_report, results, queries)) == dict.fromkeys(FILLED, "located")
    assert {r["building_key"] for r in ledger_evidence(paths, results, queries)} >= FILLED


def test_prepare_does_not_select_a_key_located_by_a_fill(world, merged):  # noqa: F811
    _, state = merged
    assert statuses(rows(state / "address_index.parquet")) == dict.fromkeys(FILLED, "located")
    with pytest.raises(ValueError, match="No eligible TGOS candidates"):
        prepare_tgos(state, world["tmp"] / "c55-work", world["tmp"] / "c55-exchange", index=world["index"])


def test_fill_whose_key_is_not_in_the_pool_adds_nothing(world):  # noqa: F811
    # Counter case: the pool was built without the ledger, so the key only the fill creates does not exist.
    ledger, _ = make_ledger(world, "c55-nopool", {VILLAGE: (FILLED_VILLAGE, OTHER)})
    inp = open_input(ledger, ledger.parent.parent.parent, writing=False)
    _, queries, results, _, _ = inp.ledger
    paths = _artifact_paths(inp.offline, inp.offline_manifest)
    assert building_key(FILLED_VILLAGE) not in {r["building_key"] for r in rows(paths["address-pool"])}
    assert any(r["status"] == "succeeded" for r in results)
    assert ledger_evidence(paths, results, queries) == evidence_from_results(results, queries)


def test_import_sees_the_fill_coordinate_and_marks_a_different_answer_conflict(world, adopted, merged):  # noqa: F811
    """A later door-level query of a filled key answered 1.4 km away is a conflict, not a second located point.

    The village key exists only through the fill (the plain one is also written with a floor and was sent in batch 1).
    """
    offline, _ = merged
    tmp = world["tmp"]
    # A fresh chain from the filled pool's offline state; the carried batch is reopened and its results dropped.
    seeded, _ = prepare_tgos(offline, tmp / "c55-chain", tmp / "c55-chain-exchange", index=world["index"],
                             ledger=adopted["ledger"], exchange_date="2026-10-29")
    first = world["batch_id"]
    second = next(b["batch_id"] for b in rows(seeded / "tgos_batches.parquet") if b["status"] == "prepared")
    asked = {q["building_key"]: q for q in rows(seeded / "tgos_queries.parquet") if q["batch_id"] == second}
    village_key = building_key(FILLED_VILLAGE)
    assert village_key in asked
    finish = adopted["exchange"] / world["folder"].relative_to(world["exchange"]) / "Address_Finish.csv"
    again = import_tgos(seeded, tmp / "c55-chain", first, finish, index=world["index"])
    village = asked[village_key]
    # The response goes into the exchange folder of the batch, so run-full can read it again below.
    exchange = tmp / "c55-chain-exchange"
    answer = next(exchange.glob(f"*-{second.removeprefix('tgos-')[:8]}")) / "Address_Finish.csv"
    response(answer, [{"Address": q["address"], "Response_Address": q["address"] if q is village else "",
                       "Response_X": GOOD[0] if q is village else "", "Response_Y": GOOD[1] if q is village else ""}
                      for q in asked.values()])
    sent = transition_batch(again, tmp / "c55-chain", second, "submitted")
    imported = import_tgos(sent, tmp / "c55-chain", second, answer, index=world["index"])
    query = next(q for q in rows(imported / "tgos_queries.parquet")
                 if q["batch_id"] == second and q["building_key"] == village_key)
    assert query["status"] == "conflict"
    _, report, _ = load_state(imported)
    assert report["status_counts"]["conflict"] >= 1

    # run-full re-imports both batches in order; the second one also sees the fill of the first.
    shutil.copytree(adopted["exchange"] / world["folder"].relative_to(world["exchange"]),
                    exchange / world["folder"].relative_to(world["exchange"]))
    rerun = tmp / "c55-rerun"
    rerun_offline = resolve_offline(adopted["pool"], world["index"], rerun)
    plan = plan_tgos_state(rerun_offline, imported, exchange, tmp)
    state = build_tgos_state(rerun_offline, rerun, plan, index=world["index"])
    query = next(q for q in rows(state / "tgos_queries.parquet")
                 if q["batch_id"] == second and q["building_key"] == village_key)
    assert query["status"] == "conflict"
    located = {r["building_key"]: r for r in rows(state / "address_index.parquet")}[village_key]
    assert located["status"] == "conflict"
