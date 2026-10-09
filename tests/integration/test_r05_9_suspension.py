"""R05-9: Penghu and Kinmen addresses are not located (owner decision 2026-10-09, option B).

Synthetic address source with three counties: Taipei (63), Kinmen (09020, suspended
by the default config) and Lienchiang (09007, not suspended).
"""

import copy
import csv
import json
import subprocess

import pytest

from lvr_pipeline.address_pool import build_pool
from lvr_pipeline.contracts.schemas import EXCEPTION_REASON_CODES
from lvr_pipeline.contracts.validate import validate_dataset_rows
from lvr_pipeline.offline.index import build_index
from lvr_pipeline.results.reconcile import (
    SUSPENDED,
    check_suspended_counties,
    check_suspension,
    load_p2,
    load_suspended_counties,
    resolve_offline,
)
from lvr_pipeline.results.review import build_review
from lvr_pipeline.storage.parquet import rows
from lvr_pipeline.storage.runs import SnapshotStore, sha256_file
from lvr_pipeline.tgos import import_tgos, load_state, prepare_tgos, transition_batch
from test_p1_conversion import ADDRESS, pipeline
from test_p3_tgos import response

KINMEN_ONE = "金門縣金城鎮測試路1號"  # one coordinate in the source
KINMEN_TWO = "金門縣金城鎮測試路2號"  # two coordinates 10 m apart (within the tolerance)
KINMEN_NONE = "金門縣金城鎮測試路3號"  # no coordinate; TGOS answers it
LIENCHIANG = "連江縣南竿鄉測試路1號"
TAIPEI_TGOS = "臺北市中正區測試路11號"  # no coordinate; TGOS answers it
ADDRESSES = [ADDRESS, KINMEN_ONE, KINMEN_TWO, KINMEN_NONE, LIENCHIANG, TAIPEI_TGOS]
DISTRICT = {ADDRESS: "中正區", TAIPEI_TGOS: "中正區", LIENCHIANG: "南竿鄉"}
TEN_M = 10 / 111194.92664455873  # degrees of latitude in 10 m on the audit sphere
SOURCE = {
    "63": [(ADDRESS, "6300100", 121.5, 25.0)],
    # Shifted about 2 degrees east like the real source; the project does not correct it.
    "09020": [
        (KINMEN_ONE, "0902001", 120.33, 24.43),
        (KINMEN_TWO, "0902001", 120.34, 24.43),
        (KINMEN_TWO, "0902001", 120.34, 24.43 + TEN_M),
    ],
    "09007": [(LIENCHIANG, "0900701", 119.95, 26.15)],
}


def _source(tmp_path):
    root = tmp_path / "address"
    root.mkdir()
    area = root / "area_2014.csv"
    area.write_text(
        "name,dgbas_id\n臺北市中正區,6300100\n金門縣金城鎮,0902001\n連江縣南竿鄉,0900701\n",
        encoding="utf-8",
    )
    custom = root / "area_custom.csv"
    custom.write_text("name,dgbas_id\n臺北巿木柵區,6300800\n", encoding="utf-8")
    (root / "roads").mkdir()
    roads = []
    for county, values in SOURCE.items():
        path = root / "roads" / f"{county}-測試路.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["FULL_ADDR", "COUNTY", "TOWN", "ROAD", "X", "Y"])
            for address, town, lng, lat in values:
                writer.writerow([address, county, town, "測試路", lng, lat])
        roads.append({"path": "roads/" + path.name, "sha256": sha256_file(path)})
    git = ["git", "-C", str(root)]
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run([*git, "add", "."], check=True, capture_output=True)
    subprocess.run([*git, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "core.hooksPath=/dev/null", "commit", "-m", "fixture"],
                   check=True, capture_output=True)
    commit = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    return root, {
        "commit": commit,
        "administrative_files": [{"path": p.name, "sha256": sha256_file(p)} for p in [area, custom]],
        "roads": roads,
    }


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("r05-9")
    records = [{"address": a, "extra": [DISTRICT.get(a, "金城鎮")]} for a in ADDRESSES]
    _, _, converted = pipeline(tmp_path, records, extra_header=["鄉鎮市區"])
    root, descriptor = _source(tmp_path)
    work = tmp_path / "work"
    index = build_index(root, descriptor, work, counties=list(SOURCE))
    pool = build_pool(converted, root, descriptor, work, index_path=index)
    state = resolve_offline(pool, index, work)
    plain = resolve_offline(pool, index, tmp_path / "plain", suspended_counties=[])
    return {"tmp": tmp_path, "converted": converted, "descriptor": descriptor, "index": index,
            "pool": pool, "state": state, "plain": plain, "work": work}


def _by_address(snapshot):
    return {row["canonical_address"]: row for row in rows(snapshot / "address_index.parquet")}


def _resolutions(snapshot):
    return {row["building_key"]: row for row in rows(snapshot / "coordinate_resolutions.parquet")}


def test_default_config_suspends_penghu_and_kinmen_only():
    listed = load_suspended_counties()
    assert [(item["code"], item["name"]) for item in listed] == [("09020", "金門縣"), ("10016", "澎湖縣")]
    assert all(item["reason"].strip() for item in listed)
    assert "address_source_suspended" in EXCEPTION_REASON_CODES


def test_suspended_county_coordinates_are_not_located_and_evidence_kept(built):
    results = _by_address(built["state"])
    resolutions = _resolutions(built["state"])
    one, two, none = results[KINMEN_ONE], results[KINMEN_TWO], results[KINMEN_NONE]
    # One coordinate and two coordinates within the tolerance: both would be located.
    for row, count in [(one, 1), (two, 2)]:
        assert row["status"] == "conflict" and row["coordinate_count"] == count
        assert (row["lng"], row["lat"], row["evidence_id"]) == (None, None, None)
        detail = resolutions[row["building_key"]]
        assert detail["resolution_basis"] == SUSPENDED and detail["status"] == "conflict"
        assert (detail["lng"], detail["lat"], detail["evidence_id"]) == (None, None, None)
        assert len(json.loads(detail["coordinates_json"])) == count
    assert resolutions[one["building_key"]]["max_distance_m"] == 0.0
    assert resolutions[two["building_key"]]["max_distance_m"] == pytest.approx(10, abs=1e-3)
    listed = json.loads(resolutions[one["building_key"]]["coordinates_json"])
    assert [(p["lng"], p["lat"], p["evidence_count"]) for p in listed] == [(120.33, 24.43, 1)]
    assert none["status"] == "unmatched" and none["coordinate_count"] == 0
    # The address source coordinates stay in the evidence.
    evidence = [e for e in rows(built["state"] / "address_observations.parquet") if e["county_code"] == "09020"]
    assert sorted((e["lng"], e["lat"]) for e in evidence) == sorted(
        (lng, lat) for _, _, lng, lat in SOURCE["09020"]
    )
    unmatched = {row["canonical_address"] for row in rows(built["state"] / "unmatched_addresses.parquet")}
    assert {KINMEN_ONE, KINMEN_TWO, KINMEN_NONE} <= unmatched
    _, report = load_p2(built["state"], "offline-state")
    assert report["address_suspension"]["rule"] == "county_suspension_v1"
    assert report["address_suspension_counts"]["09020"] == {
        "keys": 3, "with_coordinates": 2, "would_be_located": 2, "without_coordinates": 1,
        "tgos_evidence_keys": 0,
    }
    assert report["address_suspension_counts"]["10016"]["keys"] == 0


def test_counties_outside_the_list_are_unchanged(built):
    suspended = _by_address(built["state"])
    plain = _by_address(built["plain"])
    kinmen = {KINMEN_ONE, KINMEN_TWO, KINMEN_NONE}
    assert set(suspended) == set(plain)
    for address in set(plain) - kinmen:
        assert suspended[address] == plain[address]
    assert suspended[LIENCHIANG]["status"] == "located"
    assert (suspended[LIENCHIANG]["lng"], suspended[LIENCHIANG]["lat"]) == (119.95, 26.15)
    # Without the list the same Kinmen keys are located: the suspension is the only cause.
    assert plain[KINMEN_ONE]["status"] == plain[KINMEN_TWO]["status"] == "located"
    assert all(r["resolution_basis"] != SUSPENDED for r in rows(built["plain"] / "coordinate_resolutions.parquet"))
    plain_keys = {k: v for k, v in _resolutions(built["plain"]).items()}
    for key, row in _resolutions(built["state"]).items():
        if row["resolution_basis"] != SUSPENDED:
            assert plain_keys[key] == row


def test_changed_list_changes_the_binding(built):
    store = SnapshotStore(built["work"] / "offline-state")
    manifest = store.verify(built["state"])
    pool, index, work = built["pool"], built["index"], built["work"]
    assert resolve_offline(pool, index, work) == built["state"]
    only_penghu = [item for item in load_suspended_counties() if item["code"] == "10016"]
    other = resolve_offline(pool, index, work, suspended_counties=only_penghu)
    assert other != built["state"]
    assert not store.reusable(other.name, manifest["bindings"])
    assert _by_address(other)[KINMEN_ONE]["status"] == "located"
    reworded = [{**item, "reason": item["reason"] + "（改寫）"} for item in load_suspended_counties()]
    assert resolve_offline(pool, index, work, suspended_counties=reworded) not in {built["state"], other}
    with pytest.raises(ValueError, match="different bindings"):
        resolve_offline(pool, index, work, suspended_counties=[], run_id=built["state"].name)


def test_config_list_is_checked(tmp_path):
    config = tmp_path / "pipeline.toml"
    config.write_text("coordinate_tolerance_m = 30.0\n", encoding="utf-8")
    assert load_suspended_counties(config) == []
    good = {"code": "10016", "name": "澎湖縣", "reason": "r"}
    with pytest.raises(ValueError, match="code and name disagree"):
        check_suspended_counties([{**good, "name": "金門縣"}])
    with pytest.raises(ValueError, match="needs a reason"):
        check_suspended_counties([{**good, "reason": " "}])
    with pytest.raises(ValueError, match="Duplicate"):
        check_suspended_counties([good, good])
    with pytest.raises(ValueError, match="exactly code, name and reason"):
        check_suspended_counties([{"code": "10016", "name": "澎湖縣"}])
    with pytest.raises(ValueError, match="list of tables"):
        check_suspended_counties({"code": "10016"})


def test_tampered_suspension_fails_validation(built):
    state = built["state"]
    manifest, report = load_p2(state, "offline-state")
    detail = _resolutions(state)[_by_address(state)[KINMEN_ONE]["building_key"]]
    with pytest.raises(ValueError, match="adoption rule"):
        validate_dataset_rows([{**detail, "lng": 120.33, "lat": 24.43}], "coordinate-resolution")
    with pytest.raises(ValueError, match="adoption rule"):
        validate_dataset_rows([{**detail, "max_distance_m": 1.0}], "coordinate-resolution")
    with pytest.raises(ValueError, match="status or basis"):
        validate_dataset_rows([{**detail, "status": "located"}], "coordinate-resolution")
    # A report whose list differs from the rows is rejected.
    changed = copy.deepcopy(report)
    changed["address_suspension"]["counties"] = []
    with pytest.raises(ValueError, match="suspension list"):
        check_suspension(state, manifest, changed)
    changed["address_suspension"]["counties"] = [
        *report["address_suspension"]["counties"], {"code": "09007", "name": "連江縣", "reason": "r"}
    ]
    with pytest.raises(ValueError, match="suspension list"):
        check_suspension(state, manifest, changed)


def _with_earlier_kinmen_query(built, prepared, work):
    """Add a Kinmen query to the prepared batch, as a batch made before R09-0 would hold."""
    from lvr_pipeline.storage.runs import digest
    from lvr_pipeline.tgos import _artifact_paths, _write_state

    manifest, _, _ = load_state(prepared)
    paths = _artifact_paths(prepared, manifest)
    batches = list(rows(paths["tgos-batch"]))
    queries = list(rows(paths["tgos-query"]))
    kinmen = _by_address(built["state"])[KINMEN_NONE]
    queries.append({**queries[0], "ordinal": len(queries) + 1,
                    "query_fingerprint": digest([kinmen["building_key"], KINMEN_NONE]),
                    "building_key": kinmen["building_key"], "address": KINMEN_NONE,
                    "county_code": "09020", "address_family": kinmen["address_family"]})
    batches[0]["address_count"] = len(queries)
    return _write_state(prepared, work, action="pre-r09-0-batch", batches=batches, queries=queries,
                        results=[], aliases=[], alias_events=[])


@pytest.fixture(scope="module")
def tgos(built):
    tmp_path, work = built["tmp"], built["tmp"] / "tgos-work"
    prepared, _ = prepare_tgos(built["state"], work, tmp_path / "exchange", limit=10,
                               exchange_date="2026-10-09")
    queries = list(rows(prepared / "tgos_queries.parquet"))
    # R09-0: the suspended county is no longer selected; the Kinmen query stands for one sent before R09-0.
    assert {q["address"] for q in queries} == {TAIPEI_TGOS}
    prepared = _with_earlier_kinmen_query(built, prepared, work)
    queries = list(rows(prepared / "tgos_queries.parquet"))
    assert {q["address"] for q in queries} == {KINMEN_NONE, TAIPEI_TGOS}
    batch_id = queries[0]["batch_id"]
    submitted = transition_batch(prepared, work, batch_id, "submitted")
    answers = {KINMEN_NONE: ("118.32", "24.43"), TAIPEI_TGOS: ("121.51", "25.01")}
    path = tmp_path / "answer.csv"
    response(path, [{"Address": q["address"], "Response_Address": q["address"],
                     "Response_X": answers[q["address"]][0], "Response_Y": answers[q["address"]][1]}
                    for q in queries])
    return import_tgos(submitted, work, batch_id, path)


def test_tgos_located_suspended_key_is_not_located(built, tgos):
    results = _by_address(tgos)
    assert results[TAIPEI_TGOS]["status"] == "located"
    kinmen = results[KINMEN_NONE]
    assert kinmen["status"] == "conflict" and kinmen["lng"] is None and kinmen["coordinate_count"] == 1
    detail = _resolutions(tgos)[kinmen["building_key"]]
    assert detail["resolution_basis"] == SUSPENDED
    assert [(p["lng"], p["lat"]) for p in json.loads(detail["coordinates_json"])] == [(118.32, 24.43)]
    tgos_rows = [e for e in rows(tgos / "address_observations.parquet") if e["source_kind"] == "tgos_result"]
    assert {(e["building_key"], e["lng"]) for e in tgos_rows} >= {(kinmen["building_key"], 118.32)}
    _, report, _ = load_state(tgos)
    assert report["address_suspension_counts"]["09020"] == {
        "keys": 3, "with_coordinates": 3, "would_be_located": 3, "without_coordinates": 0,
        "tgos_evidence_keys": 1,
    }
    # Earlier statuses of other keys stay as they were.
    before = _by_address(built["state"])
    for address in [ADDRESS, LIENCHIANG, KINMEN_ONE, KINMEN_TWO]:
        assert results[address] == before[address]


def test_review_lists_suspended_addresses_with_county_and_town(built, tgos):
    review = build_review(built["converted"], built["state"], built["tmp"] / "review-work",
                          descriptor=built["descriptor"], tgos_state=tgos, run_id="review-r05-9")
    values = list(rows(review / "exception_addresses.parquet"))
    suspended = [row for row in values if row["reason_code"] == "address_source_suspended"]
    assert {row["raw_address"] for row in suspended} == {KINMEN_ONE, KINMEN_TWO, KINMEN_NONE}
    assert len(suspended) == 9  # three addresses, one row per category
    assert all(row["county_code"] == "09020" and row["town_code"] == "0902001"
               and row["source_district"] == "金城鎮" for row in suspended)
    related = json.loads(next(r for r in suspended if r["raw_address"] == KINMEN_NONE)["related_json"])
    assert related["county_name"] == "金門縣" and related["suspension_reason"]
    assert related["status"] == "conflict" and related["coordinates"][0]["lng"] == 118.32
    kinmen_keys = {row["building_key"] for row in suspended}
    others = {row["reason_code"] for row in values if row["building_key"] in kinmen_keys}
    assert others == {"address_source_suspended"}
    cross = list(rows(review / "cross_check_addresses.parquet"))
    assert not any(row["building_key"] in kinmen_keys for row in cross)
    report = json.loads((review / "quality.json").read_text(encoding="utf-8"))
    assert report["reason_counts"]["address_source_suspended"] == 9
    assert report["address_suspension"]["rule"] == "county_suspension_v1"


def test_review_rows_outside_the_list_are_unchanged(built):
    kinmen = {KINMEN_ONE, KINMEN_TWO, KINMEN_NONE}

    def picked(state, name):
        review = build_review(built["converted"], state, built["tmp"] / name,
                              descriptor=built["descriptor"], run_id="same-id")
        return {(r["exception_id"], r["related_json"]) for r in rows(review / "exception_addresses.parquet")
                if r["raw_address"] not in kinmen}

    assert picked(built["state"], "review-suspended") == picked(built["plain"], "review-plain")
