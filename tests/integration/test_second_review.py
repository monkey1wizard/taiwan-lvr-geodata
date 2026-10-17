"""R10-1a: second manual-review area and the owner's decision files, built from synthetic snapshots."""

import csv
import hashlib
import io
import json

import pytest

from lvr_pipeline.addresses.identity import building_key
from lvr_pipeline.contracts.schemas import EXCEPTION_REASON_CODES
from lvr_pipeline.offline.index import build_index
from lvr_pipeline.addresses.pool import build_pool
from lvr_pipeline.results.reconcile import load_p2, load_suspended_counties, resolve_offline
from lvr_pipeline.results.review import build_review
from lvr_pipeline.results.second_review import DECISION_COLUMNS, ENTRY_REASONS
from lvr_pipeline.output.rows import make_transaction_id
from lvr_pipeline.storage.parquet import rows
from lvr_pipeline.storage.runs import digest, sha256_file
from lvr_pipeline.tgos import import_tgos, load_state, merge_ledger, prepare_tgos, transition_batch
from lvr_pipeline.tgos.batches import _artifact_paths, _table, _write_state
import test_r05_9_suspension as suspension
from test_p1_conversion import ADDRESS, pipeline
from test_p2_offline import index_of, register_index, run
from test_p3_tgos import response

CONFLICT = "臺北市中正區測試路30號"  # two coordinates far apart in the source
NOT_FOUND = "臺北市中正區測試路11號"  # TGOS finds nothing
ISOLATED = "臺北市中正區測試路12號"  # TGOS answers another door
VILLAGE_Q = "臺北市中正區甲里測試路17號"  # TGOS answers the same door in another village
INCOMPLETE = "臺北市中正區測試路13號"  # TGOS answers an address without a door
FAR = "臺北市中正區測試路14號"  # TGOS answers a coordinate outside Taiwan
FOUND = "臺北市中正區測試路15號"  # TGOS locates it
AMBIGUOUS = "臺北市中正區測試路16號"  # the pool holds the door under two villages
VILLAGE_A = "臺北市中正區甲里測試路16號"
VILLAGE_B = "臺北市中正區乙里測試路16號"
PLAIN = "臺北市測試路99號"  # district_missing; TGOS finds nothing
FILLED = "臺北市大安里測試路98號"  # district_missing; TGOS fills it
NO_ROAD = "臺北市無此路7號"  # district_missing; no road of that name in the index
ANSWERS = {
    ISOLATED: ("臺北市中正區測試路99號", "121.51", "25.01"),
    VILLAGE_Q: ("臺北市中正區乙里測試路17號", "121.52", "25.02"),
    INCOMPLETE: ("臺北市中正區測試路", "121.53", "25.03"),
    FAR: (FAR, "10", "10"),
    FOUND: (FOUND, "121.54", "25.04"),
    AMBIGUOUS: (VILLAGE_A, "121.55", "25.05"),
    FILLED: ("臺北市中正區大安里測試路98號", "121.56", "25.06"),
}
SOURCE = [
    (ADDRESS, "63", "6300100", 121.5, 25.0),
    (CONFLICT, "63", "6300100", 121.5, 25.0),
    (CONFLICT, "63", "6300100", 121.6, 25.1),
    ("臺北市文山區測試路11號", "63", "6300800", 121.55, 25.02),
]
BOGUS_KEY = 'v2:{"bogus":"ledger value"}'


def _key(address):
    return building_key(address)


def _decision_file(directory, date, number, decisions, header=DECISION_COLUMNS):
    """Write a decision file named after the hash of its content; ``decisions`` are dicts."""
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(header)
    for item in decisions:
        values = {"mode": "", "tgos_result_id": "", "lng": "", "lat": "", "address": "",
                  "basis": "test basis", "decided_by": "owner", "decided_at": "2026-10-18", **item}
        writer.writerow([values[name] for name in DECISION_COLUMNS])
    data = stream.getvalue().encode("utf-8")
    path = directory / f"{date}-{number:02d}-{hashlib.sha256(data).hexdigest()[:8]}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _raw_file(directory, name, data):
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def build_base(tmp, extra_records=()):
    """The synthetic world of this module in ``tmp``: converted, offline state and one imported TGOS batch."""
    records = [{"address": a, "date": "1150102"} for a in [
        ADDRESS, CONFLICT, NOT_FOUND, ISOLATED, VILLAGE_Q, INCOMPLETE, FAR, FOUND, AMBIGUOUS, VILLAGE_A,
        VILLAGE_B, PLAIN, FILLED, NO_ROAD]] + list(extra_records)
    converted, _, descriptor, index, _, state = run(tmp, coordinates=SOURCE, records=records)
    work = tmp / "work"
    prepared, _ = prepare_tgos(state, work, tmp / "exchange", limit=30, exchange_date="2026-10-18",
                               index=index_of(state))
    queries = list(rows(prepared / "tgos_queries.parquet"))
    batch_id = queries[0]["batch_id"]
    submitted = transition_batch(prepared, work, batch_id, "submitted")
    path = tmp / "response.csv"
    response(path, [
        {"Address": q["address"], "Response_Address": ANSWERS.get(q["address"], ("", "", ""))[0],
         "Response_X": ANSWERS.get(q["address"], ("", "", ""))[1],
         "Response_Y": ANSWERS.get(q["address"], ("", "", ""))[2]}
        for q in queries])
    imported = import_tgos(submitted, work, batch_id, path)
    # A ledger that carries a wrong response key, to show the review recomputes it.
    manifest, report, _ = load_state(imported)
    paths = _artifact_paths(imported, manifest)
    results = _table(paths["tgos-result"])
    for result in results:
        if result["address"] == INCOMPLETE:
            result["response_building_key"] = BOGUS_KEY
    ledger = _write_state(
        imported, work, action="import", batches=_table(paths["tgos-batch"]), queries=_table(paths["tgos-query"]),
        results=results, aliases=_table(paths["verified-alias"]), alias_events=_table(paths["alias-event"]))
    return {"tmp": tmp, "converted": converted, "descriptor": descriptor, "index": index, "state": state,
            "prepared": prepared, "submitted": submitted, "ledger": ledger, "tgos": merged(tmp, work, ledger),
            "queries": queries, "results": {r["address"]: r for r in rows(ledger / "tgos_imports.parquet")}}


def merged(tmp, work, ledger):
    """An import or prepare snapshot holds the ledger only; review and final state read its merged tgos-state."""
    return merge_ledger(ledger, work, tmp / "merged" / ledger.name)


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    return build_base(tmp_path_factory.mktemp("second_review"))


_builds = {"count": 0}


def review(base, *, tgos=True, index=True, decisions=None, work=None, state=None):
    """Build the review stage; every call without ``work`` gets its own work directory."""
    _builds["count"] += 1
    return build_review(
        base["converted"], base["state"], work or base["tmp"] / f"review-{_builds['count']}",
        descriptor=base["descriptor"],
        tgos_state=(merged(base["tmp"], base["tmp"] / "work", state) if state else base["tgos"]) if tgos else None,
        index=base["index"] if index else None, review_decisions=decisions)


def second(path):
    return list(rows(path / "second_review.parquet"))


def by_address(values, address):
    found = [r for r in values if r["canonical_address"] == address]
    assert len(found) == 1, (address, [r["canonical_address"] for r in values])
    return found[0]


@pytest.fixture(scope="module")
def plain(base):
    return review(base)


def test_entry_reasons_are_known_codes():
    assert set(ENTRY_REASONS) - {"conflict"} <= set(EXCEPTION_REASON_CODES)
    assert {"tgos_not_found", "tgos_key_text_not_round_trip", "district_no_candidate_town"} <= set(
        EXCEPTION_REASON_CODES)


def test_every_entry_reason_has_a_row(base, plain, monkeypatch):
    values = second(plain)
    assert {r["entry_reason"] for r in values} == {
        "tgos_not_found", "tgos_isolated", "tgos_coordinate_invalid", "tgos_response_incomplete",
        "tgos_village_ambiguous", "district_no_candidate_town", "conflict"}
    assert by_address(values, NOT_FOUND)["entry_reason"] == "tgos_not_found"
    isolated = by_address(values, ISOLATED)
    assert isolated["entry_reason"] == "tgos_isolated" and json.loads(isolated["mismatch_fields"]) == ["door"]
    village = by_address(values, VILLAGE_Q)
    assert village["entry_reason"] == "tgos_isolated" and json.loads(village["mismatch_fields"]) == ["village"]
    assert by_address(values, FAR)["entry_reason"] == "tgos_coordinate_invalid"
    assert by_address(values, INCOMPLETE)["entry_reason"] == "tgos_response_incomplete"
    assert by_address(values, AMBIGUOUS)["entry_reason"] == "tgos_village_ambiguous"
    conflict = by_address(values, CONFLICT)
    assert conflict["entry_reason"] == "conflict" and conflict["offline_status"] == "conflict"
    assert conflict["tgos_result_id"] is None
    # District queries: PLAIN was sent and not found; NO_ROAD was never sent and has no candidate town.
    plain_row = by_address(values, PLAIN)
    assert plain_row["key_kind"] == "district_query" and plain_row["entry_reason"] == "tgos_not_found"
    assert plain_row["review_key"] == plain_row["query_fingerprint"] == digest(["district_missing", "63", PLAIN])
    assert plain_row["building_key"] is None and plain_row["offline_status"] is None
    no_road = by_address(values, NO_ROAD)
    assert no_road["entry_reason"] == "district_no_candidate_town" and no_road["key_kind"] == "district_query"
    assert no_road["review_key"] == digest(["district_missing", "63", NO_ROAD])
    assert all(r["status"] == "pending" and r["decision_file"] is None for r in values)
    assert all(r["review_id"] == digest(["second_review", r["review_key"]]) for r in values)
    # The one reason with no TGOS query: the key text does not give the key back.
    import lvr_pipeline.tgos.batches as batches
    bad = _key(NOT_FOUND)
    real = batches.key_query_text

    def text(key):
        if key == bad:
            raise ValueError("TGOS query text does not give back its building key")
        return real(key)

    monkeypatch.setattr(batches, "key_query_text", text)
    offline = second(review(base, tgos=False))
    row = by_address(offline, NOT_FOUND)
    assert row["entry_reason"] == "tgos_key_text_not_round_trip" and row["review_key"] == bad
    assert row["tgos_result_id"] is None and row["offline_status"] == "unmatched"
    # Never-queried keys that do round-trip do not enter.
    assert {r["entry_reason"] for r in offline} == {
        "tgos_key_text_not_round_trip", "conflict", "district_no_candidate_town"}
    assert {r["canonical_address"] for r in offline if r["key_kind"] == "district_query"} == {NO_ROAD}


def test_waiting_queries_do_not_enter(base):
    for waiting in (base["prepared"], base["submitted"]):
        values = second(review(base, state=waiting))
        assert {r["entry_reason"] for r in values} == {"conflict", "district_no_candidate_town"}
        assert not [r for r in values if r["canonical_address"] in {NOT_FOUND, ISOLATED, PLAIN}]


def test_located_and_filled_keys_do_not_enter(base, plain):
    values = second(plain)
    # FOUND was located by TGOS; FILLED is a succeeded district fill; ADDRESS was located offline.
    assert not [r for r in values if r["canonical_address"] in {FOUND, FILLED, ADDRESS}]
    assert base["results"][FOUND]["status"] == "succeeded" and base["results"][FILLED]["status"] == "succeeded"


def test_conflict_enters_but_suspended_county_does_not(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("second_review_susp")
    config = suspension._suspension_config(tmp)
    records = [{"address": a, "date": "1150102", "extra": [suspension.DISTRICT.get(a, "金城鎮")]}
               for a in [ADDRESS, CONFLICT, suspension.KINMEN_ONE]]
    records[1]["extra"] = ["中正區"]
    _, _, converted = pipeline(tmp, records, extra_header=["鄉鎮市區"])
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(suspension.SOURCE, "63", [
            (ADDRESS, "6300100", 121.5, 25.0), (CONFLICT, "6300100", 121.5, 25.0), (CONFLICT, "6300100", 121.6, 25.1)])
        root, descriptor = suspension._source(tmp)
    work = tmp / "work"
    index = register_index(build_index(root, descriptor, work, counties=list(suspension.SOURCE)))
    pool = build_pool(converted, root, descriptor, work, index_path=index)
    state = resolve_offline(pool, index, work, suspended_counties=load_suspended_counties(config))
    statuses = {r["canonical_address"]: r["status"] for r in rows(state / "address_index.parquet")}
    assert statuses[CONFLICT] == "conflict" and statuses[suspension.KINMEN_ONE] == "conflict"
    path = build_review(converted, state, tmp / "review", descriptor=descriptor, index=index)
    values = second(path)
    assert [r["canonical_address"] for r in values] == [CONFLICT]
    assert values[0]["entry_reason"] == "conflict"


def test_report_counts_and_district_check_flag(base, plain):
    report = json.loads((plain / "quality.json").read_text(encoding="utf-8"))
    block = report["second_review"]
    assert block["district_candidates_checked"] is True and block["rows"] == len(second(plain))
    assert block["entry_reason_counts"]["district_no_candidate_town"] == 1
    assert block["status_counts"] == {"pending": len(second(plain)), "adopted": 0, "rejected": 0, "deferred": 0}
    assert block["decision_files"] == [] and block["decision_totals"]["applied"] == 0
    assert report["dataset_counts"]["second-review"] == block["rows"]
    assert load_p2(plain, "review")[1]["dataset_counts"] == report["dataset_counts"]
    assert report["index_manifest_sha256"] == sha256_file(base["index"] / "manifest.json")
    # Without the index the district entry reason is not produced and the report says so.
    without = review(base, index=False)
    block = json.loads((without / "quality.json").read_text(encoding="utf-8"))["second_review"]
    assert block["district_candidates_checked"] is False
    assert block["entry_reason_counts"]["district_no_candidate_town"] == 0
    assert "district_no_candidate_town" not in {r["entry_reason"] for r in second(without)}
    csv_rows = list(csv.DictReader((plain / "second_review.csv").open(encoding="utf-8-sig", newline="")))
    assert [r["review_id"] for r in csv_rows] == [r["review_id"] for r in second(plain)]


def _decide(base, tmp_path, files):
    """Write ``files`` (list of lists of decision dicts) as 20261018-NN files and build the review."""
    directory = tmp_path / "decisions"
    for number, decisions in enumerate(files, 1):
        _decision_file(directory, "20261018", number, decisions)
    return review(base, decisions=directory), directory


def test_four_decisions_are_applied(base, plain, tmp_path):
    values = second(plain)
    isolated, village = by_address(values, ISOLATED), by_address(values, VILLAGE_Q)
    not_found, incomplete = by_address(values, NOT_FOUND), by_address(values, INCOMPLETE)
    path, directory = _decide(base, tmp_path, [[
        {"review_key": isolated["review_key"], "decision": "adopted", "mode": "accept_tgos_response",
         "tgos_result_id": isolated["tgos_result_id"]},
        {"review_key": village["review_key"], "decision": "adopted", "mode": "manual_coordinate",
         "lng": "121.52", "lat": "25.02", "address": "臺北市中正區乙里測試路17號"},
        {"review_key": not_found["review_key"], "decision": "rejected"},
        {"review_key": incomplete["review_key"], "decision": "deferred"},
    ]])
    after = {r["review_key"]: r for r in second(path)}
    name = next(directory.iterdir()).name
    accepted = after[isolated["review_key"]]
    assert accepted["status"] == "adopted" and accepted["decision_mode"] == "accept_tgos_response"
    assert (accepted["decision_lng"], accepted["decision_lat"]) == (121.51, 25.01)
    assert accepted["decision_address"] == "臺北市中正區測試路99號"
    assert accepted["decision_file"] == name and accepted["decision_row"] == 1 and accepted["decided_at"] == "2026-10-18"
    assert accepted["decision_file_sha256"] == sha256_file(directory / name)
    manual = after[village["review_key"]]
    assert manual["status"] == "adopted" and manual["decision_mode"] == "manual_coordinate"
    assert (manual["decision_lng"], manual["decision_lat"], manual["decision_row"]) == (121.52, 25.02, 2)
    assert manual["decision_address"] == "臺北市中正區乙里測試路17號"
    assert after[not_found["review_key"]]["status"] == "rejected" and after[not_found["review_key"]]["decision_lng"] is None
    assert after[incomplete["review_key"]]["status"] == "deferred"
    assert sum(r["status"] != "pending" for r in after.values()) == 4
    # Decisions never change keys or merge rows.
    assert {k: (r["building_key"], r["canonical_address"]) for k, r in after.items()} == {
        r["review_key"]: (r["building_key"], r["canonical_address"]) for r in values}
    block = json.loads((path / "quality.json").read_text(encoding="utf-8"))["second_review"]
    assert block["status_counts"] == {"pending": len(values) - 4, "adopted": 2, "rejected": 1, "deferred": 1}
    assert block["decision_files"] == [{"file": name, "sha256": sha256_file(directory / name), "rows": 4,
                                        "applied": 4, "superseded": 0, "orphaned": 0, "stale": 0}]


def test_duplicate_review_key_in_one_file_is_rejected(base, tmp_path):
    key = _key(NOT_FOUND)
    with pytest.raises(ValueError, match="repeats review_key"):
        _decide(base, tmp_path, [[{"review_key": key, "decision": "rejected"},
                                   {"review_key": key, "decision": "deferred"}]])


def test_file_name_hash_must_match_the_content(base, tmp_path):
    directory = tmp_path / "decisions"
    path = _decision_file(directory, "20261018", 1, [{"review_key": _key(NOT_FOUND), "decision": "rejected"}])
    path.rename(directory / "20261018-01-00000000.csv")
    with pytest.raises(ValueError, match="differs from its name"):
        review(base, decisions=directory)


HEADER = ",".join(DECISION_COLUMNS) + "\n"
OK_TAIL = ",,,,,,b,o,2026-10-18\n"


@pytest.mark.parametrize("content,message", [
    (b"\xef\xbb\xbf" + HEADER.encode(), "BOM"),
    (b"review_key,decision\n", "header"),
    (b"", "header"),
    ((HEADER + "x,maybe" + OK_TAIL).encode(), "row 1: decision"),
    ((HEADER + "x,adopted" + OK_TAIL).encode(), "row 1: adopted needs mode"),
    ((HEADER + "x,adopted,manual_coordinate,,121.5,25.0,,b,o,2026-10-18\n").encode(), "row 1: manual_coordinate needs"),
    ((HEADER + "x,adopted,accept_tgos_response,,,,,b,o,2026-10-18\n").encode(), "row 1: accept_tgos_response needs"),
    ((HEADER + "x,rejected,,,,,,,o,2026-10-18\n").encode(), "row 1: basis is empty"),
    ((HEADER + "x,rejected,,,,,,b,o,2026-13-40\n").encode(), "row 1: decided_at"),
    ((HEADER + "x,rejected,,,,,,b,o,18-10-2026\n").encode(), "row 1: decided_at"),
    ((HEADER + "x,rejected\n").encode(), "row 1 has 2 fields"),
    ((HEADER + "x,rejected,,,,,,b,o,2026-10-18\n\n").encode(), "row 2 has 0 fields"),
])
def test_malformed_decision_files_fail_the_stage(base, tmp_path, content, message):
    directory = tmp_path / "decisions"
    _raw_file(directory, f"20261018-01-{hashlib.sha256(content).hexdigest()[:8]}.csv", content)
    with pytest.raises(ValueError, match=message):
        review(base, decisions=directory)


def test_file_name_must_follow_the_pattern(base, tmp_path):
    directory = tmp_path / "decisions"
    _raw_file(directory, "notes.csv", HEADER.encode())
    with pytest.raises(ValueError, match="file name"):
        review(base, decisions=directory)
    with pytest.raises(ValueError, match="does not exist"):
        review(base, decisions=tmp_path / "missing")


def test_unknown_key_is_orphaned_and_not_applied(base, plain, tmp_path):
    located = _key(FOUND)  # located by TGOS: an adopted decision keeps it in the area (R10-1b)
    path, directory = _decide(base, tmp_path, [[
        {"review_key": BOGUS_KEY, "decision": "rejected"},
        {"review_key": located, "decision": "adopted", "mode": "manual_coordinate",
         "lng": "121.5", "lat": "25.0", "address": FOUND},
    ]])
    values = second(path)
    assert len(values) == len(second(plain)) + 1
    assert [r["status"] for r in values if r["entry_reason"] != "tgos_manual_conflict"] == ["pending"] * len(second(plain))
    block = json.loads((path / "quality.json").read_text(encoding="utf-8"))["second_review"]
    assert block["decision_totals"] == {"rows": 2, "applied": 1, "superseded": 0, "orphaned": 1, "stale": 0}


def test_tgos_located_key_with_an_adopted_decision_stays_as_tgos_manual_conflict(base, plain, tmp_path):
    located = _key(FOUND)
    path, _ = _decide(base, tmp_path, [[
        {"review_key": located, "decision": "adopted", "mode": "manual_coordinate",
         "lng": "121.5", "lat": "25.0", "address": "臺北市中正區測試路15號之一"},
    ]])
    row = by_address(second(path), FOUND)
    assert row["entry_reason"] == "tgos_manual_conflict" and row["review_key"] == located
    assert row["status"] == "adopted" and row["decision_mode"] == "manual_coordinate"
    assert (row["decision_lng"], row["decision_lat"]) == (121.5, 25.0)
    assert row["offline_status"] == "located" and row["observation_count"] == 3
    assert (row["response_lng"], row["response_lat"]) == (121.54, 25.04)
    # A later rejected decision means the key has no adopted decision, so it does not stay in the area.
    path, _ = _decide(base, tmp_path / "again", [
        [{"review_key": located, "decision": "adopted", "mode": "manual_coordinate",
          "lng": "121.5", "lat": "25.0", "address": FOUND}],
        [{"review_key": located, "decision": "rejected"}]])
    assert not [r for r in second(path) if r["canonical_address"] == FOUND]
    block = json.loads((path / "quality.json").read_text(encoding="utf-8"))["second_review"]
    # The key is not in the area of this run, so both decisions are orphaned.
    assert block["decision_totals"]["orphaned"] == 2 and block["decision_totals"]["applied"] == 0
    assert block["entry_reason_counts"]["tgos_manual_conflict"] == 0


def test_invalid_coordinate_or_foreign_result_is_stale(base, plain, tmp_path):
    values = second(plain)
    far, isolated, village = by_address(values, FAR), by_address(values, ISOLATED), by_address(values, VILLAGE_Q)
    assert (far["response_lng"], far["response_lat"]) == (10.0, 10.0)
    path, _ = _decide(base, tmp_path, [[
        # The result's coordinate is outside Taiwan.
        {"review_key": far["review_key"], "decision": "adopted", "mode": "accept_tgos_response",
         "tgos_result_id": far["tgos_result_id"]},
        # A result of another key.
        {"review_key": isolated["review_key"], "decision": "adopted", "mode": "accept_tgos_response",
         "tgos_result_id": village["tgos_result_id"]},
        # A result that is not in the ledger.
        {"review_key": village["review_key"], "decision": "adopted", "mode": "accept_tgos_response",
         "tgos_result_id": "0" * 64},
        {"review_key": by_address(values, NOT_FOUND)["review_key"], "decision": "adopted",
         "mode": "manual_coordinate", "lng": "abc", "lat": "25", "address": NOT_FOUND},
        {"review_key": by_address(values, INCOMPLETE)["review_key"], "decision": "adopted",
         "mode": "manual_coordinate", "lng": "10", "lat": "10", "address": INCOMPLETE},
        # No TGOS evidence at all.
        {"review_key": by_address(values, CONFLICT)["review_key"], "decision": "adopted",
         "mode": "accept_tgos_response", "tgos_result_id": isolated["tgos_result_id"]},
    ]])
    assert [r["status"] for r in second(path)] == ["pending"] * len(values)
    block = json.loads((path / "quality.json").read_text(encoding="utf-8"))["second_review"]
    assert block["decision_totals"] == {"rows": 6, "applied": 0, "superseded": 0, "orphaned": 0, "stale": 6}


def test_same_decision_files_give_identical_outputs(base, tmp_path):
    key = _key(NOT_FOUND)
    directory = tmp_path / "decisions"
    _decision_file(directory, "20261018", 1, [{"review_key": key, "decision": "rejected"}])
    names = ("second_review.parquet", "second_review.csv", "second_review_observations.parquet")
    first = review(base, decisions=directory)
    again = review(base, decisions=directory)
    assert first != again
    assert {n: sha256_file(first / n) for n in names} == {n: sha256_file(again / n) for n in names}
    # The same inputs and work directory reuse the stage.
    work = base["tmp"] / "review-fixed"
    assert review(base, decisions=directory, work=work) == review(base, decisions=directory, work=work)


def test_later_decision_replaces_an_earlier_one(base, tmp_path):
    key = _key(NOT_FOUND)
    path, directory = _decide(base, tmp_path, [
        [{"review_key": key, "decision": "rejected"}],
        [{"review_key": key, "decision": "adopted", "mode": "manual_coordinate",
          "lng": "121.5", "lat": "25.0", "address": NOT_FOUND}],
        [{"review_key": key, "decision": "adopted", "mode": "manual_coordinate",
          "lng": "abc", "lat": "25.0", "address": NOT_FOUND}],  # stale: does not replace the second
    ])
    row = next(r for r in second(path) if r["review_key"] == key)
    names = sorted(p.name for p in directory.iterdir())
    assert row["status"] == "adopted" and row["decision_file"] == names[1]
    block = json.loads((path / "quality.json").read_text(encoding="utf-8"))["second_review"]
    assert [(f["file"], f["applied"], f["superseded"], f["stale"]) for f in block["decision_files"]] == [
        (names[0], 0, 1, 0), (names[1], 1, 0, 0), (names[2], 0, 0, 1)]
    assert block["decision_totals"]["applied"] == 1 and block["decision_totals"]["superseded"] == 1


def test_a_new_decision_file_rebuilds_the_review_stage(base, tmp_path):
    directory = tmp_path / "decisions"
    directory.mkdir()
    work = base["tmp"] / "review-rebuild"
    empty = review(base, decisions=directory, work=work)
    assert review(base, decisions=directory, work=work) == empty
    _decision_file(directory, "20261018", 1, [{"review_key": _key(NOT_FOUND), "decision": "deferred"}])
    rebuilt = review(base, decisions=directory, work=work)
    assert rebuilt != empty
    assert [r["status"] for r in second(rebuilt) if r["canonical_address"] == NOT_FOUND] == ["deferred"]
    assert [r["status"] for r in second(empty) if r["canonical_address"] == NOT_FOUND] == ["pending"]
    assert review(base, decisions=directory, work=work) == rebuilt


def test_response_key_is_recomputed_and_the_ledger_column_ignored(base, plain):
    values = second(plain)
    isolated = by_address(values, ISOLATED)
    assert base["results"][ISOLATED]["response_building_key"] is None
    assert isolated["response_building_key"] == _key("臺北市中正區測試路99號")
    assert isolated["response_building_key_reason"] is None
    assert isolated["response_address"] == "臺北市中正區測試路99號"
    assert (isolated["response_lng"], isolated["response_lat"]) == (121.51, 25.01)
    assert isolated["response_sha256"] == base["results"][ISOLATED]["response_sha256"]
    # A wrong ledger value is not read, and a response that gives no key says why.
    assert base["results"][INCOMPLETE]["response_building_key"] == BOGUS_KEY
    incomplete = by_address(values, INCOMPLETE)
    assert incomplete["response_building_key"] is None
    assert incomplete["response_building_key_reason"] == "building_key returned None"
    not_found = by_address(values, NOT_FOUND)
    assert not_found["response_building_key"] is None and not_found["response_building_key_reason"] == "no response address"
    assert not_found["prior_result_ids"] == "[]"


def test_observations_link_transactions_and_doors(base, plain):
    values = second(plain)
    observations = list(rows(plain / "second_review_observations.parquet"))
    converted = base["converted"]
    manifest = json.loads((converted / "manifest.json").read_text(encoding="utf-8"))
    source = {r["raw_record_id"]: r for item in manifest["artifacts"] if item["schema"] == "observation"
              for r in rows(converted / item["path"])}
    for address in (NOT_FOUND, PLAIN, CONFLICT):
        row = by_address(values, address)
        linked = [o for o in observations if o["review_id"] == row["review_id"]]
        assert row["observation_count"] == len(linked) == 3
        assert len({o["transaction_id"] for o in linked}) == 3 and {o["door_index"] for o in linked} == {1}
        assert {o["category"] for o in linked} == {"sales", "presale", "rent"}
        for o in linked:
            ob = source[o["raw_record_id"]]
            assert o["transaction_id"] == make_transaction_id(ob["src_batch"], ob["member_path"], ob["source_row_number"])
            assert o["tx_yyyymm"] == ob["tx_yyyymm"] == 202601
        assert row["tx_yyyymm_min"] == row["tx_yyyymm_max"] == 202601
    assert sum(r["observation_count"] for r in values) == len(observations)
    assert len({(o["review_id"], o["raw_record_id"], o["component_id"]) for o in observations}) == len(observations)


def test_district_address_without_a_parseable_door_enters_with_the_address_digest(base, monkeypatch):
    # The address pool only labels district_missing when a door parses, so this branch is driven by
    # making the second review's own parse fail for one address.
    import lvr_pipeline.results.second_review as module
    real = module.district_query
    monkeypatch.setattr(module, "district_query", lambda text: None if text == NO_ROAD else real(text))
    row = by_address(second(review(base)), NO_ROAD)
    assert row["entry_reason"] == "district_no_candidate_town" and row["key_kind"] == "district_query"
    assert row["review_key"] == digest(["district_missing", "63", NO_ROAD])
    assert row["observation_count"] == 3


def test_rejected_result_keeps_its_raw_coordinate_and_can_be_accepted(base, plain, tmp_path):
    """response_lng/response_lat are the returned pair also for a rejected row; accept_tgos_response adopts it."""
    isolated = by_address(second(plain), ISOLATED)
    assert (isolated["response_lng"], isolated["response_lat"]) == (121.51, 25.01)
    result = base["results"][ISOLATED]
    assert result["status"] == "rejected" and result["lng"] is None and (result["response_lng"], result["response_lat"]) == (
        121.51, 25.01)
    far = by_address(second(plain), FAR)
    assert (far["response_lng"], far["response_lat"]) == (10.0, 10.0)
    path, _ = _decide(base, tmp_path, [[
        {"review_key": isolated["review_key"], "decision": "adopted", "mode": "accept_tgos_response",
         "tgos_result_id": isolated["tgos_result_id"]},
        {"review_key": far["review_key"], "decision": "adopted", "mode": "accept_tgos_response",
         "tgos_result_id": far["tgos_result_id"]},
    ]])
    after = {r["review_key"]: r for r in second(path)}
    adopted = after[isolated["review_key"]]
    assert adopted["status"] == "adopted" and (adopted["decision_lng"], adopted["decision_lat"]) == (121.51, 25.01)
    assert adopted["decision_address"] == "臺北市中正區測試路99號"
    # The raw coordinate is outside Taiwan: stale, nothing adopted.
    assert after[far["review_key"]]["status"] == "pending"
    block = json.loads((path / "quality.json").read_text(encoding="utf-8"))["second_review"]
    assert block["decision_totals"] == {"rows": 2, "applied": 1, "superseded": 0, "orphaned": 0, "stale": 1}


def keyless_world(base):
    """The ledger of ``base`` where the NOT_FOUND query has no key under the current rules (a carried query)."""
    from lvr_pipeline.tgos.validate import KEYLESS_REASON

    manifest, _, _ = load_state(base["ledger"])
    paths = _artifact_paths(base["ledger"], manifest)
    queries, results = _table(paths["tgos-query"]), _table(paths["tgos-result"])
    query = next(q for q in queries if q["address"] == NOT_FOUND)
    query["query_kind"], query["building_key"] = "keyless", None
    result = next(r for r in results if r["query_fingerprint"] == query["query_fingerprint"])
    result.update(status="rejected", reason=KEYLESS_REASON, lng=None, lat=None, response_building_key=None,
                  response_address=NOT_FOUND, response_lng=121.5, response_lat=25.0)
    ledger = _write_state(
        base["ledger"], base["tmp"] / "work", action="import", batches=_table(paths["tgos-batch"]), queries=queries,
        results=results, aliases=_table(paths["verified-alias"]), alias_events=_table(paths["alias-event"]))
    return merged(base["tmp"], base["tmp"] / "work", ledger), query


def test_keyless_query_enters_without_doors_and_every_decision_on_it_is_stale(base, plain, tmp_path):
    tgos, query = keyless_world(base)
    fingerprint = query["query_fingerprint"]
    directory = tmp_path / "decisions"
    _decision_file(directory, "20261018", 1, [
        {"review_key": fingerprint, "decision": "adopted", "mode": "manual_coordinate",
         "lng": "121.5", "lat": "25.0", "address": NOT_FOUND}])
    _decision_file(directory, "20261018", 2, [{"review_key": fingerprint, "decision": "rejected"}])
    _builds["count"] += 1
    path = build_review(base["converted"], base["state"], base["tmp"] / f"review-{_builds['count']}",
                        descriptor=base["descriptor"], tgos_state=tgos, index=base["index"], review_decisions=directory)
    entries = [r for r in second(path) if r["entry_reason"] == "tgos_keyless_query"]
    assert len(entries) == 1
    row = entries[0]
    assert row["key_kind"] == "keyless_query" and row["review_key"] == row["query_fingerprint"] == fingerprint
    assert row["building_key"] is None and row["observation_count"] == 0 and row["canonical_address"] == NOT_FOUND
    assert row["county_code"] == query["county_code"] and row["review_id"] == digest(["second_review", fingerprint])
    assert (row["response_address"], row["response_lng"], row["response_lat"]) == (NOT_FOUND, 121.5, 25.0)
    assert row["tgos_batch_id"] == query["batch_id"] and row["tgos_result_id"] and row["response_sha256"]
    assert row["status"] == "pending" and row["decision_file"] is None and row["decision_mode"] is None
    assert not [o for o in rows(path / "second_review_observations.parquet") if o["review_id"] == row["review_id"]]
    block = json.loads((path / "quality.json").read_text(encoding="utf-8"))["second_review"]
    assert block["entry_reason_counts"]["tgos_keyless_query"] == 1
    assert block["decision_totals"] == {"rows": 2, "applied": 0, "superseded": 0, "orphaned": 0, "stale": 2}
    # The offline status of the key itself is untouched, and the review table gets no row for it.
    report = json.loads((path / "quality.json").read_text(encoding="utf-8"))
    assert report["reason_counts"]["tgos_keyless_query"] == 0
    assert "tgos_keyless_query" in EXCEPTION_REASON_CODES
