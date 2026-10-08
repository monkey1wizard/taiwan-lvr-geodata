"""Resolve only exact verified coordinates; retain conflicts and all unresolved keys."""

from pathlib import Path
import json
import tempfile

import duckdb

from ..storage.parquet import BatchWriter, duckdb_config
from ..storage.runs import Stage, bindings, digest
from ..storage.runs import SnapshotStore
from ..sources import sha256_file


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
    run_id=None,
):
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
        {"coordinate_equality": "exact_float64", "prior_state": bool(prior_state)},
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
    statuses = {
        name: 0 for name in ["located", "conflict", "unmatched", "outside_scope"]
    }
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
            columns = [x[0] for x in cursor.description]
            while batch := cursor.fetchmany(1024):
                for values in batch:
                    evidence.add(dict(zip(columns, values)))
            evidence.close()
            query = """WITH coordinates AS (
      SELECT building_key,lng,lat,min(evidence_id) evidence_id,count(*) evidence_count
      FROM evidence WHERE validity='valid' GROUP BY building_key,lng,lat),
     summaries AS (SELECT building_key,count(*) coordinate_count,sum(evidence_count) evidence_count,
       min(lng) lng,min(lat) lat,min(evidence_id) evidence_id FROM coordinates GROUP BY building_key)
     SELECT p.key_version,p.building_key,p.canonical_address,p.county_code,p.town_code,p.address_family,
       coalesce(s.coordinate_count,0),coalesce(s.evidence_count,0),s.lng,s.lat,s.evidence_id
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
                    lng,
                    lat,
                    eid,
                ) in batch:
                    status = (
                        "located"
                        if count == 1
                        else "conflict"
                        if count > 1
                        else "unmatched"
                        if county in index_report["address_counties"]
                        else "outside_scope"
                    )
                    statuses[status] += 1
                    if status != "located":
                        lng = lat = None
                        eid = None
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
    aliases = BatchWriter(stage.build / "verified_aliases.parquet", "verified-alias")
    aliases.close()
    ledger = BatchWriter(stage.build / "tgos_results.parquet", "tgos-ledger")
    ledger.close()
    report = {
        **pool_report,
        "index_manifest_sha256": hashes[1],
        "status_counts": statuses,
        "tgos_started": False,
        "offline_source": index_report,
        "dataset_counts": {
            "address-result": result.row_count,
            "offline-row": evidence.row_count,
            "verified-alias": 0,
            "tgos-ledger": 0,
            "unmatched-address": unmatched.row_count,
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
