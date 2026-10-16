"""R09-0 and R09-2b: TGOS rebuild under the current address rules.

1. A carried ledger query with no key under the current rules makes prepare-tgos fail.
2. prepare-tgos does not select keys of suspended counties.
"""

import pytest

from lvr_pipeline.addresses.identity import building_key
from lvr_pipeline.storage.parquet import rows
from lvr_pipeline.storage.runs import digest
from lvr_pipeline.tgos import (
    _artifact_paths,
    _write_state,
    load_state,
    prepare_tgos,
    transition_batch,
)
from test_p1_conversion import ADDRESS
from test_p2_offline import run
from test_r05_9_suspension import KINMEN_NONE, TAIPEI_TGOS, built  # noqa: F401  built is a fixture

UNKEYED = "臺北市中正區測試路7鄰鄰21號"  # county and town parse, the door text yields no key
OTHERS = ["臺北市中正區測試路21號", "臺北市中正區測試路22號", "臺北市中正區測試路23號"]


@pytest.fixture(scope="module")
def ledger(tmp_path_factory):
    """An old TGOS state whose submitted batch holds a query that has no key under the current rules."""
    assert building_key(UNKEYED) is None
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


def test_ledger_query_without_a_key_makes_prepare_fail(ledger):
    with pytest.raises(ValueError, match="Carried TGOS query has no building key under the current rules"):
        prepare_tgos(ledger["state"], ledger["tmp"] / "new", ledger["tmp"] / "exchange",
                     ledger=ledger["ledger"], limit=1)


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
