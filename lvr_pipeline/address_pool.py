"""Disk-backed unique address pool with separate, bounded occurrence rows."""

from pathlib import Path
import json
import tempfile
import re

import duckdb

from .address import building_key_v2
from .offline_lookup import load_pinned_source
from .storage.parquet import BatchWriter, duckdb_config, rows
from .storage.runs import Stage, bindings, digest, load_snapshot
from .sources import sha256_file


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
        from .address_state import load_p2
        from .garbled_resolve import resolve

        _, index_report = load_p2(index_path, "offline-index")
        if index_report["address_source_commit"] != descriptor["commit"]:
            raise ValueError("Candidate evidence pin mismatch")
        input_hashes.append(sha256_file(Path(index_path) / "manifest.json"))
        probe_db = duckdb.connect(config=duckdb_config())
        probe_db.read_parquet(
            str(Path(index_path) / "offline_rows.parquet")
        ).create_view("evidence")
        for item in descriptor["roads"]:
            county, road = item["path"].split("/")[1][:-4].split("-", 1)
            roads.setdefault(county, set()).add(road)
    binding = bindings(
        "address-pool",
        {
            "address_commit": descriptor["commit"],
            "coordinate_confirmed_candidates": bool(index_path),
        },
        input_hashes,
    )
    stage = Stage(Path(work_dir) / "address-pool", "address-pool", binding, run_id)
    if stage.reused:
        return stage.path
    occurrence = BatchWriter(
        stage.build / "address_occurrences.parquet", "address-occurrence"
    )
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
                if (
                    probe_db
                    and county
                    and any(c == "?" or "\ue000" <= c <= "\uf8ff" for c in address)
                ):

                    def probe(candidate):
                        candidate_key = building_key_v2(
                            names.canonicalize(candidate)[0]
                        )
                        return (
                            probe_db.execute(
                                "SELECT count(*) FROM (SELECT lng,lat FROM evidence WHERE building_key=? AND validity='valid' GROUP BY lng,lat)",
                                [candidate_key],
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
    finally:
        occurrence.close()
        if probe_db:
            probe_db.close()
    pool = BatchWriter(stage.build / "unique_addresses.parquet", "address-pool")
    observation_paths = [
        str(Path(converted) / x["path"])
        for x in manifest["artifacts"]
        if x["schema"] == "observation"
    ]
    with tempfile.TemporaryDirectory(prefix="pool-", dir=stage.build) as spill:
        with duckdb.connect(
            config=duckdb_config(spill)
        ) as db:
            db.read_parquet(str(occurrence.path)).create_view("occurrence")
            db.read_parquet(observation_paths).create_view("observations")
            query = """SELECT a.building_key,min(a.normalized_address),min(a.county_code),min(a.town_code),
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
        "dataset_counts": {
            "address-occurrence": occurrence.row_count,
            "address-pool": pool.row_count,
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
        ],
        report,
    )
