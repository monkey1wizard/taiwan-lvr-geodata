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

Door identity (R04-12, owner decision 2026-10-09): the candidates of a pool key
are the address rows with the same door signature (county, town, street or
place, door) whose village and neighbourhood agree with the ones the pool key
writes. Each distinct (village, neighbourhood) among the candidates is one door.
One distinct coordinate is located even when it comes from several doors. Several
distinct coordinates from one door follow the tolerance rule above. Several
distinct coordinates from several doors are not located (status ``conflict``,
basis ``door_cross_check``); the tolerance is never applied between doors, and
the key goes to the manual cross-check list of the review stage.

The tolerance comes from ``coordinate_tolerance_m`` in the pipeline config and
is part of the stage bindings, so changing it makes the stage not reusable.

County suspension (R05-9, owner decision 2026-10-09, option B): the pipeline
config lists counties whose address source coordinates are not used
(``suspended_counties``). A key of such a county with one or more candidate
coordinates is never located, whatever the coordinates are: status
``conflict``, basis ``address_source_suspended``, no representative point. Its
coordinates stay in the evidence and in the coordinate-resolution row. Keys
without coordinates keep their status. The list is part of the stage bindings.
"""

from pathlib import Path
import json
import math
import tempfile
import tomllib

import duckdb

from ..addresses.identity import door_signature_sql, sub_identity_sql
from ..contracts.schemas import dataset_schema
from ..offline.index import EARTH_RADIUS_M
from ..storage.parquet import BatchWriter, duckdb_config
from ..storage.runs import ROOT, Stage, bindings, digest
from ..storage.runs import SnapshotStore
from ..storage.runs import sha256_file

DEFAULT_CONFIG = ROOT / "config" / "pipeline.example.toml"
WITHIN_TOLERANCE = "within_tolerance_medoid"
EXCEEDS_TOLERANCE = "exceeds_tolerance"
DOOR_CROSS_CHECK = "door_cross_check"
DOOR_RULE = "village_neighborhood_subid_v1"
SUSPENDED = "address_source_suspended"
SUSPENSION_RULE = "county_suspension_v1"


def projected_evidence_id(evidence_id: str, building_key: str) -> str:
    """Identifier of an address row projected onto one pool key (SQL: sha256(id || chr(31) || key))."""
    import hashlib

    return hashlib.sha256(f"{evidence_id}\x1f{building_key}".encode("utf-8")).hexdigest()


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


def load_suspended_counties(config: Path | None = None) -> list[dict]:
    """Read ``suspended_counties`` (R05-9) from the pipeline TOML config; missing means none."""
    path = Path(config) if config is not None else DEFAULT_CONFIG
    with path.open("rb") as stream:
        value = tomllib.load(stream).get("suspended_counties", [])
    return check_suspended_counties(value)


def check_suspended_counties(value) -> list[dict]:
    """Validated suspension list sorted by county code; each item has code, name and reason."""
    from ..offline.match import COUNTY_NAMES

    if not isinstance(value, list):
        raise ValueError("suspended_counties must be a list of tables")
    items = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"code", "name", "reason"}:
            raise ValueError("Each suspended county needs exactly code, name and reason")
        if not all(isinstance(item[k], str) for k in ("code", "name", "reason")):
            raise ValueError("Suspended county fields must be text")
        if COUNTY_NAMES.get(item["code"]) != item["name"]:
            raise ValueError("Suspended county code and name disagree")
        if not item["reason"].strip():
            raise ValueError("Suspended county needs a reason")
        items.append({"code": item["code"], "name": item["name"], "reason": item["reason"]})
    codes = [item["code"] for item in items]
    if len(codes) != len(set(codes)):
        raise ValueError("Duplicate suspended county")
    return sorted(items, key=lambda item: item["code"])


def suspension_report(counties: list[dict]) -> dict:
    return {"rule": SUSPENSION_RULE, "counties": counties}


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


def door_list(doors) -> list[dict]:
    """Sorted, distinct doors as JSON objects; a missing part sorts first."""
    unique = {(village, neighborhood) for village, neighborhood in doors}
    return [
        {"village": village, "neighborhood": neighborhood}
        for village, neighborhood in sorted(
            unique, key=lambda d: (d[0] is not None, d[0] or "", d[1] is not None, d[1] or "")
        )
    ]


def resolution_row(key_version, building_key, points: dict, tolerance_m: float) -> dict:
    """The coordinate-resolution row of a key with several distinct coordinates.

    ``points`` maps (lng, lat) to {"evidence_count": rows, "evidence_id": smallest id}
    and, in states built with the door rule, "doors": [(village, neighborhood), ...].
    """
    adoption = adopt_coordinates(points, tolerance_m)
    door_aware = any("doors" in value for value in points.values())
    doors = {tuple(door) for value in points.values() for door in value.get("doors", [])}
    if door_aware and len(doors) > 1:
        adoption = {
            **adoption,
            "status": "conflict",
            "resolution_basis": DOOR_CROSS_CHECK,
            "representative": None,
        }
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
        "coordinates_json": _coordinates_json(points, adoption["distance_sums"], door_aware),
    }


def _coordinates_json(points: dict, distance_sums, door_aware: bool) -> str:
    return json.dumps(
        [
            {
                "lng": lng,
                "lat": lat,
                "evidence_count": points[(lng, lat)]["evidence_count"],
                "evidence_id": points[(lng, lat)]["evidence_id"],
                "distance_sum_m": total,
                **({"doors": door_list(points[(lng, lat)]["doors"])} if door_aware else {}),
            }
            for (lng, lat), total in distance_sums
        ],
        separators=(",", ":"),
    )


def suspended_row(key_version, building_key, points: dict, tolerance_m: float) -> tuple[dict, str]:
    """Coordinate-resolution row of a key in a suspended county (R05-9), and the status it
    would have without the suspension. ``points`` is as in ``resolution_row``; one point is allowed."""
    if len(points) > 1:
        normal = resolution_row(key_version, building_key, points, tolerance_m)
        would_be = normal["status"]
        row = {**normal, "lng": None, "lat": None, "evidence_id": None}
    else:
        ((point, value),) = points.items()
        would_be = "located"
        row = {
            "key_version": key_version,
            "building_key": building_key,
            "tolerance_m": tolerance_m,
            "max_distance_m": 0.0,
            "coordinate_count": 1,
            "evidence_count": value["evidence_count"],
            "lng": None,
            "lat": None,
            "evidence_id": None,
            "coordinates_json": _coordinates_json(points, [(point, 0.0)], "doors" in value),
        }
    return {**row, "status": "conflict", "resolution_basis": SUSPENDED}, would_be


def new_suspension_counts(counties: list[dict]) -> dict:
    return {
        item["code"]: {"keys": 0, "with_coordinates": 0, "would_be_located": 0,
                       "without_coordinates": 0, "tgos_evidence_keys": 0}
        for item in counties
    }


def check_suspension(path: Path, manifest: dict, report: dict) -> None:
    """R05-9: with a suspension list in the report, no key of a listed county is located and
    every listed key with coordinates carries the suspension basis, and only those do."""
    suspension = report.get("address_suspension")
    if suspension is None:
        return
    if suspension.get("rule") != SUSPENSION_RULE:
        raise ValueError("Unknown county suspension rule")
    codes = [item["code"] for item in check_suspended_counties(suspension["counties"])]
    files = {}
    for item in manifest["artifacts"]:
        if item["schema"] in {"address-result", "coordinate-resolution"}:
            files.setdefault(item["schema"], []).append(str(Path(path) / item["path"]))
    if "address-result" not in files:
        return
    with duckdb.connect() as db:
        db.read_parquet(files["address-result"]).create_view("result")
        if "coordinate-resolution" in files:
            db.read_parquet(files["coordinate-resolution"]).create_view("resolution")
        else:
            db.execute("CREATE TEMP TABLE resolution (building_key VARCHAR, resolution_basis VARCHAR)")
        db.execute("CREATE TEMP TABLE suspended (code VARCHAR)")
        if codes:
            db.executemany("INSERT INTO suspended VALUES (?)", [(code,) for code in codes])
        wrong = db.execute(
            f"""SELECT count(*) FROM result a LEFT JOIN resolution r USING (building_key)
            WHERE (a.county_code IN (SELECT code FROM suspended)
                   AND (a.status = 'located' OR (a.coordinate_count > 0
                        AND (a.status <> 'conflict' OR r.resolution_basis IS DISTINCT FROM '{SUSPENDED}'))))
               OR (r.resolution_basis = '{SUSPENDED}' AND a.county_code NOT IN (SELECT code FROM suspended))"""
        ).fetchone()[0]
    if wrong:
        raise ValueError("County suspension differs from the suspension list")


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
    if report["stage"] in {"offline-state", "tgos-state"}:
        check_suspension(path, manifest, report)
    return manifest, report


def resolve_offline(
    pool_path: Path,
    index_path: Path,
    work_dir: Path,
    *,
    prior_state: Path | None = None,
    coordinate_tolerance_m: float | None = None,
    suspended_counties: list[dict] | None = None,
    run_id=None,
):
    """Build the offline state; ``coordinate_tolerance_m=None`` and ``suspended_counties=None``
    read the pipeline config."""
    tolerance = (
        load_coordinate_tolerance()
        if coordinate_tolerance_m is None
        else check_tolerance(coordinate_tolerance_m)
    )
    suspended = (
        load_suspended_counties()
        if suspended_counties is None
        else check_suspended_counties(suspended_counties)
    )
    suspended_codes = {item["code"] for item in suspended}
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
            "door_rule": DOOR_RULE,
            "earth_radius_m": EARTH_RADIUS_M,
            "prior_state": bool(prior_state),
            "county_suspension_rule": SUSPENSION_RULE,
            "suspended_counties": suspended,
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
    cross_check = 0
    suspension_counts = new_suspension_counts(suspended)
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
            # Candidates of each pool key: same door signature, and the village and
            # neighbourhood the pool key writes. Each candidate row is projected onto the
            # pool key so evidence_id stays unique when one row supports several keys.
            db.execute(
                f"""CREATE TEMP TABLE projected AS
      WITH p AS (SELECT building_key, {door_signature_sql("building_key")} sig,
                        {sub_identity_sql("building_key", "village")} v,
                        {sub_identity_sql("building_key", "neighborhood")} n FROM pool),
           e AS (SELECT *, {door_signature_sql("building_key")} sig,
                        {sub_identity_sql("building_key", "village")} v,
                        {sub_identity_sql("building_key", "neighborhood")} n
                 FROM evidence WHERE building_key IS NOT NULL)
      SELECT sha256(e.evidence_id || chr(31) || p.building_key) evidence_id, e.key_version,
             p.building_key, e.normalized_address, e.county_code, e.town_code, e.lng, e.lat,
             e.validity, e.source_kind, e.source_ref, e.input_sha256, e.source_row_number,
             e.v door_village, e.n door_neighborhood
      FROM p JOIN e ON e.sig = p.sig AND (p.v IS NULL OR e.v = p.v) AND (p.n IS NULL OR e.n = p.n)"""
            )
            columns = [field.name for field in dataset_schema("offline-row")]
            cursor = db.execute(
                f"SELECT {','.join(columns)} FROM projected ORDER BY evidence_id"
            )
            while batch := cursor.fetchmany(1024):
                for values in batch:
                    evidence.add(dict(zip(columns, values)))
            evidence.close()
            query = """WITH coordinates AS (
      SELECT building_key,lng,lat,min(evidence_id) evidence_id,count(*) evidence_count,
        list(DISTINCT [door_village, door_neighborhood]) doors
      FROM projected WHERE validity='valid' GROUP BY building_key,lng,lat),
     summaries AS (SELECT building_key,count(*) coordinate_count,sum(evidence_count) evidence_count,
       list(struct_pack(lng:=lng,lat:=lat,n:=evidence_count,eid:=evidence_id,doors:=doors) ORDER BY lng,lat) points
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
                    if county in suspended_codes:
                        counted = suspension_counts[county]
                        counted["keys"] += 1
                        counted["with_coordinates" if count else "without_coordinates"] += 1
                    if count >= 1 and county in suspended_codes:
                        # R05-9: never located; the coordinates stay listed in the evidence.
                        detail, would_be = suspended_row(
                            version,
                            key,
                            {
                                (p["lng"], p["lat"]): {
                                    "evidence_count": p["n"],
                                    "evidence_id": p["eid"],
                                    "doors": [tuple(d) for d in p["doors"]],
                                }
                                for p in points
                            },
                            tolerance,
                        )
                        resolutions.add(detail)
                        counted["would_be_located"] += would_be == "located"
                        status = "conflict"
                    elif count == 1:
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
                                    "doors": [tuple(d) for d in p["doors"]],
                                }
                                for p in points
                            },
                            tolerance,
                        )
                        resolutions.add(detail)
                        if detail["resolution_basis"] == DOOR_CROSS_CHECK:
                            cross_check += 1
                        else:
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
        "door_rule": DOOR_RULE,
        "door_cross_check_keys": cross_check,
        "address_suspension": suspension_report(suspended),
        "address_suspension_counts": suspension_counts,
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
    # R05-7: the district lookups of the pool travel with the state for the review stage.
    district = [item for item in pool_manifest["artifacts"] if item["schema"] == "district-candidate"]
    if district:
        report["dataset_counts"]["district-candidate"] = sum(item["row_count"] for item in district)
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
        *[
            (item["path"], Path(pool_path) / item["path"], "district-candidate", item["row_count"])
            for item in district
        ],
    ]
    return stage.finish(artifacts, report)
