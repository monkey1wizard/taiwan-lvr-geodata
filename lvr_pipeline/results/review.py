"""Exception and review table (R05-8), built read-only from existing snapshots.

Each row names one source observation component and one reason code. Rows are a
review view only: no other dataset is changed and no observation is removed.
Reason codes are defined in ``contracts.schemas.EXCEPTION_REASON_CODES``; this
stage produces the codes that existing snapshot data can decide:

- ``invalid_admin``: address-pool occurrence status ``invalid_admin``. Since R05-7
  a missing district is looked up first; ``invalid_admin`` is left for text whose
  county and town cannot be read and that has no candidate door.
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

R04-12 codes (``R04_12_CODES``). Pairs need the same county, town, village and
neighbourhood; they are never merged and each side points at the other:

- ``annex_variant_pair``: doors ``N附M`` and ``N號附M``. ``subdoor_variant_pair``
  keeps the doors with 之 only.
- ``named_lane_variant_pair``: same door; one street ends with an extra text lane
  name (豐年街 and 豐年街豐二巷).
- ``lane_numeral_variant_pair``: streets that differ only in lane or alley numbers
  written in Chinese or Arabic numerals (十二巷 and 12巷).
- ``bracket_note``: the transaction address carries a bracketed note, which takes
  no part in the key; ``related_json`` lists the notes.
- ``door_cross_check``: the key's candidates are several doors (village or
  neighbourhood differ) with different coordinates. These rows go to the manual
  cross-check list ``cross_check_addresses.parquet`` / ``.csv``, separate from the
  general review table, and are not listed as ``coordinate_conflict``.

R05-7 codes (``R05_7_CODES``), from the district lookup of the address pool
(``district_candidates.parquet``); ``related_json`` gives the search scope, the
source county from the ZIP member letter and every candidate door:

- ``district_missing``: county written, town missing, no candidate door in the county.
- ``district_ambiguous``: candidate doors in two or more towns of the county.
- ``road_only_not_unique``: road only; no candidate door, or doors in two or more
  towns, in the source county (the whole country when it is unknown).
- ``door_cross_check`` is also written to the manual cross-check list when the
  candidates of a ``district_ambiguous`` or ``road_only_not_unique`` row are in
  several towns and have different coordinates.

R05-9 code (``R05_9_CODES``):

- ``address_source_suspended``: the key's county is in the suspension list of the
  address status source (``address_suspension`` in the TGOS or offline state
  report) and the key is not located. One row per transaction address member, with
  county and town; ``related_json`` gives the county name, the suspension reason,
  the key's status and every coordinate of the evidence. These keys are not also
  listed as ``offline_unmatched``, ``coordinate_conflict`` or ``door_cross_check``.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import re
import tempfile

import duckdb

from ..contracts.schemas import EXCEPTION_REASON_CODES, dataset_schema
from ..offline.resolve import _find_candidates
from ..addresses.parse import parse
from ..storage.parquet import BatchWriter, duckdb_config
from ..storage.runs import Stage, bindings, digest, load_snapshot, sha256_file
from ..addresses.identity import cjk_number
from ..offline.index import _haversine_sql
from .reconcile import DOOR_CROSS_CHECK, load_p2

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
R04_12_CODES = (
    "annex_variant_pair",
    "named_lane_variant_pair",
    "lane_numeral_variant_pair",
    "bracket_note",
)
CROSS_CHECK_CODES = ("door_cross_check",)
R05_7_CODES = ("district_missing", "district_ambiguous", "road_only_not_unique")
R05_9_CODES = ("address_source_suspended",)
# A street that ends with a text lane name (豐年街豐二巷): group 1 is the street without it.
NAMED_LANE_SQL = "^(.+[路街道段巷弄])([^0-9路街道段巷弄]*[^0-9一二三四五六七八九十百路街道段巷弄][^0-9路街道段巷弄]*巷)$"
_LANE_NUMERAL = re.compile("([一二三四五六七八九十百]+)(?=[巷弄])")


def lane_numeral_signature(locality_road: str) -> str | None:
    """The street with Chinese lane and alley numbers in Arabic numerals; None when there are none."""
    if not _LANE_NUMERAL.search(locality_road):
        return None
    return _LANE_NUMERAL.sub(lambda m: str(cjk_number(m.group(1)) or m.group(1)), locality_road)
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
         json_extract_string(substr(building_key, 4), '$.door') d,
         json_extract_string(substr(building_key, 4), '$.village') v,
         json_extract_string(substr(building_key, 4), '$.neighborhood') n
  FROM pool),
lane_named AS (
  SELECT *, regexp_extract(r, '{named_lane}', 1) base_r FROM keys
  WHERE regexp_full_match(r, '{named_lane}')),
named AS (
  SELECT a.building_key ka, a.canonical_address aa, b.building_key kb, b.canonical_address ab
  FROM keys a JOIN lane_named b
    ON a.c = b.c AND a.t = b.t AND a.d = b.d AND a.v IS NOT DISTINCT FROM b.v AND a.n IS NOT DISTINCT FROM b.n
   AND a.r = b.base_r),
numeral AS (
  SELECT a.building_key ka, a.canonical_address aa, b.building_key kb, b.canonical_address ab
  FROM lane_signatures s JOIN keys a ON a.building_key = s.building_key
  JOIN keys b ON a.c = b.c AND a.t = b.t AND a.d = b.d AND a.v IS NOT DISTINCT FROM b.v
   AND a.n IS NOT DISTINCT FROM b.n AND b.r = s.signature),
pairs AS (
  SELECT CASE WHEN contains(a.d, '附') THEN 'annex_variant_pair' ELSE 'subdoor_variant_pair' END code,
         a.building_key, b.building_key pair_key, b.canonical_address pair_address
  FROM keys a JOIN keys b
    ON a.c = b.c AND a.t = b.t AND a.r = b.r AND a.v IS NOT DISTINCT FROM b.v AND a.n IS NOT DISTINCT FROM b.n
   AND replace(a.d, '號', '') = replace(b.d, '號', '') AND a.d <> b.d
  UNION ALL SELECT 'named_lane_variant_pair', ka, kb, ab FROM named
  UNION ALL SELECT 'named_lane_variant_pair', kb, ka, aa FROM named
  UNION ALL SELECT 'lane_numeral_variant_pair', ka, kb, ab FROM numeral
  UNION ALL SELECT 'lane_numeral_variant_pair', kb, ka, aa FROM numeral),
coordinates AS (
  SELECT building_key,
         to_json(list(struct_pack(lng := lng, lat := lat, evidence_count := n) ORDER BY lng, lat)) points
  FROM (SELECT building_key, lng, lat, count(*) n FROM evidence WHERE validity = 'valid' GROUP BY ALL)
  GROUP BY building_key),
district_detail AS (
  SELECT component_id, reason, town_count, coordinate_count,
         json_object('search_scope', search_scope, 'search_county_code', search_county_code,
                     'source_county_code', source_county_code, 'town_count', town_count,
                     'coordinate_count', coordinate_count, 'candidates', candidates_json::JSON)::VARCHAR related
  FROM district WHERE reason IN ('district_missing', 'district_ambiguous', 'road_only_not_unique')),
cross_keys AS (
  SELECT building_key, coordinates_json FROM resolution WHERE resolution_basis = '{cross_check}'),
suspended_keys AS (
  SELECT s.building_key, s.status, s.coordinate_count, x.name, x.reason
  FROM result s JOIN suspended x ON s.county_code = x.code WHERE s.status <> 'located'),
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
  SELECT p.code, b.*, p.pair_key,
         json_object('pair_building_key', p.pair_key, 'pair_address', p.pair_address)::VARCHAR
  FROM base b JOIN pairs p USING (building_key)
  UNION ALL
  SELECT 'bracket_note', b.*, '',
         json_object('notes', regexp_extract_all(b.normalized_address, '\([^()]*\)|（[^（）]*）'))::VARCHAR
  FROM base b WHERE regexp_matches(b.normalized_address, '\([^()]*\)|（[^（）]*）')
  UNION ALL
  SELECT 'door_cross_check', b.*, '', json_object('coordinates', k.coordinates_json::JSON)::VARCHAR
  FROM base b JOIN result s USING (building_key) JOIN cross_keys k USING (building_key)
  WHERE s.status = 'conflict'
  UNION ALL
  SELECT d.reason, b.*, '', d.related FROM base b JOIN district_detail d USING (component_id)
  UNION ALL
  SELECT 'door_cross_check', b.*, '', d.related FROM base b JOIN district_detail d USING (component_id)
  WHERE d.town_count > 1 AND d.coordinate_count > 1
  UNION ALL
  SELECT 'address_source_suspended', b.*, '',
         json_object('county_name', k.name, 'suspension_reason', k.reason, 'status', k.status,
                     'coordinate_count', k.coordinate_count, 'coordinates', c.points)::VARCHAR
  FROM base b JOIN suspended_keys k USING (building_key) LEFT JOIN coordinates c USING (building_key)
  UNION ALL
  SELECT 'offline_unmatched', b.*, '', NULL
  FROM base b JOIN result s USING (building_key) WHERE s.status = 'unmatched'
    AND building_key NOT IN (SELECT building_key FROM suspended_keys)
  UNION ALL
  SELECT 'coordinate_conflict', b.*, '',
         json_object('coordinates', c.points, 'max_distance_m', d.max_distance_m)::VARCHAR
  FROM base b JOIN result s USING (building_key) LEFT JOIN coordinates c USING (building_key)
  LEFT JOIN spans d USING (building_key)
  WHERE s.status = 'conflict' AND building_key NOT IN (SELECT building_key FROM cross_keys)
    AND building_key NOT IN (SELECT building_key FROM suspended_keys)
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
    status_report = state_report
    if tgos_state is not None:
        tgos_state = Path(tgos_state)
        tgos_manifest, tgos_report = load_p2(tgos_state, "tgos-state")
        if (
            tgos_report["converted_snapshot_sha256"] != converted_hash
            or tgos_report["address_source_commit"] != descriptor["commit"]
        ):
            raise ValueError("TGOS state comes from other inputs")
        hashes.append(sha256_file(tgos_state / "manifest.json"))
        status_report = tgos_report
    # R05-9: the suspension list of the address status source; states before R05-9 have none.
    suspension = status_report.get("address_suspension")
    suspended = suspension["counties"] if suspension is not None else []
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
         "r04_12_codes": list(R04_12_CODES), "cross_check_codes": list(CROSS_CHECK_CODES),
         "r05_7_codes": list(R05_7_CODES), "r05_9_codes": list(R05_9_CODES),
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
    # R04-12: the manual cross-check list is a separate file with the same columns.
    cross_writer = BatchWriter(stage.build / "cross_check_addresses.parquet", "exception-address")
    cross_csv_path = stage.build / "cross_check_addresses.csv"
    reason_rows = {code: 0 for code in EXCEPTION_REASON_CODES}
    candidate_cache: dict[tuple[str, str], list[str]] = {}
    with tempfile.TemporaryDirectory(prefix="review-", dir=stage.build) as spill:
        with duckdb.connect(config=duckdb_config(spill)) as db, csv_path.open(
            "w", encoding="utf-8-sig", newline=""
        ) as stream, cross_csv_path.open("w", encoding="utf-8-sig", newline="") as cross_stream:
            db.read_parquet(_paths(state, state_manifest, "address-occurrence")).create_view("occurrence")
            db.read_parquet(_paths(state, state_manifest, "address-pool")).create_view("pool")
            # With a TGOS state, its address status and evidence are the final ones.
            status_snapshot, status_manifest = (
                (tgos_state, tgos_manifest) if tgos_manifest is not None else (state, state_manifest)
            )
            db.read_parquet(_paths(status_snapshot, status_manifest, "address-result")).create_view("result")
            db.read_parquet(_paths(status_snapshot, status_manifest, "offline-row")).create_view("evidence")
            db.read_parquet(_paths(converted, converted_manifest, "observation")).create_view("observations")
            resolution_paths = _paths(status_snapshot, status_manifest, "coordinate-resolution")
            if resolution_paths:
                db.read_parquet(resolution_paths).create_view("resolution")
            else:
                db.execute("CREATE TEMP TABLE resolution (building_key VARCHAR, resolution_basis VARCHAR, "
                           "coordinates_json VARCHAR)")
            district_paths = _paths(state, state_manifest, "district-candidate")
            if district_paths:
                db.read_parquet(district_paths).create_view("district")
            else:
                db.execute("CREATE TEMP TABLE district (component_id VARCHAR, reason VARCHAR, search_scope VARCHAR, "
                           "search_county_code VARCHAR, source_county_code VARCHAR, town_count BIGINT, "
                           "coordinate_count BIGINT, candidates_json VARCHAR)")
            db.execute("CREATE TEMP TABLE suspended (code VARCHAR, name VARCHAR, reason VARCHAR)")
            if suspended:
                db.executemany("INSERT INTO suspended VALUES (?, ?, ?)",
                               [(item["code"], item["name"], item["reason"]) for item in suspended])
            db.execute("CREATE TEMP TABLE lane_signatures (building_key VARCHAR, signature VARCHAR)")
            signatures = []
            for key, road in db.execute(
                "SELECT building_key, json_extract_string(substr(building_key, 4), '$.locality_road') FROM pool "
                "WHERE regexp_matches(json_extract_string(substr(building_key, 4), '$.locality_road'), "
                "'[一二三四五六七八九十百]+[巷弄]') ORDER BY building_key"
            ).fetchall():
                signature = lane_numeral_signature(road)
                if signature is not None and signature != road:
                    signatures.append((key, signature))
            if signatures:
                db.executemany("INSERT INTO lane_signatures VALUES (?, ?)", signatures)
            tgos_sql = ""
            if tgos_manifest is not None:
                db.read_parquet(_paths(tgos_state, tgos_manifest, "tgos-result")).create_view("tgos_result")
                db.read_parquet(_paths(tgos_state, tgos_manifest, "tgos-query")).create_view("tgos_query")
                db.execute("CREATE TEMP TABLE rejection_codes (reason VARCHAR, code VARCHAR)")
                db.executemany("INSERT INTO rejection_codes VALUES (?, ?)", list(TGOS_REJECTION_CODES.items()))
                tgos_sql = _TGOS_SQL
            db.execute(
                "CREATE TEMP TABLE picked AS "
                + _SQL.format(tgos=tgos_sql, distance=_haversine_sql("a", "b"), cross_check=DOOR_CROSS_CHECK,
                              named_lane=NAMED_LANE_SQL)
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
            cross_output = csv.writer(cross_stream)
            cross_output.writerow(COLUMNS)
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
                    cross = row["reason_code"] in CROSS_CHECK_CODES
                    (cross_writer if cross else writer).add(row)
                    (cross_output if cross else output).writerow(
                        ["" if row[name] is None else row[name] for name in COLUMNS]
                    )
            distinct = dict(
                db.execute("SELECT reason_code, count(DISTINCT normalized_address) FROM picked GROUP BY 1").fetchall()
            )
    writer.close()
    cross_writer.close()
    stage.store.add(stage.id, "exception_addresses.csv", csv_path, format_name="csv", row_count=writer.row_count)
    stage.store.add(stage.id, "cross_check_addresses.csv", cross_csv_path, format_name="csv",
                    row_count=cross_writer.row_count)
    report = {
        "converted_snapshot_sha256": converted_hash,
        "offline_state_manifest_sha256": hashes[1],
        "address_status_source": "tgos-state" if tgos_manifest is not None else "offline-state",
        "tgos_state_manifest_sha256": hashes[2] if tgos_manifest is not None else None,
        "prior_review": str(prior_review) if prior_review is not None else None,
        "address_source_commit": descriptor["commit"],
        "address_suspension": suspension,
        "run_id": stage.id,
        "reason_counts": reason_rows,
        "reason_distinct_addresses": {code: distinct.get(code, 0) for code in EXCEPTION_REASON_CODES},
        "csv_bytes": csv_path.stat().st_size,
        "cross_check_rows": cross_writer.row_count,
        "cross_check_csv_bytes": cross_csv_path.stat().st_size,
        # Both files use the exception-address columns; the count covers both.
        "dataset_counts": {"exception-address": writer.row_count + cross_writer.row_count},
    }
    return stage.finish(
        [("exception_addresses.parquet", writer.path, "exception-address", writer.row_count),
         ("cross_check_addresses.parquet", cross_writer.path, "exception-address", cross_writer.row_count)],
        report,
    )
