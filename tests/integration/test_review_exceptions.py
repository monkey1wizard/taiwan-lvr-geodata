"""R05-8 exception and review table, built read-only from synthetic snapshots."""

import csv
import json
import subprocess

import pytest

from lvr_pipeline.address_pool import build_pool
from lvr_pipeline.cli import main
from lvr_pipeline.contracts.schemas import EXCEPTION_REASON_CODES
from lvr_pipeline.contracts.validate import validate_dataset_rows
from lvr_pipeline.offline.index import build_index
from lvr_pipeline.results.reconcile import load_p2, resolve_offline
from lvr_pipeline.results.review import COLUMNS, PRODUCED_CODES, build_review, exception_id
from lvr_pipeline.storage.parquet import BatchWriter, inspect_parquet, rows
from lvr_pipeline.storage.runs import sha256_file
from lvr_pipeline.tgos import import_tgos, prepare_tgos, transition_batch
from test_p1_conversion import ADDRESS, pipeline
from test_p2_offline import source

VARIANT = "臺北市中正區測試路10之1號"  # ADDRESS is 測試路10號之1
CONFLICT = "臺北市中正區測試路30號"
UNMATCHED = "臺北市中正區測試路11號"
NO_ADMIN = "測試路5號"
GARBLED = "臺北市中正區測路12號"
RANGE = "臺北市中正區測試路1-3號"  # county and town parse, the door range yields no key
INCOMPLETE = "臺北市中正區測試路13號"  # TGOS answers with an address that has no door
FAR = "臺北市中正區測試路14號"  # TGOS answers with a coordinate outside Taiwan
TGOS_FOUND = "臺北市中正區測試路15號"  # TGOS locates it, so it is no longer unmatched
TGOS_ANSWERS = {
    UNMATCHED: ("臺北市中正區測試路99號", "121.51", "25.01"),
    INCOMPLETE: ("臺北市中正區測試路", "121.52", "25.02"),
    FAR: (FAR, "10", "10"),
    TGOS_FOUND: (TGOS_FOUND, "121.53", "25.03"),
}


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _two_road_source(tmp_path):
    root, descriptor = source(
        tmp_path,
        [
            (ADDRESS, "63", "6300100", 121.5, 25.0),
            (CONFLICT, "63", "6300100", 121.5, 25.0),
            (CONFLICT, "63", "6300100", 121.6, 25.1),
        ],
    )
    other = root / "roads" / "63-測驗路.csv"
    with other.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["FULL_ADDR", "COUNTY", "TOWN", "ROAD", "X", "Y"])
        writer.writerow(["臺北市中正區測驗路1號", "63", "6300100", "測驗路", 121.4, 25.2])
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "core.hooksPath=/dev/null", "commit", "-m", "second road")
    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    descriptor = {**descriptor, "commit": commit,
                  "roads": [*descriptor["roads"], {"path": "roads/" + other.name, "sha256": sha256_file(other)}]}
    return root, descriptor


def _tree_hashes(directory):
    return {p.relative_to(directory).as_posix(): sha256_file(p) for p in sorted(directory.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("review")
    records = [{"address": value, "date": "1150102", "extra": ["中正區"]}
               for value in [ADDRESS, VARIANT, CONFLICT, UNMATCHED, NO_ADMIN, GARBLED, RANGE,
                             INCOMPLETE, FAR, TGOS_FOUND]]
    _, _, converted = pipeline(tmp_path, records, extra_header=["鄉鎮市區"])
    root, descriptor = _two_road_source(tmp_path)
    work = tmp_path / "work"
    index = build_index(root, descriptor, work, counties=["63"])
    pool = build_pool(converted, root, descriptor, work, index_path=index)
    state = resolve_offline(pool, index, work)
    prepared, _ = prepare_tgos(state, work, tmp_path / "exchange", limit=10, exchange_date="2026-10-08")
    queries = list(rows(prepared / "tgos_queries.parquet"))
    batch_id = queries[0]["batch_id"]
    submitted = transition_batch(prepared, work, batch_id, "submitted")
    response = tmp_path / "response.csv"
    with response.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["Address", "Response_Address", "Response_X", "Response_Y"])
        writer.writeheader()
        for query in queries:
            answer, x, y = TGOS_ANSWERS.get(query["address"], ("", "", ""))
            writer.writerow({"Address": query["address"], "Response_Address": answer,
                             "Response_X": x, "Response_Y": y})
    tgos_state = import_tgos(submitted, work, batch_id, response)
    before = {name: _tree_hashes(path) for name, path in
              [("converted", converted), ("state", state), ("tgos", tgos_state)]}
    review = build_review(converted, state, tmp_path / "review-work", descriptor=descriptor,
                          tgos_state=tgos_state, run_id="review-a")
    return {"tmp": tmp_path, "converted": converted, "state": state, "tgos": tgos_state,
            "descriptor": descriptor, "review": review, "before": before,
            "rows": list(rows(review / "exception_addresses.parquet"))}


def _by_reason(values, code):
    return [row for row in values if row["reason_code"] == code]


def test_each_produced_reason_code_has_rows(built):
    found = {row["reason_code"] for row in built["rows"]}
    assert found == set(PRODUCED_CODES)
    assert set(PRODUCED_CODES) <= set(EXCEPTION_REASON_CODES)
    assert {"district_missing", "district_ambiguous", "road_only_not_unique"}.isdisjoint(found)
    report = json.loads((built["review"] / "quality.json").read_text(encoding="utf-8"))
    assert report["address_status_source"] == "tgos-state"
    report = json.loads((built["review"] / "quality.json").read_text(encoding="utf-8"))
    assert report["reason_counts"]["district_missing"] == 0
    assert sum(report["reason_counts"].values()) == len(built["rows"])
    # Each synthetic address appears once per category (sales, presale, rent).
    invalid = _by_reason(built["rows"], "invalid_admin")
    assert {row["raw_address"] for row in invalid} == {NO_ADMIN} and len(invalid) == 3
    assert all(row["county_code"] is None and row["town_code"] is None and row["source_district"] == "中正區"
               for row in invalid)
    assert {row["category"] for row in invalid} == {"sales", "presale", "rent"}
    assert {row["src_batch"] for row in invalid} == {"115q1"}
    garbled = _by_reason(built["rows"], "garbled_pending")
    assert {row["raw_address"] for row in garbled} == {GARBLED}
    assert json.loads(garbled[0]["related_json"]) == {"candidate_roads": ["測試路", "測驗路"], "candidate_count": 2}
    conflict = _by_reason(built["rows"], "coordinate_conflict")
    assert {row["raw_address"] for row in conflict} == {CONFLICT}
    points = json.loads(conflict[0]["related_json"])["coordinates"]
    assert [(p["lng"], p["lat"]) for p in points] == [(121.5, 25.0), (121.6, 25.1)]
    # Final status comes from the TGOS state: TGOS_FOUND was located there.
    unmatched = _by_reason(built["rows"], "offline_unmatched")
    assert {row["raw_address"] for row in unmatched} == {UNMATCHED, VARIANT, INCOMPLETE, FAR}
    review = _by_reason(built["rows"], "address_review")
    assert {row["raw_address"] for row in review} == {RANGE} and len(review) == 3
    assert all(row["building_key"] is None and row["town_code"] == "6300100" for row in review)
    incomplete = _by_reason(built["rows"], "tgos_response_incomplete")
    assert {row["raw_address"] for row in incomplete} == {INCOMPLETE}
    assert json.loads(incomplete[0]["related_json"])["reason"] == "TGOS response is not a complete address"
    far = _by_reason(built["rows"], "tgos_coordinate_invalid")
    assert {row["raw_address"] for row in far} == {FAR}
    assert json.loads(far[0]["related_json"])["reason"] == "TGOS coordinate is outside declared WGS84 Taiwan bounds"
    isolated = _by_reason(built["rows"], "tgos_isolated")
    assert {row["raw_address"] for row in isolated} == {UNMATCHED} and len(isolated) == 3
    detail = json.loads(isolated[0]["related_json"])
    assert detail["submitted_address"] == UNMATCHED and detail["response_address"] == "臺北市中正區測試路99號"
    assert detail["reason"] == "TGOS response address differs from submitted address"


def test_coordinate_conflict_lists_largest_distance(built):
    # R05-4: only keys whose coordinates are more than 30 m apart stay a conflict.
    from lvr_pipeline.results.reconcile import haversine_m

    conflict = _by_reason(built["rows"], "coordinate_conflict")
    related = json.loads(conflict[0]["related_json"])
    expected = haversine_m((121.5, 25.0), (121.6, 25.1))
    assert related["max_distance_m"] == pytest.approx(expected, rel=1e-9)
    assert related["max_distance_m"] > 30


def test_without_tgos_state_offline_status_is_used(built):
    review = build_review(built["converted"], built["state"], built["tmp"] / "review-offline",
                          descriptor=built["descriptor"], run_id="review-offline")
    values = list(rows(review / "exception_addresses.parquet"))
    unmatched = {row["raw_address"] for row in values if row["reason_code"] == "offline_unmatched"}
    assert TGOS_FOUND in unmatched
    assert not any(row["reason_code"].startswith("tgos_") for row in values)
    report = json.loads((review / "quality.json").read_text(encoding="utf-8"))
    assert report["address_status_source"] == "offline-state"


def test_subdoor_variants_point_at_each_other_and_keep_their_keys(built):
    pairs = _by_reason(built["rows"], "subdoor_variant_pair")
    keys = {row["building_key"] for row in pairs}
    assert len(keys) == 2 and len(pairs) == 6
    for row in pairs:
        related = json.loads(row["related_json"])
        assert related["pair_building_key"] in keys - {row["building_key"]}
    addresses = {row["raw_address"]: json.loads(row["related_json"])["pair_address"] for row in pairs}
    assert addresses == {ADDRESS: VARIANT, VARIANT: ADDRESS}


def test_csv_has_bom_and_matches_parquet(built):
    path = built["review"] / "exception_addresses.csv"
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == COLUMNS
        values = list(reader)
    assert [row["exception_id"] for row in values] == [row["exception_id"] for row in built["rows"]]
    assert values[0]["raw_address"] == built["rows"][0]["raw_address"]


def test_exception_ids_recompute_and_first_run_is_kept(built):
    for row in built["rows"]:
        if row["reason_code"] == "subdoor_variant_pair":
            extra = json.loads(row["related_json"])["pair_building_key"]
        elif row["reason_code"].startswith("tgos_"):
            extra = json.loads(row["related_json"])["result_id"]
        else:
            extra = ""
        assert row["exception_id"] == exception_id(row["reason_code"], row["component_id"], extra)
    assert {row["first_run_id"] for row in built["rows"]} == {"review-a"}
    again = build_review(built["converted"], built["state"], built["tmp"] / "review-next",
                         descriptor=built["descriptor"], tgos_state=built["tgos"],
                         prior_review=built["review"], run_id="review-b")
    second = list(rows(again / "exception_addresses.parquet"))
    assert [row["exception_id"] for row in second] == [row["exception_id"] for row in built["rows"]]
    assert {row["first_run_id"] for row in second} == {"review-a"}


def test_schema_validation_rejects_bad_rows(built, tmp_path):
    good = dict(built["rows"][0])
    with pytest.raises(ValueError, match="reason code"):
        validate_dataset_rows([{**good, "reason_code": "guessed"}], "exception-address")
    with pytest.raises(ValueError):
        validate_dataset_rows([{**good, "related_json": "{not json"}], "exception-address")
    with pytest.raises(ValueError, match="identifier"):
        validate_dataset_rows([{**good, "exception_id": "x"}], "exception-address")
    writer = BatchWriter(tmp_path / "bad.parquet", "exception-address")
    writer.add({**good, "reason_code": "guessed"})
    writer.close()
    with pytest.raises(ValueError, match="reason code"):
        inspect_parquet(tmp_path / "bad.parquet", "exception-address")


def test_existing_snapshots_are_unchanged(built):
    after = {name: _tree_hashes(path) for name, path in
             [("converted", built["converted"]), ("state", built["state"]), ("tgos", built["tgos"])]}
    assert after == built["before"]
    load_p2(built["state"], "offline-state")
    assert load_p2(built["review"], "review")[1]["dataset_counts"] == {"exception-address": len(built["rows"])}


def test_cli_build_review(built, tmp_path, capsys):
    descriptor = tmp_path / "address_source.json"
    descriptor.write_text(json.dumps(built["descriptor"], ensure_ascii=False), encoding="utf-8")
    code = main(["build-review", "--input", str(built["converted"]), "--state", str(built["state"]),
                 "--tgos-state", str(built["tgos"]), "--address-source", str(descriptor),
                 "--work-dir", str(tmp_path / "cli"), "--run-id", "review-cli"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0 and output["completed"] is True
    assert output["reason_counts"]["tgos_isolated"] == 3
    path = tmp_path / "cli" / "review" / "snapshots" / "review-cli" / "exception_addresses.parquet"
    assert [row["exception_id"] for row in rows(path)] == [row["exception_id"] for row in built["rows"]]
