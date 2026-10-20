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

A ``district_missing`` address whose query was answered by TGOS inside its candidate towns is
filled from that answer when a TGOS ledger is given (``tgos_district_fill``; see
``adopted_district_fills``). Where the key is located is then decided by resolve-offline with
the door rule of R04-12, and for such a key by the tgos-state stage from the answer's coordinate.
Candidates are never chosen by road-name similarity, and the transaction text is never
changed; the filled address is kept only in the occurrence row. A component whose text after
the county starts like an administrative name (市東區…, 桃園縣…) and has no candidate stays
``invalid_admin``. ``district_candidates.parquet`` records every lookup and its candidates.
"""

from pathlib import Path
import csv
import json
import re

import duckdb
import pyarrow as pa

from . import admin, identity
from .identity import building_key, door_parts, door_signature_sql, sub_identity_sql
from .parse import _COUNTY_CODE
from ..offline.index import load_pinned_source
from ..storage.parquet import BatchWriter, duckdb_config, rows
from ..storage.spill import measured_spill
from ..storage.runs import Stage, bindings, digest, load_snapshot
from ..storage.runs import ROOT, sha256_file

DISTRICT_FILL_RULE = "road_unique_v1"
REPEATED_COUNTY_RULE = "exact_repeat_v1"
COUNTY_LETTERS = ROOT / "config" / "reference" / "lvr_county_letters.csv"
TGOS_FILL_REASON = "tgos_district_fill"
FILLED_REASONS = ("road_unique_in_county", "road_only_unique", TGOS_FILL_REASON)
_COUNTY_START = re.compile("^(" + "|".join(sorted(_COUNTY_CODE, key=len, reverse=True)) + ")")
# Text that starts like a county or town name (市東區…, v新興區…, 桃園縣…); used only to label a
# lookup without candidates as invalid_admin, never to match.
_ADMIN_START = re.compile(r"^[^0-9路街道段巷弄號村里鄰]{0,4}?[縣市區鄉鎮]")


def district_query(address: str):
    """Door parts of an address whose town was not found.

    Returns (county code or None, text after the county, locality_road, door, village,
    neighbourhood), or None when no door can be parsed. A real town of that county stands in for
    the missing one only to split the text with building_key (the town must exist in the county);
    the stand-in never reaches a key. Without a county, 臺北市 is used for the split.
    """
    match = _COUNTY_START.match(address)
    county = _COUNTY_CODE[match.group(1)] if match else None
    rest = address[match.end():] if match else address
    stand_in = max(admin.load_admin_towns()["counties"][county or "63"], key=lambda t: (len(t), t))
    key = building_key((match.group(1) if match else "臺北市") + stand_in + rest)
    if key is None:
        return None
    value = json.loads(key[3:])
    if value["town"] != stand_in:
        return None
    return county, rest, value["locality_road"], value["door"], value.get("village"), value.get("neighborhood")


def district_fill_key(county_code: str, address: str, town: str):
    """(filled address, building_key) of a district_missing address once ``town`` follows its county.

    Village and neighbourhood come only from ``address`` (the transaction text or the query text
    built from it), never from a TGOS answer. None when the filled text does not keep the written
    road, door, village and neighbourhood under the given town, or the county differs.
    """
    parsed = district_query(address)
    if parsed is None or parsed[0] != county_code:
        return None
    _, rest, locality_road, door, village, neighborhood = parsed
    filled = admin.county_name(county_code) + town + rest
    key = building_key(filled)
    if key is None:
        return None
    value = json.loads(key[3:])
    if value["county"] != county_code or (
        value["town"] != town and value["town"] not in admin.town_aliases(county_code, town)
    ):
        return None
    signature, key_village, key_neighborhood = door_parts(key)
    if signature.split("\x1f")[2:] != [locality_road, door] or (key_village, key_neighborhood) != (village, neighborhood):
        return None
    return filled, key


def adopted_district_fills(ledger: Path, exchange_dir: Path):
    """District fills TGOS answered inside the rules now in force, from a TGOS ledger snapshot.

    Every district_missing query of a batch with a recorded response is checked again: the response
    file is found and hash-checked like the run-full re-import does (``ledger_responses``), and each
    row goes through the current ``adoption_response_key`` and ``_check_district_fill``. Any
    rejection leaves the address district_missing. Returns ({query fingerprint: fill}, counts)
    where a fill holds county, town (from the answer), building_key (from the query text) and the
    answer. A fingerprint answered with two different outcomes is not filled (``conflicting``).
    """
    from ..tgos.validate import (
        _check_district_fill, _tgos_coordinate, adoption_response_key, ledger_responses)

    carried, responses, ledger_sha256 = ledger_responses(ledger, exchange_dir)
    district = {}
    for query in carried.queries:
        if query["query_kind"] == "district_missing":
            district.setdefault(query["batch_id"], {})[query["address"]] = query
    counts = {"queries": sum(map(len, district.values())), "adopted": 0, "rejected": 0, "not_found": 0,
              "no_response": 0, "conflicting": 0, "ledger_manifest_sha256": ledger_sha256}
    answered = {}
    for batch_id, file, _ in responses:
        submitted = district.get(batch_id)
        if not submitted:
            continue
        with Path(file).open(encoding="utf-8-sig", newline="") as stream:
            returned = list(csv.DictReader(stream))
        seen = set()
        for row in returned:
            query = submitted.get(row.get("Address"))
            if query is None or query["query_fingerprint"] in seen:
                continue
            seen.add(query["query_fingerprint"])
            try:
                coordinate = _tgos_coordinate(row["Response_X"], row["Response_Y"])
                if coordinate is None:
                    counts["not_found"] += 1
                    continue
                if not row["Response_Address"].strip():
                    raise ValueError("Successful TGOS row lacks Response_Address")
                target = adoption_response_key(row["Response_Address"])
                if not target:
                    raise ValueError("TGOS response is not a complete address")
                _check_district_fill(query, target)
                town = json.loads(target[3:])["town"]
                filled = district_fill_key(query["county_code"], query["address"], town)
                if filled is None:
                    raise ValueError("TGOS response town gives no key for the queried door")
            except (TypeError, ValueError):
                counts["rejected"] += 1
                continue
            counts["adopted"] += 1
            answered.setdefault(query["query_fingerprint"], set()).add(
                (query["county_code"], town, filled[1], coordinate))
        counts["no_response"] += sum(
            1 for q in submitted.values() if q["query_fingerprint"] not in seen)
    for batch_id, queries in district.items():
        if batch_id not in {b for b, _, _ in responses}:
            counts["no_response"] += len(queries)
    fills = {}
    for fingerprint, outcomes in answered.items():
        if len(outcomes) > 1:
            counts["conflicting"] += 1
            continue
        ((county, town, key, _),) = outcomes
        fills[fingerprint] = {"county_code": county, "town": town, "building_key": key}
    return fills, counts


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


def lane_free_road(locality_road):
    return re.split(r"(?=[0-9一二三四五六七八九十百]+巷)", locality_road)[0]


def family(key):
    value = json.loads(key[3:])
    return digest([value["county"], value["town"], lane_free_road(value["locality_road"])])


def build_pool(
    converted: Path,
    address_dir: Path,
    descriptor: dict,
    work_dir: Path,
    *,
    index_path=None,
    run_id=None,
    tgos_ledger=None,
    exchange_dir=None,
):
    """Build the address pool; ``tgos_ledger`` and ``exchange_dir`` (data/tgos) switch on the TGOS district fill.

    With a TGOS ledger snapshot, a district_missing address whose query TGOS answered inside the rules
    in force is filled from that answer (reason ``tgos_district_fill``, ``adopted_district_fills``).
    The ledger needs the offline index. Without a ledger, or when none of its answers is adopted,
    the binding and the pool are those of a build without a ledger.
    """
    if tgos_ledger is not None and (not index_path or exchange_dir is None):
        raise ValueError("A TGOS ledger needs the offline index and the TGOS exchange directory")
    manifest, previous = load_snapshot(converted, "converted")
    names = load_pinned_source(Path(address_dir), descriptor)
    input_hashes = [sha256_file(Path(converted) / "manifest.json"), digest(descriptor)]
    probe_db = None
    roads = {}
    if index_path:
        from ..results.reconcile import load_p2
        from ..offline.resolve import resolve

        _, index_report = load_p2(index_path, "offline-index")
        if index_report["address_source_commit"] != descriptor["commit"]:
            raise ValueError("Candidate evidence pin mismatch")
        input_hashes.append(sha256_file(Path(index_path) / "manifest.json"))
        for item in descriptor["roads"]:
            county, road = item["path"].split("/")[1][:-4].split("-", 1)
            roads.setdefault(county, set()).add(road)
    fills, fill_report = {}, None
    if tgos_ledger is not None:
        fills, fill_report = adopted_district_fills(Path(tgos_ledger), Path(exchange_dir))
    parameters = {
        "address_commit": descriptor["commit"],
        "coordinate_confirmed_candidates": bool(index_path),
        "district_fill_rule": DISTRICT_FILL_RULE if index_path else None,
        "normalization_version": identity.NORMALIZATION_VERSION,
        "admin_towns_sha256": admin.admin_towns_sha256(),
        "repeated_county_rule": REPEATED_COUNTY_RULE,
    }
    if fills:
        # Digest of the sorted adopted fills; absent when none is adopted, so the binding stays unchanged.
        parameters["tgos_district_fills"] = digest(
            sorted([fingerprint, fill["town"], fill["building_key"]] for fingerprint, fill in fills.items()))
    binding = bindings("address-pool", parameters, input_hashes)
    stage = Stage(Path(work_dir) / "address-pool", "address-pool", binding, run_id)
    if stage.reused:
        return stage.path
    probe_spill = None
    if index_path:
        # R07-12i: the candidate probe database spills inside the stage build area.
        probe_spill = measured_spill(stage.build, "probe")
        probe_db = duckdb.connect(config=duckdb_config(probe_spill.__enter__().path))
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
                key = building_key(address) if county and town else None
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
                        candidate_key = building_key(
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
                        key = building_key(address)
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
            fill_counts = _fill_districts(
                probe_db, pending, names, observation_paths, occurrence, candidates, fills=fills)
    finally:
        occurrence.close()
        candidates.close()
        if probe_db:
            probe_db.close()
        if probe_spill:
            probe_spill.__exit__(None, None, None)
    pool = BatchWriter(stage.build / "unique_addresses.parquet", "address-pool")
    with measured_spill(stage.build, "pool") as spill:
        with duckdb.connect(
            config=duckdb_config(spill.path)
        ) as db:
            db.read_parquet(str(occurrence.path)).create_view("occurrence")
            db.read_parquet(observation_paths).create_view("observations")
            # R05-7: a filled-in occurrence (offline or TGOS fill) never changes the representative address
            # of a key that other occurrences already give.
            filled_reasons = ",".join(f"'{reason}'" for reason in FILLED_REASONS)
            query = f"""SELECT a.building_key,coalesce(min(a.normalized_address) FILTER (WHERE a.reason NOT IN
     ({filled_reasons})),min(a.normalized_address)),min(a.county_code),min(a.town_code),
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
        **({"tgos_district_fill": {**fill_report, "adopted_fills": len(fills),
                                   "filled_occurrences": fill_counts.get(TGOS_FILL_REASON, 0)}}
           if fill_report is not None else {}),
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


def _tgos_fill(fills, county, address, rest, locality_road, door, village, neighborhood, names):
    """(key, filled address, county code, town code) of a district_missing occurrence TGOS filled.

    None when its door-level query fingerprint has no adopted fill; "mismatch" when the filled text does
    not give the adopted key (the occurrence then stays district_missing). The fingerprint is the one
    of ``select_district_candidates``.
    """
    from ..tgos.batches import district_text_or_none

    text = district_text_or_none(county, (county, rest, locality_road, door, village, neighborhood))
    fill = fills.get(digest(["district_missing", county, text])) if text is not None else None
    if fill is None:
        return None
    found = district_fill_key(county, address, fill["town"])
    if found is None or found[1] != fill["building_key"]:
        return "mismatch"
    candidate, county_found, town_found = names.canonicalize(found[0])
    if not town_found or building_key(candidate) != found[1]:
        return "mismatch"
    return found[1], candidate, county_found, town_found


def _fill_districts(db, pending, names, observation_paths, occurrence, candidates, fills=None) -> dict:
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
    if fills:
        counts[TGOS_FILL_REASON] = 0
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
                key = building_key(candidate) if town_found == first_town else None
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
                    tgos = _tgos_fill(fills, county, address, rest, lr, door, village, neighborhood, names)                         if fills and reason == "district_missing" else None
                    if tgos == "mismatch":
                        counts["fill_key_mismatch"] += 1
                    elif tgos:
                        reason = TGOS_FILL_REASON
                        key, filled, county, town = tgos
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
