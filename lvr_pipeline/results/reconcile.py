"""Resolve verified coordinates per building key; retain conflicts and all unresolved keys.

Coordinate adoption rule (R05-4, owner decision 2026-10-08):

- One distinct coordinate for a key: located at that coordinate.
- Several distinct coordinates: compute the haversine distance of every pair
  (same formula and radius as ``offline.index._haversine_sql``). If every
  pair is at most the tolerance (equal counts as within), the key is located
  at the medoid: the source coordinate with the smallest sum of distances to
  the others, ties broken by the smallest (longitude, latitude). The medoid is
  always a coordinate that exists in the source; coordinates are never
  averaged and the first row is never taken.
- Any pair above the tolerance: conflict, not located.

The tolerance comes from ``coordinate_tolerance_m`` in the pipeline config and
is part of the stage bindings, so changing it makes the stage not reusable.
"""

from pathlib import Path
import json
import math
import tempfile
import tomllib

import duckdb

from ..offline.index import EARTH_RADIUS_M
from ..storage.parquet import BatchWriter, duckdb_config
from ..storage.runs import ROOT, Stage, bindings, digest
from ..storage.runs import SnapshotStore
from ..storage.runs import sha256_file

DEFAULT_CONFIG = ROOT / "config" / "pipeline.example.toml"
WITHIN_TOLERANCE = "within_tolerance_medoid"
EXCEEDS_TOLERANCE = "exceeds_tolerance"


def load_coordinate_tolerance(config: Path | None = None) -> float:
    """Read ``coordinate_tolerance_m`` (metres) from the pipeline TOML config."""
    path = Path(config) if config is not None else DEFAULT_CONFIG
    with path.open("rb") as stream:
        value = tomllib.load(stream).get("coordinate_tolerance_m")
    return check_tolerance(value)


def check_tolerance(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("coordinate_tolerance_m must be a number of metres")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("coordinate_tolerance_m must be finite and not negative")
    return value


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance in metres between (lng, lat) points; same formula as ``_haversine_sql``."""
    lng_a, lat_a = a
    lng_b, lat_b = b
    h = math.sin(math.radians(lat_b - lat_a) / 2) ** 2 + math.cos(
        math.radians(lat_a)
    ) * math.cos(math.radians(lat_b)) * math.sin(math.radians(lng_b - lng_a) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))


def adopt_coordinates(points, tolerance_m: float | None) -> dict:
    """Apply the adoption rule to the distinct coordinates of one key.

    ``points`` is an iterable of at least two distinct (lng, lat) pairs.
    ``tolerance_m=None`` is the rule before R05-4 (any second coordinate is a
    conflict); it is used only for states built before R05-4. Returns status,
    basis, representative point, largest pairwise distance and the distance
    sum of every coordinate, ordered by (lng, lat).
    """
    coordinates = sorted(points)
    if len(coordinates) < 2 or len(set(coordinates)) != len(coordinates):
        raise ValueError("Adoption rule needs at least two distinct coordinates")
    sums = {point: [] for point in coordinates}
    largest = 0.0
    for i, a in enumerate(coordinates):
        for b in coordinates[i + 1 :]:
            distance = haversine_m(a, b)
            sums[a].append(distance)
            sums[b].append(distance)
            largest = max(largest, distance)
    # fsum is exactly rounded, so ties do not depend on summation order.
    totals = {point: math.fsum(values) for point, values in sums.items()}
    within = tolerance_m is not None and largest <= tolerance_m
    representative = (
        min(coordinates, key=lambda point: (totals[point], point)) if within else None
    )
    return {
        "status": "located" if within else "conflict",
        "resolution_basis": WITHIN_TOLERANCE if within else EXCEEDS_TOLERANCE,
        "representative": representative,
        "max_distance_m": largest,
        "distance_sums": [(point, totals[point]) for point in coordinates],
    }


def resolution_row(key_version, building_key, points: dict, tolerance_m: float) -> dict:
    """The coordinate-resolution row of a key with several distinct coordinates.

    ``points`` maps (lng, lat) to {"evidence_count": rows, "evidence_id": smallest id}.
    """
    adoption = adopt_coordinates(points, tolerance_m)
    point = adoption["representative"]
    # The medoid's own (lng, lat); None when the key is a conflict.
    representative_lng, representative_lat = point if point else (None, None)
    return {
        "key_version": key_version,
        "building_key": building_key,
        "status": adoption["status"],
        "resolution_basis": adoption["resolution_basis"],
        "tolerance_m": tolerance_m,
        "max_distance_m": adoption["max_distance_m"],
        "coordinate_count": len(points),
        "evidence_count": sum(v["evidence_count"] for v in points.values()),
        "lng": representative_lng,
        "lat": representative_lat,
        "evidence_id": points[point]["evidence_id"] if point else None,
        "coordinates_json": json.dumps(
            [
                {
                    "lng": lng,
                    "lat": lat,
                    "evidence_count": points[(lng, lat)]["evidence_count"],
                    "evidence_id": points[(lng, lat)]["evidence_id"],
                    "distance_sum_m": total,
                }
                for (lng, lat), total in adoption["distance_sums"]
            ],
            separators=(",", ":"),
        ),
    }


def load_p2(path: Path, kind: str):
    path = Path(path)
    manifest = SnapshotStore(path.parent.parent).verify(path)
    report = json.loads((path / "quality.json").read_text(encoding="utf-8"))
    if (
        report["stage"] != kind
        or digest(report["producer_config"]) != manifest["bindings"]["config_sha256"]
    ):
        raise ValueError("Offline stage or producer config mismatch")
    totals = {}
    for item in manifest["artifacts"]:
        if item["schema"]:
            totals[item["schema"]] = totals.get(item["schema"], 0) + item["row_count"]
    if totals != report["dataset_counts"]:
        raise ValueError("Offline dataset counts differ")
    return manifest, report


def resolve_offline(
    pool_path: Path,
    index_path: Path,
    work_dir: Path,
    *,
    prior_state: Path | None = None,
    coordinate_tolerance_m: float | None = None,
    run_id=None,
):
    """Build the offline state; ``coordinate_tolerance_m=None`` reads the pipeline config."""
    tolerance = (
        load_coordinate_tolerance()
        if coordinate_tolerance_m is None
        else check_tolerance(coordinate_tolerance_m)
    )
    pool_manifest, pool_report = load_p2(pool_path, "address-pool")
    index_manifest, index_report = load_p2(index_path, "offline-index")
    if pool_report["address_source_commit"] != index_report["address_source_commit"]:
        raise ValueError("Address pool/index pin mismatch")
    hashes = [
        sha256_file(Path(pool_path) / "manifest.json"),
        sha256_file(Path(index_path) / "manifest.json"),
    ]
    if prior_state:
        _, prior = load_p2(prior_state, "offline-state")
        if prior["tgos_started"]:
            raise ValueError(
                "P3 ledger reconciliation required; offline-only code cannot reset TGOS state"
            )
        hashes.append(sha256_file(Path(prior_state) / "manifest.json"))
    binding = bindings(
        "offline-state",
        {
            "coordinate_equality": "exact_float64",
            "coordinate_rule": "pairwise_haversine_medoid",
            "coordinate_tolerance_m": tolerance,
            "earth_radius_m": EARTH_RADIUS_M,
            "prior_state": bool(prior_state),
        },
        hashes,
    )
    stage = Stage(Path(work_dir) / "offline-state", "offline-state", binding, run_id)
    if stage.reused:
        return stage.path
    result = BatchWriter(stage.build / "address_index.parquet", "address-result")
    unmatched = BatchWriter(
        stage.build / "unmatched_addresses.parquet", "unmatched-address"
    )
    evidence = BatchWriter(stage.build / "address_observations.parquet", "offline-row")
    resolutions = BatchWriter(
        stage.build / "coordinate_resolutions.parquet", "coordinate-resolution"
    )
    statuses = {
        name: 0 for name in ["located", "conflict", "unmatched", "outside_scope"]
    }
    basis_counts = {WITHIN_TOLERANCE: 0, EXCEEDS_TOLERANCE: 0}
    with tempfile.TemporaryDirectory(prefix="state-", dir=stage.build) as spill:
        with duckdb.connect(
            config=duckdb_config(spill)
        ) as db:
            db.read_parquet(
                str(Path(pool_path) / "unique_addresses.parquet")
            ).create_view("pool")
            db.read_parquet(str(Path(index_path) / "offline_rows.parquet")).create_view(
                "evidence"
            )
            cursor = db.execute(
                "SELECT e.* FROM evidence e JOIN pool p USING(building_key) ORDER BY e.evidence_id"
            )
            # Column names of the cursor, not a row choice.
            columns = [x[0] for x in cursor.description]
            while batch := cursor.fetchmany(1024):
                for values in batch:
                    evidence.add(dict(zip(columns, values)))
            evidence.close()
            query = """WITH coordinates AS (
      SELECT building_key,lng,lat,min(evidence_id) evidence_id,count(*) evidence_count
      FROM evidence WHERE validity='valid' GROUP BY building_key,lng,lat),
     summaries AS (SELECT building_key,count(*) coordinate_count,sum(evidence_count) evidence_count,
       list(struct_pack(lng:=lng,lat:=lat,n:=evidence_count,eid:=evidence_id) ORDER BY lng,lat) points
       FROM coordinates GROUP BY building_key)
     SELECT p.key_version,p.building_key,p.canonical_address,p.county_code,p.town_code,p.address_family,
       coalesce(s.coordinate_count,0),coalesce(s.evidence_count,0),s.points
     FROM pool p LEFT JOIN summaries s USING(building_key) ORDER BY p.building_key"""
            cursor = db.execute(query)
            while batch := cursor.fetchmany(1024):
                for (
                    version,
                    key,
                    address,
                    county,
                    town,
                    fam,
                    count,
                    n,
                    points,
                ) in batch:
                    lng = lat = eid = None
                    if count == 1:
                        # The only distinct coordinate, not a first-row pick.
                        (only,) = points
                        status = "located"
                        lng, lat, eid = only["lng"], only["lat"], only["eid"]
                    elif count > 1:
                        detail = resolution_row(
                            version,
                            key,
                            {
                                (p["lng"], p["lat"]): {
                                    "evidence_count": p["n"],
                                    "evidence_id": p["eid"],
                                }
                                for p in points
                            },
                            tolerance,
                        )
                        resolutions.add(detail)
                        basis_counts[detail["resolution_basis"]] += 1
                        status = detail["status"]
                        lng, lat, eid = detail["lng"], detail["lat"], detail["evidence_id"]
                    else:
                        status = (
                            "unmatched"
                            if county in index_report["address_counties"]
                            else "outside_scope"
                        )
                    statuses[status] += 1
                    row = {
                        "key_version": version,
                        "building_key": key,
                        "canonical_address": address,
                        "county_code": county,
                        "town_code": town,
                        "address_family": fam,
                        "status": status,
                        "lng": lng,
                        "lat": lat,
                        "evidence_id": eid,
                        "evidence_count": n,
                        "coordinate_count": count,
                        "source_commit": index_report["address_source_commit"],
                    }
                    result.add(row)
                    if status != "located":
                        unmatched.add(row)
    result.close()
    unmatched.close()
    resolutions.close()
    aliases = BatchWriter(stage.build / "verified_aliases.parquet", "verified-alias")
    aliases.close()
    ledger = BatchWriter(stage.build / "tgos_results.parquet", "tgos-ledger")
    ledger.close()
    report = {
        **pool_report,
        "index_manifest_sha256": hashes[1],
        "status_counts": statuses,
        "coordinate_rule": "pairwise_haversine_medoid",
        "coordinate_tolerance_m": tolerance,
        "coordinate_resolution_basis_counts": basis_counts,
        "tgos_started": False,
        "offline_source": index_report,
        "dataset_counts": {
            "address-result": result.row_count,
            "offline-row": evidence.row_count,
            "verified-alias": 0,
            "tgos-ledger": 0,
            "unmatched-address": unmatched.row_count,
            "coordinate-resolution": resolutions.row_count,
            "address-pool": pool_report["pool_rows"],
            "address-occurrence": pool_report["occurrence_rows"],
        },
    }
    artifacts = [
        ("address_index.parquet", result.path, "address-result", result.row_count),
        (
            "address_observations.parquet",
            evidence.path,
            "offline-row",
            evidence.row_count,
        ),
        (
            "unmatched_addresses.parquet",
            unmatched.path,
            "unmatched-address",
            unmatched.row_count,
        ),
        (
            "coordinate_resolutions.parquet",
            resolutions.path,
            "coordinate-resolution",
            resolutions.row_count,
        ),
        ("verified_aliases.parquet", aliases.path, "verified-alias", 0),
        ("tgos_results.parquet", ledger.path, "tgos-ledger", 0),
        (
            "unique_addresses.parquet",
            Path(pool_path) / "unique_addresses.parquet",
            "address-pool",
            pool_report["pool_rows"],
        ),
        (
            "address_occurrences.parquet",
            Path(pool_path) / "address_occurrences.parquet",
            "address-occurrence",
            pool_report["occurrence_rows"],
        ),
    ]
    return stage.finish(artifacts, report)
