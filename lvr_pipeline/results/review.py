"""Exception and review table (R05-8), built read-only from existing snapshots.

Each row names one source observation component and one reason code. Rows are a
review view only: no other dataset is changed and no observation is removed.
Reason codes are defined in ``contracts.schemas.EXCEPTION_REASON_CODES``; this
stage produces the codes that existing snapshot data can decide:

- ``invalid_admin``: address-pool occurrence status ``invalid_admin``. The pool
  does not separate a missing district from other unparsable starts, so
  ``district_missing`` is not produced here (R05-7).
- ``garbled_pending``: occurrence status ``candidate_review``. The candidate road
  list is recomputed with the existing resolver candidate search.
- ``subdoor_variant_pair``: two pool keys with the same county, town and
  locality/road whose doors are ``N之M`` and ``N號之M``. One row per side,
  each pointing at the other. They are not merged.
- ``address_review``: occurrence status ``address_review`` (county and town
  parse, but the door text yields no key, such as a range or several doors).
- ``offline_unmatched`` and ``coordinate_conflict``: address status of the TGOS
  state when one is given (its final status after TGOS), else the offline state.
  Since R05-4 a conflict is a key whose coordinates are more than the tolerance
  apart; ``related_json`` lists every coordinate and the largest pairwise
  haversine distance ``max_distance_m``.
- ``tgos_isolated``, ``tgos_response_incomplete``, ``tgos_coordinate_invalid``:
  TGOS results rejected with the matching reason text recorded by ``import-tgos``.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import tempfile

import duckdb

from ..contracts.schemas import EXCEPTION_REASON_CODES, dataset_schema
from ..offline.resolve import _find_candidates
from ..addresses.parse import parse
from ..storage.parquet import BatchWriter, duckdb_config
from ..storage.runs import Stage, bindings, digest, load_snapshot, sha256_file
from ..offline.index import _haversine_sql
from .reconcile import load_p2

# Reason codes for TGOS results rejected by import-tgos, keyed by its recorded reason text.
TGOS_REJECTION_CODES = {
    "TGOS response address differs from submitted address": "tgos_isolated",
    "TGOS response is not a complete address": "tgos_response_incomplete",
    "TGOS coordinate is outside declared WGS84 Taiwan bounds": "tgos_coordinate_invalid",
}
PRODUCED_CODES = (
    "invalid_admin",
    "address_review",
    "garbled_pending",
    "subdoor_variant_pair",
    "offline_unmatched",
    "coordinate_conflict",
    *TGOS_REJECTION_CODES.values(),
)
COLUMNS = [field.name for field in dataset_schema("exception-address")]


def exception_id(reason_code: str, component_id: str, discriminator: str = "") -> str:
    """Stable identifier: reason code, source component and the related item (pair key or TGOS result)."""
    text = "\x1f".join([reason_code, component_id, discriminator])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _paths(snapshot: Path, manifest: dict, schema: str) -> list[str]:
    return [str(Path(snapshot) / item["path"]) for item in manifest["artifacts"] if item["schema"] == schema]


def _roads_by_county(descriptor: dict) -> dict[str, set[str]]:
    roads: dict[str, set[str]] = {}
    for item in descriptor["roads"]:
        county, road = item["path"].split("/")[1][:-4].split("-", 1)
        roads.setdefault(county, set()).add(road)
    return roads


def garbled_candidates(address: str, county_roads: set[str]) -> list[str]:
    """Road names the existing resolver search matches for a single garbled road character."""
    _, _, road, _ = parse(address)
    if not road:
        return []
    return sorted(_find_candidates(road, county_roads, []))


_SQL = r"""
WITH base AS (
  SELECT a.component_id, a.raw_record_id, a.normalized_address, a.building_key,
         nullif(a.county_code, '') county_code, nullif(a.town_code, '') town_code, a.reason
  FROM occurrence a),
keys AS (
  SELECT building_key, canonical_address,
         json_extract_string(substr(building_key, 4), '$.county') c,
         json_extract_string(substr(building_key, 4), '$.town') t,
         json_extract_string(substr(building_key, 4), '$.locality_road') r,
         json_extract_string(substr(building_key, 4), '$.door') d
  FROM pool),
pairs AS (
  SELECT a.building_key, b.building_key pair_key, b.canonical_address pair_address
  FROM keys a JOIN keys b
    ON a.c = b.c AND a.t = b.t AND a.r = b.r
   AND replace(a.d, '號', '') = replace(b.d, '號', '') AND a.d <> b.d),
coordinates AS (
  SELECT building_key,
         to_json(list(struct_pack(lng := lng, lat := lat, evidence_count := n) ORDER BY lng, lat)) points
  FROM (SELECT building_key, lng, lat, count(*) n FROM evidence WHERE validity = 'valid' GROUP BY ALL)
  GROUP BY building_key),
conflict_points AS (
  SELECT DISTINCT e.building_key, e.lng, e.lat FROM evidence e JOIN result s USING (building_key)
  WHERE e.validity = 'valid' AND s.status = 'conflict'),
spans AS (
  SELECT a.building_key, max({distance}) max_distance_m
  FROM conflict_points a JOIN conflict_points b
    ON a.building_key = b.building_key AND (a.lng, a.lat) < (b.lng, b.lat)
  GROUP BY a.building_key),
chosen AS (
  SELECT 'invalid_admin' reason_code, b.*, '' disc, NULL::VARCHAR related_json
  FROM base b WHERE b.reason = 'invalid_admin'
  UNION ALL
  SELECT 'garbled_pending', b.*, '', NULL FROM base b WHERE b.reason = 'candidate_review'
  UNION ALL
  SELECT 'address_review', b.*, '', NULL FROM base b WHERE b.reason = 'address_review'
  UNION ALL
  SELECT 'subdoor_variant_pair', b.*, p.pair_key,
         json_object('pair_building_key', p.pair_key, 'pair_address', p.pair_address)::VARCHAR
  FROM base b JOIN pairs p USING (building_key)
  UNION ALL
  SELECT 'offline_unmatched', b.*, '', NULL
  FROM base b JOIN result s USING (building_key) WHERE s.status = 'unmatched'
  UNION ALL
  SELECT 'coordinate_conflict', b.*, '',
         json_object('coordinates', c.points, 'max_distance_m', d.max_distance_m)::VARCHAR
  FROM base b JOIN result s USING (building_key) LEFT JOIN coordinates c USING (building_key)
  LEFT JOIN spans d USING (building_key)
  WHERE s.status = 'conflict'
  {tgos}
)
SELECT p.reason_code, p.component_id, p.disc, p.raw_record_id, o.src_batch, o.category, o.raw_address,
       p.normalized_address, p.building_key, p.county_code, p.town_code,
       json_extract_string(o.props_json, '$."鄉鎮市區"') source_district, p.related_json
FROM chosen p JOIN observations o USING (raw_record_id)
"""

_TGOS_SQL = r"""
  UNION ALL
  SELECT m.code, b.*, r.result_id,
         json_object('batch_id', r.batch_id, 'result_id', r.result_id, 'submitted_address', r.address,
                     'response_address', r.response_address, 'reason', r.reason)::VARCHAR
  FROM tgos_result r JOIN rejection_codes m ON r.reason = m.reason
  JOIN tgos_query q USING (batch_id, query_fingerprint)
  JOIN base b ON b.building_key = q.building_key
  WHERE r.status = 'rejected'
"""


def build_review(
    converted: Path,
    state: Path,
    work_dir: Path,
    *,
    descriptor: dict,
    tgos_state: Path | None = None,
    prior_review: Path | None = None,
    run_id: str | None = None,
) -> Path:
    converted, state = Path(converted), Path(state)
    converted_manifest, _ = load_snapshot(converted, "converted")
    state_manifest, state_report = load_p2(state, "offline-state")
    converted_hash = sha256_file(converted / "manifest.json")
    if state_report["converted_snapshot_sha256"] != converted_hash:
        raise ValueError("Offline state was not built from this converted snapshot")
    if state_report["address_source_commit"] != descriptor["commit"]:
        raise ValueError("Address source pin differs from offline state")
    hashes = [converted_hash, sha256_file(state / "manifest.json")]
    tgos_manifest = None
    if tgos_state is not None:
        tgos_state = Path(tgos_state)
        tgos_manifest, tgos_report = load_p2(tgos_state, "tgos-state")
        if (
            tgos_report["converted_snapshot_sha256"] != converted_hash
            or tgos_report["address_source_commit"] != descriptor["commit"]
        ):
            raise ValueError("TGOS state comes from other inputs")
        hashes.append(sha256_file(tgos_state / "manifest.json"))
    prior_paths = []
    if prior_review is not None:
        prior_review = Path(prior_review)
        prior_manifest, _ = load_p2(prior_review, "review")
        prior_paths = _paths(prior_review, prior_manifest, "exception-address")
        hashes.append(sha256_file(prior_review / "manifest.json"))
    hashes.append(digest(descriptor))
    binding = bindings(
        "review",
        {"reason_codes": list(EXCEPTION_REASON_CODES), "produced_codes": list(PRODUCED_CODES),
         "tgos_rejection_codes": TGOS_REJECTION_CODES, "prior_review": prior_review is not None,
         "final_status_from_tgos": tgos_state is not None},
        hashes,
    )
    stage = Stage(Path(work_dir) / "review", "review", binding, run_id)
    if stage.reused:
        return stage.path
    roads = _roads_by_county(descriptor)
    writer = BatchWriter(stage.build / "exception_addresses.parquet", "exception-address")
    csv_path = stage.build / "exception_addresses.csv"
    reason_rows = {code: 0 for code in EXCEPTION_REASON_CODES}
    candidate_cache: dict[tuple[str, str], list[str]] = {}
    with tempfile.TemporaryDirectory(prefix="review-", dir=stage.build) as spill:
        with duckdb.connect(config=duckdb_config(spill)) as db, csv_path.open(
            "w", encoding="utf-8-sig", newline=""
        ) as stream:
            db.read_parquet(_paths(state, state_manifest, "address-occurrence")).create_view("occurrence")
            db.read_parquet(_paths(state, state_manifest, "address-pool")).create_view("pool")
            # With a TGOS state, its address status and evidence are the final ones.
            status_snapshot, status_manifest = (
                (tgos_state, tgos_manifest) if tgos_manifest is not None else (state, state_manifest)
            )
            db.read_parquet(_paths(status_snapshot, status_manifest, "address-result")).create_view("result")
            db.read_parquet(_paths(status_snapshot, status_manifest, "offline-row")).create_view("evidence")
            db.read_parquet(_paths(converted, converted_manifest, "observation")).create_view("observations")
            tgos_sql = ""
            if tgos_manifest is not None:
                db.read_parquet(_paths(tgos_state, tgos_manifest, "tgos-result")).create_view("tgos_result")
                db.read_parquet(_paths(tgos_state, tgos_manifest, "tgos-query")).create_view("tgos_query")
                db.execute("CREATE TEMP TABLE rejection_codes (reason VARCHAR, code VARCHAR)")
                db.executemany("INSERT INTO rejection_codes VALUES (?, ?)", list(TGOS_REJECTION_CODES.items()))
                tgos_sql = _TGOS_SQL
            db.execute(
                "CREATE TEMP TABLE picked AS "
                + _SQL.format(tgos=tgos_sql, distance=_haversine_sql("a", "b"))
            )
            if prior_paths:
                db.read_parquet(prior_paths).create_view("prior")
            else:
                db.execute("CREATE TEMP TABLE prior (exception_id VARCHAR, first_run_id VARCHAR)")
            # Same text and hash as exception_id(); computed in SQL to join the prior review.
            cursor = db.execute(
                """SELECT i.*, coalesce(p.first_run_id, ?) first_run_id FROM (
                  SELECT sha256(concat_ws(chr(31), reason_code, component_id, disc)) exception_id, *
                  FROM picked) i
                LEFT JOIN prior p USING (exception_id) ORDER BY i.reason_code, i.exception_id""",
                [stage.id],
            )
            # Column names of the cursor, not a row choice.
            names = [x[0] for x in cursor.description]
            output = csv.writer(stream)
            output.writerow(COLUMNS)
            while batch := cursor.fetchmany(1024):
                for values in batch:
                    row = dict(zip(names, values))
                    row.pop("disc")
                    if row["reason_code"] == "garbled_pending":
                        # cache_key[0] below is the county code of this tuple, not a row choice.
                        cache_key = (row["county_code"] or "", row["normalized_address"])
                        if cache_key not in candidate_cache:
                            candidate_cache[cache_key] = garbled_candidates(
                                row["normalized_address"], roads.get(cache_key[0], set())
                            )
                        found = candidate_cache[cache_key]
                        row["related_json"] = json.dumps(
                            {"candidate_roads": found, "candidate_count": len(found)},
                            ensure_ascii=False, separators=(",", ":"),
                        )
                    reason_rows[row["reason_code"]] += 1
                    writer.add(row)
                    output.writerow(["" if row[name] is None else row[name] for name in COLUMNS])
            distinct = dict(
                db.execute("SELECT reason_code, count(DISTINCT normalized_address) FROM picked GROUP BY 1").fetchall()
            )
    writer.close()
    stage.store.add(stage.id, "exception_addresses.csv", csv_path, format_name="csv", row_count=writer.row_count)
    report = {
        "converted_snapshot_sha256": converted_hash,
        "offline_state_manifest_sha256": hashes[1],
        "address_status_source": "tgos-state" if tgos_manifest is not None else "offline-state",
        "tgos_state_manifest_sha256": hashes[2] if tgos_manifest is not None else None,
        "prior_review": str(prior_review) if prior_review is not None else None,
        "address_source_commit": descriptor["commit"],
        "run_id": stage.id,
        "reason_counts": reason_rows,
        "reason_distinct_addresses": {code: distinct.get(code, 0) for code in EXCEPTION_REASON_CODES},
        "csv_bytes": csv_path.stat().st_size,
        "dataset_counts": {"exception-address": writer.row_count},
    }
    return stage.finish(
        [("exception_addresses.parquet", writer.path, "exception-address", writer.row_count)],
        report,
    )
