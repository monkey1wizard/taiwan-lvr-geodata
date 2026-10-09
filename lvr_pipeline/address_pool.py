"""Disk-backed unique address pool with separate, bounded occurrence rows.

District fill (R05-7, owner decisions 2026-10-07 and 2026-10-09). When the town of an address
cannot be found, and the offline index is given:

- County written, town missing (新竹市明湖路1015巷12號): candidates are the valid index rows of
  that county with the same street or place, lanes and door, and the village and neighbourhood
  the address writes. One candidate town: the town is filled in as evidenced, the key is made
  from the filled address and the occurrence reason is ``road_unique_in_county``. Several towns:
  ``district_ambiguous``. None: ``district_missing`` (left for TGOS, R09-6).
- Road only, no county and no town (慈濟路165號): the same search inside the source county,
  taken from the ZIP member letter (config/reference/lvr_county_letters.csv); the whole country
  only when the source county is unknown. One town: ``road_only_unique``; otherwise
  ``road_only_not_unique``.

Where the key is located is then decided by resolve-offline with the door rule of R04-12.
Candidates are never chosen by road-name similarity, and the transaction text is never
changed; the filled address is kept only in the occurrence row. A component whose text after
the county starts like an administrative name (市東區…, 桃園縣…) and has no candidate stays
``invalid_admin``. ``district_candidates.parquet`` records every lookup and its candidates.
"""

from pathlib import Path
import csv
import json
import tempfile
import re

import duckdb
import pyarrow as pa

from .addresses import identity
from .addresses.identity import building_key_v2, door_parts, door_signature_sql, sub_identity_sql
from .addresses.parse import _COUNTY_CODE
from .offline.index import load_pinned_source
from .storage.parquet import BatchWriter, duckdb_config, rows
from .storage.runs import Stage, bindings, digest, load_snapshot
from .storage.runs import ROOT, sha256_file

DISTRICT_FILL_RULE = "road_unique_v1"
REPEATED_COUNTY_RULE = "exact_repeat_v1"
COUNTY_LETTERS = ROOT / "config" / "reference" / "lvr_county_letters.csv"
FILLED_REASONS = ("road_unique_in_county", "road_only_unique")
_COUNTY_START = re.compile("^(" + "|".join(sorted(_COUNTY_CODE, key=len, reverse=True)) + ")")
# Text that starts like a county or town name (市東區…, v新興區…, 桃園縣…); used only to label a
# lookup without candidates as invalid_admin, never to match.
_ADMIN_START = re.compile(r"^[^0-9路街道段巷弄號村里鄰]{0,4}?[縣市區鄉鎮]")
_PLACEHOLDER_TOWN = "東區"


def district_query(address: str):
    """Door parts of an address whose town was not found.

    Returns (county code or None, text after the county, locality_road, door, village,
    neighbourhood), or None when no door can be parsed. The town is replaced by a placeholder
    only to split the text with building_key_v2; the placeholder never reaches a key.
    """
    match = _COUNTY_START.match(address)
    county = _COUNTY_CODE[match.group(1)] if match else None
    rest = address[match.end():] if match else address
    key = building_key_v2((match.group(1) if match else "臺北市") + _PLACEHOLDER_TOWN + rest)
    if key is None:
        return None
    value = json.loads(key[3:])
    if value["town"] != _PLACEHOLDER_TOWN:
        return None
    return county, rest, value["locality_road"], value["door"], value.get("village"), value.get("neighborhood")


def county_letter_codes(path: Path = COUNTY_LETTERS) -> dict[str, str]:
    """ZIP member letter to county code."""
    with Path(path).open(encoding="utf-8", newline="") as stream:
        return {row["letter"]: row["county_code"] for row in csv.DictReader(stream)}


_FILL_SQL = r"""
WITH p AS (
  SELECT q.*, l.county_code source_county, coalesce(q.county, l.county_code) search_county
  FROM pending q LEFT JOIN fill_observations o USING (raw_record_id)
  LEFT JOIN letters l ON l.letter = regexp_extract(lower(o.member_path), '(?:^|/)([a-z])_lvr_land_[abc]\.csv$', 1)),
queries AS (SELECT DISTINCT search_county, lr, d, v, n FROM p),
matched AS (
  SELECT q.search_county, q.lr, q.d, q.v, q.n, e.c, e.t, e.v ev, e.n en, e.lng, e.lat, e.source_rows
  FROM queries q JOIN fill_doors e
    ON e.lr = q.lr AND e.d = q.d AND (q.v IS NULL OR e.v = q.v) AND (q.n IS NULL OR e.n = q.n)
   AND (q.search_county IS NULL OR e.c = q.search_county)),
answers AS (
  SELECT search_county, lr, d, v, n, count(DISTINCT t) town_count, count(DISTINCT (lng, lat)) coordinate_count,
         min(t) first_town,
         to_json(list(struct_pack(county_code := c, town_code := t, village := ev, neighborhood := en,
                                  lng := lng, lat := lat, source_rows := source_rows) ORDER BY t, ev, en, lng, lat))::VARCHAR candidates
  FROM matched GROUP BY ALL)
SELECT p.i, p.search_county, p.source_county, coalesce(a.town_count, 0), coalesce(a.coordinate_count, 0),
       a.first_town, coalesce(a.candidates, '[]')
FROM p LEFT JOIN answers a
  ON a.search_county IS NOT DISTINCT FROM p.search_county AND a.lr = p.lr AND a.d = p.d
 AND a.v IS NOT DISTINCT FROM p.v AND a.n IS NOT DISTINCT FROM p.n
ORDER BY p.i
"""


def family(key):
    value = json.loads(key[3:])
    road = re.split(r"(?=[0-9一二三四五六七八九十百]+巷)", value["locality_road"])[0]
    return digest([value["county"], value["town"], road])


def build_pool(
    converted: Path,
    address_dir: Path,
    descriptor: dict,
    work_dir: Path,
    *,
    index_path=None,
    run_id=None,
):
    manifest, previous = load_snapshot(converted, "converted")
    names = load_pinned_source(Path(address_dir), descriptor)
    input_hashes = [sha256_file(Path(converted) / "manifest.json"), digest(descriptor)]
    probe_db = None
    roads = {}
    if index_path:
        from .results.reconcile import load_p2
        from .offline.resolve import resolve

        _, index_report = load_p2(index_path, "offline-index")
        if index_report["address_source_commit"] != descriptor["commit"]:
            raise ValueError("Candidate evidence pin mismatch")
        input_hashes.append(sha256_file(Path(index_path) / "manifest.json"))
        probe_db = duckdb.connect(config=duckdb_config())
        probe_db.read_parquet(
            str(Path(index_path) / "offline_rows.parquet")
        ).create_view("evidence")
        # R04-12: candidates are matched by door signature and the written village and
        # neighbourhood, as in resolve_offline.
        probe_db.execute(
            f"""CREATE TEMP TABLE doors AS SELECT {door_signature_sql('building_key')} sig,
                   {sub_identity_sql('building_key', 'village')} v,
                   {sub_identity_sql('building_key', 'neighborhood')} n, lng, lat
               FROM evidence WHERE validity='valid' AND building_key IS NOT NULL ORDER BY sig"""
        )
        for item in descriptor["roads"]:
            county, road = item["path"].split("/")[1][:-4].split("-", 1)
            roads.setdefault(county, set()).add(road)
    binding = bindings(
        "address-pool",
        {
            "address_commit": descriptor["commit"],
            "coordinate_confirmed_candidates": bool(index_path),
            "district_fill_rule": DISTRICT_FILL_RULE if index_path else None,
            "normalization_version": identity.NORMALIZATION_VERSION,
            "repeated_county_rule": REPEATED_COUNTY_RULE,
        },
        input_hashes,
    )
    stage = Stage(Path(work_dir) / "address-pool", "address-pool", binding, run_id)
    if stage.reused:
        return stage.path
    occurrence = BatchWriter(
        stage.build / "address_occurrences.parquet", "address-occurrence"
    )
    candidates = BatchWriter(
        stage.build / "district_candidates.parquet", "district-candidate"
    )
    pending = []
    fill_counts = {}
    observation_paths = [
        str(Path(converted) / x["path"])
        for x in manifest["artifacts"]
        if x["schema"] == "observation"
    ]
    try:
        for item in manifest["artifacts"]:
            if item["schema"] != "address-component":
                continue
            for row in rows(Path(converted) / item["path"]):
                address, county, town = names.canonicalize(row["normalized_address"])
                key = building_key_v2(address) if county and town else None
                reason = (
                    "valid"
                    if key
                    else ("invalid_admin" if not county else "address_review")
                )
                if probe_db and not county:
                    query = district_query(address)
                    if query is not None:
                        # Decided after all rows are read, in one lookup (R05-7).
                        pending.append((row["component_id"], row["raw_record_id"], row["building_key"],
                                        address, *query))
                        continue
                if (
                    probe_db
                    and county
                    and any(c == "?" or "\ue000" <= c <= "\uf8ff" for c in address)
                ):

                    def probe(candidate):
                        candidate_key = building_key_v2(
                            names.canonicalize(candidate)[0]
                        )
                        if candidate_key is None:
                            return False
                        signature, village, neighborhood = door_parts(candidate_key)
                        return (
                            probe_db.execute(
                                "SELECT count(*) FROM (SELECT lng,lat FROM doors WHERE sig=? "
                                "AND (?::VARCHAR IS NULL OR v=?) AND (?::VARCHAR IS NULL OR n=?) GROUP BY lng,lat)",
                                [signature, village, village, neighborhood, neighborhood],
                            ).fetchone()[0]
                            == 1
                        )

                    tier, candidate, _ = resolve(
                        address, roads.get(county, set()), {}, probe
                    )
                    if tier == "tier1_apply":
                        address, county, town = names.canonicalize(candidate)
                        key = building_key_v2(address)
                        reason = "coordinate_confirmed_candidate"
                    else:
                        key = None
                        reason = "candidate_review"
                occurrence.add(
                    {
                        "component_id": row["component_id"],
                        "raw_record_id": row["raw_record_id"],
                        "key_version": "v2",
                        "building_key": key,
                        "original_key": row["building_key"],
                        "normalized_address": address,
                        "county_code": county,
                        "town_code": town,
                        "reason": reason,
                    }
                )
        if pending:
            fill_counts = _fill_districts(probe_db, pending, names, observation_paths, occurrence, candidates)
    finally:
        occurrence.close()
        candidates.close()
        if probe_db:
            probe_db.close()
    pool = BatchWriter(stage.build / "unique_addresses.parquet", "address-pool")
    with tempfile.TemporaryDirectory(prefix="pool-", dir=stage.build) as spill:
        with duckdb.connect(
            config=duckdb_config(spill)
        ) as db:
            db.read_parquet(str(occurrence.path)).create_view("occurrence")
            db.read_parquet(observation_paths).create_view("observations")
            # R05-7: a filled-in occurrence never changes the representative address of a key
            # that other occurrences already give.
            query = """SELECT a.building_key,coalesce(min(a.normalized_address) FILTER (WHERE a.reason NOT IN
     ('road_unique_in_county','road_only_unique')),min(a.normalized_address)),min(a.county_code),min(a.town_code),
     count(*),min(o.tx_yyyymm),max(o.tx_yyyymm)
     FROM occurrence a JOIN observations o USING(raw_record_id)
     WHERE a.building_key IS NOT NULL GROUP BY a.building_key ORDER BY a.building_key"""
            cursor = db.execute(query)
            while batch := cursor.fetchmany(1024):
                for key, address, county, town, count, first, last in batch:
                    pool.add(
                        {
                            "key_version": "v2",
                            "building_key": key,
                            "canonical_address": address,
                            "county_code": county,
                            "town_code": town,
                            "address_family": family(key),
                            "record_count": count,
                            "first_seen": first,
                            "last_seen": last,
                        }
                    )
    pool.close()
    load_pinned_source(Path(address_dir), descriptor)
    report = {
        "converted_snapshot_sha256": binding["input_sha256"][0],
        "address_source_commit": descriptor["commit"],
        "batches": previous["batches"],
        "source_sha256": previous["source_sha256"],
        "record_grain": "source_observation",
        "occurrence_rows": occurrence.row_count,
        "pool_rows": pool.row_count,
        "district_fill_rule": DISTRICT_FILL_RULE if index_path else None,
        "district_fill": fill_counts,
        "district_candidate_rows": candidates.row_count,
        "dataset_counts": {
            "address-occurrence": occurrence.row_count,
            "address-pool": pool.row_count,
            "district-candidate": candidates.row_count,
        },
    }
    return stage.finish(
        [
            (
                "address_occurrences.parquet",
                occurrence.path,
                "address-occurrence",
                occurrence.row_count,
            ),
            ("unique_addresses.parquet", pool.path, "address-pool", pool.row_count),
            (
                "district_candidates.parquet",
                candidates.path,
                "district-candidate",
                candidates.row_count,
            ),
        ],
        report,
    )


def _fill_districts(db, pending, names, observation_paths, occurrence, candidates) -> dict:
    """Look up every pending component at once and write its occurrence and candidate rows."""
    db.execute(
        """CREATE TEMP TABLE fill_doors AS
           SELECT county_code c, town_code t,
                  json_extract_string(substr(building_key, 4), '$.locality_road') lr,
                  json_extract_string(substr(building_key, 4), '$.door') d,
                  json_extract_string(substr(building_key, 4), '$.village') v,
                  json_extract_string(substr(building_key, 4), '$.neighborhood') n,
                  lng, lat, count(*) source_rows
           FROM evidence WHERE validity = 'valid' AND building_key IS NOT NULL GROUP BY ALL"""
    )
    letters = county_letter_codes()
    db.register("letters", pa.table({"letter": list(letters), "county_code": list(letters.values())}))
    db.read_parquet(observation_paths).create_view("fill_observations")
    names_ = ["i", "raw_record_id", "county", "lr", "d", "v", "n"]
    values = {name: [] for name in names_}
    for i, item in enumerate(pending):
        for name, value in zip(names_, (i, item[1], item[4], item[6], item[7], item[8], item[9])):
            values[name].append(value)
    db.register("pending", pa.table(values, schema=pa.schema(
        [("i", pa.int64())] + [(name, pa.string()) for name in names_[1:]])))
    counts = {reason: 0 for reason in ("road_unique_in_county", "road_only_unique", "district_ambiguous",
                                       "district_missing", "road_only_not_unique", "invalid_admin")}
    counts["fill_key_mismatch"] = 0
    counts["cross_check_candidates"] = 0
    cursor = db.execute(_FILL_SQL)
    while batch := cursor.fetchmany(4096):
        for i, search_county, source_county, town_count, coordinate_count, first_town, found in batch:
            component_id, raw_record_id, original_key, address, county, rest, lr, door, village, neighborhood = pending[i]
            road_only = county is None
            key, filled, town = None, address, ""
            if town_count == 1:
                candidate, county_found, town_found = names.canonicalize(names.current[first_town] + rest)
                key = building_key_v2(candidate) if town_found == first_town else None
                parts = door_parts(key) if key else None
                if parts and parts[0].split("\x1f")[2:] == [lr, door] and parts[1:] == (village, neighborhood):
                    filled, county, town = candidate, county_found, town_found
                    reason = "road_only_unique" if road_only else "road_unique_in_county"
                else:
                    key = None
                    counts["fill_key_mismatch"] += 1
            if key is None:
                if town_count > 1:
                    reason = "road_only_not_unique" if road_only else "district_ambiguous"
                    counts["cross_check_candidates"] += coordinate_count > 1
                elif town_count == 0 and _ADMIN_START.match(rest):
                    reason = "invalid_admin"
                else:
                    reason = "road_only_not_unique" if road_only else "district_missing"
            counts[reason] += 1
            occurrence.add(
                {
                    "component_id": component_id,
                    "raw_record_id": raw_record_id,
                    "key_version": "v2",
                    "building_key": key,
                    "original_key": original_key,
                    "normalized_address": filled,
                    "county_code": county or "",
                    "town_code": town,
                    "reason": reason,
                }
            )
            candidates.add(
                {
                    "component_id": component_id,
                    "raw_record_id": raw_record_id,
                    "reason": reason,
                    "search_scope": "county" if not road_only else "source_county" if search_county else "nationwide",
                    "search_county_code": search_county,
                    "source_county_code": source_county,
                    "town_count": town_count,
                    "coordinate_count": coordinate_count,
                    "candidates_json": found,
                }
            )
    counts["lookups"] = len(pending)
    return counts
