"""R05-4 coordinate adoption: 30 m pairwise haversine tolerance and medoid representative."""

import json
import math

import duckdb
import pytest

from lvr_pipeline.contracts.validate import validate_dataset_rows
from lvr_pipeline.offline.index import EARTH_RADIUS_M, _haversine_sql
from lvr_pipeline.results.reconcile import (
    EXCEEDS_TOLERANCE,
    WITHIN_TOLERANCE,
    adopt_coordinates,
    haversine_m,
    load_coordinate_tolerance,
    load_p2,
    resolve_offline,
)
from lvr_pipeline.storage.parquet import rows, verify_relations
from lvr_pipeline.storage.runs import SnapshotStore
from lvr_pipeline.tgos import import_tgos, load_state, prepare_tgos, transition_batch
from test_p1_conversion import ADDRESS
from test_p2_offline import run
from test_p3_tgos import response

TOLERANCE = 30.0
# One metre of latitude in degrees on the audit sphere.
LAT_DEGREE_PER_M = 180 / (math.pi * EARTH_RADIUS_M)


def north(point, metres):
    return point[0], point[1] + metres * LAT_DEGREE_PER_M


def test_config_tolerance_is_thirty_metres():
    assert load_coordinate_tolerance() == TOLERANCE


def test_haversine_matches_audit_sql_formula():
    a, b = (121.5, 25.0), (121.5003, 25.0002)
    with duckdb.connect() as db:
        db.execute("CREATE TABLE p (lng DOUBLE, lat DOUBLE, side VARCHAR)")
        db.execute("INSERT INTO p VALUES (?, ?, 'a'), (?, ?, 'b')", [*a, *b])
        expected = db.sql(
            "SELECT " + _haversine_sql("a", "b") + " FROM (SELECT * FROM p WHERE side='a') a,"
            " (SELECT * FROM p WHERE side='b') b"
        ).fetchone()[0]
    assert haversine_m(a, b) == pytest.approx(expected, rel=1e-12)
    assert haversine_m(a, b) == haversine_m(b, a)


def test_within_tolerance_located_at_medoid_of_source_points():
    a = (121.5, 25.0)
    points = [north(a, 20), a, north(a, 10)]
    adoption = adopt_coordinates(points, TOLERANCE)
    assert adoption["status"] == "located"
    assert adoption["resolution_basis"] == WITHIN_TOLERANCE
    # The middle point has the smallest distance sum and is a real source point.
    assert adoption["representative"] == north(a, 10)
    assert adoption["representative"] in points
    assert adoption["max_distance_m"] == pytest.approx(20, abs=1e-6)


def test_distance_equal_to_tolerance_is_within():
    a, b = (121.5, 25.0), north((121.5, 25.0), 30)
    distance = haversine_m(a, b)
    assert distance == pytest.approx(30, abs=1e-6)
    assert adopt_coordinates([a, b], distance)["status"] == "located"
    assert adopt_coordinates([a, b], math.nextafter(distance, 0))["status"] == "conflict"


def test_just_over_tolerance_conflicts():
    a = (121.5, 25.0)
    inside = adopt_coordinates([a, north(a, 29.99)], TOLERANCE)
    outside = adopt_coordinates([a, north(a, 30.01)], TOLERANCE)
    assert inside["status"] == "located"
    assert outside["status"] == "conflict" and outside["representative"] is None
    assert outside["resolution_basis"] == EXCEEDS_TOLERANCE
    assert outside["max_distance_m"] == pytest.approx(30.01, abs=1e-6)


def test_one_far_point_of_three_makes_the_key_a_conflict():
    a = (121.5, 25.0)
    adoption = adopt_coordinates([a, north(a, 5), north(a, 100)], TOLERANCE)
    assert adoption["status"] == "conflict" and adoption["representative"] is None
    assert adoption["max_distance_m"] == pytest.approx(100, abs=1e-6)


def test_medoid_tie_takes_smallest_longitude_then_latitude():
    # Two points always tie; the smaller (lng, lat) wins whatever the input order.
    east, west = (121.50001, 25.0), (121.5, 25.0)
    assert adopt_coordinates([east, west], TOLERANCE)["representative"] == west
    same_lng = [(121.5, 25.00002), (121.5, 25.0)]
    assert adopt_coordinates(same_lng, TOLERANCE)["representative"] == (121.5, 25.0)
    # A strictly smaller distance sum beats a smaller (lng, lat).
    points = [(121.5, 25.0), (121.5, 25.0001), (121.5, 25.0002)]
    assert adopt_coordinates(points, TOLERANCE)["representative"] == (121.5, 25.0001)


def test_legacy_rule_without_tolerance_is_always_conflict():
    a = (121.5, 25.0)
    assert adopt_coordinates([a, north(a, 1)], None)["status"] == "conflict"
    with pytest.raises(ValueError, match="two distinct"):
        adopt_coordinates([a], TOLERANCE)


def near_state(tmp_path, metres=10.0, **kwargs):
    a = (121.5, 25.0)
    b = north(a, metres)
    coordinates = [
        (ADDRESS, "63", "6300100", *b),
        (ADDRESS, "63", "6300100", *a),
        (ADDRESS, "63", "6300100", *a),
    ]
    return run(tmp_path, coordinates, **kwargs), a, b


def test_single_coordinate_key_is_unchanged(tmp_path):
    *_, state = run(tmp_path)
    _, report = load_p2(state, "offline-state")
    [result] = rows(state / "address_index.parquet")
    assert result["status"] == "located" and result["coordinate_count"] == 1
    assert (result["lng"], result["lat"]) == (121.5, 25.0)
    assert not list(rows(state / "coordinate_resolutions.parquet"))
    assert report["coordinate_tolerance_m"] == TOLERANCE
    assert report["dataset_counts"]["coordinate-resolution"] == 0


def test_close_coordinates_located_and_all_sources_kept(tmp_path):
    (converted, root, d, index, pool, state), a, b = near_state(tmp_path)
    _, report = load_p2(state, "offline-state")
    [result] = rows(state / "address_index.parquet")
    assert result["status"] == "located"
    assert result["coordinate_count"] == 2 and result["evidence_count"] == 3
    # Both points have one distance; the tie goes to the smaller (lng, lat).
    assert (result["lng"], result["lat"]) == min(a, b)
    assert not list(rows(state / "unmatched_addresses.parquet"))
    [detail] = rows(state / "coordinate_resolutions.parquet")
    assert detail["resolution_basis"] == WITHIN_TOLERANCE
    assert detail["tolerance_m"] == TOLERANCE
    assert detail["max_distance_m"] == pytest.approx(10, abs=1e-6)
    listed = json.loads(detail["coordinates_json"])
    assert [(p["lng"], p["lat"], p["evidence_count"]) for p in listed] == [
        (*a, 2),
        (*b, 1),
    ]
    assert detail["evidence_id"] == result["evidence_id"]
    evidence = list(rows(state / "address_observations.parquet"))
    assert len(evidence) == 3
    assert {(e["lng"], e["lat"]) for e in evidence} == {a, b}
    assert report["status_counts"]["located"] == 1
    assert report["coordinate_resolution_basis_counts"] == {
        WITHIN_TOLERANCE: 1,
        EXCEEDS_TOLERANCE: 0,
    }


def test_far_coordinates_conflict_with_distance_recorded(tmp_path):
    (*_, state), a, b = near_state(tmp_path, metres=30.5)
    [result] = rows(state / "address_index.parquet")
    assert result["status"] == "conflict" and result["lng"] is None
    assert result["evidence_id"] is None
    [detail] = rows(state / "coordinate_resolutions.parquet")
    assert detail["resolution_basis"] == EXCEEDS_TOLERANCE
    assert detail["max_distance_m"] == pytest.approx(30.5, abs=1e-6)
    assert list(rows(state / "unmatched_addresses.parquet")) == [result]


def test_changed_tolerance_makes_the_stage_not_reusable(tmp_path):
    (converted, root, d, index, pool, state), *_ = near_state(tmp_path)
    work = tmp_path / "work"
    store = SnapshotStore(work / "offline-state")
    manifest = store.verify(state)
    assert resolve_offline(pool, index, work, coordinate_tolerance_m=TOLERANCE) == state
    strict = resolve_offline(pool, index, work, coordinate_tolerance_m=5.0)
    assert strict != state
    assert not store.reusable(strict.name, manifest["bindings"])
    [result] = rows(strict / "address_index.parquet")
    assert result["status"] == "conflict"
    with pytest.raises(ValueError, match="different bindings"):
        resolve_offline(
            pool, index, work, coordinate_tolerance_m=10.0, run_id=state.name
        )


def test_invalid_tolerance_is_rejected(tmp_path):
    config = tmp_path / "pipeline.toml"
    config.write_text("coordinate_tolerance_m = -1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not negative"):
        load_coordinate_tolerance(config)
    config.write_text("other = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="number of metres"):
        load_coordinate_tolerance(config)


def test_tampered_or_legacy_resolution_fails_validation(tmp_path):
    (*_, state), a, b = near_state(tmp_path)
    [detail] = rows(state / "coordinate_resolutions.parquet")
    other = max(a, b)
    with pytest.raises(ValueError, match="adoption rule"):
        validate_dataset_rows(
            [{**detail, "lng": other[0], "lat": other[1]}], "coordinate-resolution"
        )
    with pytest.raises(ValueError, match="adoption rule"):
        validate_dataset_rows([{**detail, "tolerance_m": 1.0}], "coordinate-resolution")
    # Without adoption rows (a state before R05-4) several coordinates cannot be located.
    with pytest.raises(ValueError, match="needs coordinate resolution"):
        verify_relations({"address-result": [state / "address_index.parquet"]})
    verify_relations(
        {
            "address-result": [state / "address_index.parquet"],
            "coordinate-resolution": [state / "coordinate_resolutions.parquet"],
            "offline-row": [state / "address_observations.parquet"],
        }
    )


def test_tgos_import_keeps_the_tolerance_rule(tmp_path):
    other = "臺北市中正區測試路11號"
    (_, _, _, _, _, state), a, b = near_state(
        tmp_path, records=[{"address": ADDRESS}, {"address": other}]
    )
    prepared, _ = prepare_tgos(state, tmp_path / "work", tmp_path / "exchange", limit=1)
    batch_id = list(rows(prepared / "tgos_batches.parquet"))[0]["batch_id"]
    submitted = transition_batch(prepared, tmp_path / "work", batch_id, "submitted")
    result = tmp_path / "result.csv"
    response(
        result,
        [{"Address": other, "Response_Address": other, "Response_X": "121.51", "Response_Y": "25.01"}],
    )
    imported = import_tgos(submitted, tmp_path / "work", batch_id, result)
    _, report, _ = load_state(imported)
    assert report["status_counts"]["located"] == 2
    assert report["coordinate_tolerance_m"] == TOLERANCE
    located = {
        r["canonical_address"]: r for r in rows(imported / "address_index.parquet")
    }
    assert (located[ADDRESS]["lng"], located[ADDRESS]["lat"]) == min(a, b)
    [detail] = rows(imported / "coordinate_resolutions.parquet")
    assert detail["status"] == "located"
