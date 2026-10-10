"""TGOS result validation: coordinates, address matching, and resolution rebuild."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from ..addresses.identity import building_key_v2, door_parts, same_door
from ..contracts.validate import within_taiwan_bounds
from ..results.reconcile import (
    DOOR_CROSS_CHECK,
    EXCEEDS_TOLERANCE,
    WITHIN_TOLERANCE,
    check_suspended_counties,
    new_suspension_counts,
    resolution_row,
    suspended_row,
)
from ..storage.parquet import rows
from ..storage.runs import digest, sha256_file
from ..transactions.normalize import normalize_address
from .batches import (
    UNCARRIED,
    _artifact_paths,
    _require_latest,
    _table,
    _write_state,
    load_state,
)


def _tgos_coordinate(x: str, y: str):
    if not x.strip() and not y.strip():
        return None
    if not x.strip() or not y.strip():
        raise ValueError("Partial TGOS coordinate pair")
    lng, lat = float(x), float(y)
    if not within_taiwan_bounds(lng, lat):
        raise ValueError("TGOS coordinate is outside declared WGS84 Taiwan bounds")
    return lng, lat


def _rebuild_resolution(
    pool_rows: list[dict],
    evidence_rows: list[dict],
    source_commit: str,
    tolerance_m: float | None = None,
    door_rule: str | None = None,
    suspension: dict | None = None,
    suspension_counts: dict | None = None,
):
    """Rebuild address status with the offline adoption rule (R05-4).

    ``tolerance_m`` is the state's ``coordinate_tolerance_m``. States built
    before R05-4 have none; for them several coordinates stay a conflict and
    no coordinate-resolution rows are returned (None).

    ``door_rule`` is the state's ``door_rule`` (R04-12). With it, each address
    row's door (village, neighbourhood) comes from the key of its own address
    text, and coordinates from several doors are not adopted. TGOS results name
    the queried key and add no door. States built before R04-12 have none and
    keep the earlier behaviour.

    ``suspension`` is the state's ``address_suspension`` (R05-9). A key of a
    listed county with any coordinate, TGOS results included, is not located
    (basis ``address_source_suspended``). ``suspension_counts``, when given, is
    filled per county code. States built before R05-9 have none.
    """
    suspended_codes = (
        {item["code"] for item in check_suspended_counties(suspension["counties"])}
        if suspension is not None
        else set()
    )
    tgos_keys = set()
    coordinates = {}
    doors = {}
    door_cache = {}
    for row in evidence_rows:
        if row["validity"] == "valid" and row["building_key"]:
            point = (row["lng"], row["lat"])
            coordinates.setdefault(row["building_key"], {}).setdefault(
                point, []
            ).append(row["evidence_id"])
            if row["source_kind"] == "tgos_result":
                tgos_keys.add(row["building_key"])
            if door_rule is not None:
                found = doors.setdefault(row["building_key"], {}).setdefault(point, set())
                if row["source_kind"] != "tgos_result":
                    text = row["normalized_address"]
                    if text not in door_cache:
                        own = building_key_v2(text)
                        door_cache[text] = door_parts(own)[1:] if own else (None, None)
                    found.add(door_cache[text])
    address_rows = []
    unmatched = []
    resolutions = [] if tolerance_m is not None else None
    statuses = {name: 0 for name in ["located", "conflict", "unmatched", "outside_scope"]}
    for row in pool_rows:
        points = coordinates.get(row["building_key"], {})
        count = len(points)
        point, evidence_id = (None, None), None
        suspended = row["county_code"] in suspended_codes
        if suspended and suspension_counts is not None:
            counted = suspension_counts[row["county_code"]]
            counted["keys"] += 1
            counted["with_coordinates" if count else "without_coordinates"] += 1
            counted["tgos_evidence_keys"] += row["building_key"] in tgos_keys
        if count >= 1 and suspended and tolerance_m is not None:
            # R05-9: never located, TGOS results included; coordinates stay in the evidence.
            detail, would_be = suspended_row(
                row["key_version"],
                row["building_key"],
                {
                    key: {
                        "evidence_count": len(ids),
                        "evidence_id": min(ids),
                        **(
                            {"doors": sorted(doors[row["building_key"]][key], key=str)}
                            if door_rule is not None
                            else {}
                        ),
                    }
                    for key, ids in points.items()
                },
                tolerance_m,
            )
            resolutions.append(detail)
            status = "conflict"
            if suspension_counts is not None:
                suspension_counts[row["county_code"]]["would_be_located"] += would_be == "located"
        elif count == 1:
            # The only distinct coordinate, not a first-row pick.
            ((point, ids),) = points.items()
            status, evidence_id = "located", min(ids)
        elif count > 1 and tolerance_m is not None:
            detail = resolution_row(
                row["key_version"],
                row["building_key"],
                {
                    key: {
                        "evidence_count": len(ids),
                        "evidence_id": min(ids),
                        **(
                            {"doors": sorted(doors[row["building_key"]][key], key=str)}
                            if door_rule is not None
                            else {}
                        ),
                    }
                    for key, ids in points.items()
                },
                tolerance_m,
            )
            resolutions.append(detail)
            status = detail["status"]
            point, evidence_id = (detail["lng"], detail["lat"]), detail["evidence_id"]
        else:
            status = "conflict" if count > 1 else "unmatched"
        result = {
            "key_version": row["key_version"],
            "building_key": row["building_key"],
            "canonical_address": row["canonical_address"],
            "county_code": row["county_code"],
            "town_code": row["town_code"],
            "address_family": row["address_family"],
            "status": status,
            "lng": point[0],
            "lat": point[1],
            "evidence_id": evidence_id,
            "evidence_count": sum(len(v) for v in points.values()),
            "coordinate_count": count,
            "source_commit": source_commit,
        }
        statuses[status] += 1
        address_rows.append(result)
        if status != "located":
            unmatched.append(result)
    return address_rows, unmatched, statuses, resolutions


def import_tgos(
    source: Path,
    work_dir: Path,
    batch_id: str,
    response: Path,
    *,
    run_id: str | None = None,
):
    manifest, report, stage_name = load_state(source)
    _require_latest(source, stage_name)
    paths = _artifact_paths(source, manifest)
    batches = _table(paths.get("tgos-batch"))
    queries = _table(paths.get("tgos-query"))
    results = _table(paths.get("tgos-result"))
    aliases = _table(paths.get("verified-alias"))
    events = _table(paths.get("alias-event"))
    batch = next((row for row in batches if row["batch_id"] == batch_id), None)
    if not batch or batch["status"] not in {"submitted", "submission_unknown", "completed"}:
        raise ValueError("TGOS batch is not importable")
    response_hash = sha256_file(response)
    if batch["status"] == "completed" and batch["response_sha256"] == response_hash:
        return Path(source)
    submitted = {row["address"]: row for row in queries if row["batch_id"] == batch_id}
    # R09-0: queries set aside for having no key were sent too; their rows are not imported.
    uncarried = {
        row["address"] for row in report.get(UNCARRIED, []) if row["batch_id"] == batch_id
    }
    if len(submitted) + len(uncarried) != batch["address_count"] or uncarried & set(submitted):
        raise ValueError("TGOS submitted Address values are not one-to-one")
    with Path(response).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"Address", "Response_Address", "Response_X", "Response_Y"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("TGOS response columns missing")
        returned = list(reader)
    addresses = [row["Address"] for row in returned]
    if len(addresses) != len(set(addresses)):
        raise ValueError("Duplicate Address in TGOS response")
    if set(addresses) != set(submitted) | uncarried:
        raise ValueError("TGOS response Address set differs from submitted batch")
    evidence = _table(paths["offline-row"])
    prior_coordinates = {
        row["building_key"]: (row["lng"], row["lat"])
        for row in rows(paths["address-result"])
        if row["status"] == "located"
    }
    new_results = []
    result_by_query = {}
    for line, row in enumerate(returned, 2):
        if row["Address"] in uncarried:
            continue
        query = submitted[row["Address"]]
        reason = None
        try:
            coordinate = _tgos_coordinate(row["Response_X"], row["Response_Y"])
            status = "succeeded" if coordinate else "failed"
            if coordinate and not row["Response_Address"].strip():
                raise ValueError("Successful TGOS row lacks Response_Address")
            if coordinate:
                response_address = normalize_address(row["Response_Address"])
                target_key = building_key_v2(response_address)
                if not target_key:
                    raise ValueError("TGOS response is not a complete address")
                # A success flag or a shared district does not prove the same
                # door number; only the same door is adopted. R04-12: a village or
                # neighbourhood the query writes must not differ in the response.
                if not same_door(query["building_key"], target_key):
                    raise ValueError("TGOS response address differs from submitted address")
        except (TypeError, ValueError) as exc:
            coordinate = None
            status = "rejected"
            reason = str(exc)
        result_id = digest([batch_id, query["query_fingerprint"], response_hash, line])
        result = {
            "result_id": result_id,
            "batch_id": batch_id,
            "query_fingerprint": query["query_fingerprint"],
            "address": row["Address"],
            "response_address": row["Response_Address"].strip() or None,
            "lng": coordinate[0] if coordinate else None,
            "lat": coordinate[1] if coordinate else None,
            "status": status,
            "response_sha256": response_hash,
            "source_row_number": line,
            "reason": reason,
        }
        new_results.append(result)
        result_by_query[query["query_fingerprint"]] = result
        if status == "succeeded":
            evidence_id = digest(["tgos", result_id])
            evidence.append(
                {
                    "evidence_id": evidence_id,
                    "key_version": "v2",
                    "building_key": query["building_key"],
                    "normalized_address": query["address"],
                    "county_code": query["county_code"],
                    "town_code": json.loads(query["building_key"][3:])["town"],
                    "lng": coordinate[0],
                    "lat": coordinate[1],
                    "validity": "valid",
                    "source_kind": "tgos_result",
                    "source_ref": batch_id,
                    "input_sha256": response_hash,
                    "source_row_number": line,
                }
            )
    results.extend(new_results)
    for row in queries:
        result = result_by_query.get(row["query_fingerprint"])
        if result:
            if result["status"] == "succeeded" and row["building_key"] in prior_coordinates and prior_coordinates[row["building_key"]] != (result["lng"], result["lat"]):
                row["status"] = "conflict"
            else:
                row["status"] = result["status"]
            row["result_id"] = result["result_id"]
    batch["status"] = "completed"
    batch["response_sha256"] = response_hash
    pool = _table(paths["address-pool"])
    suspension = report.get("address_suspension")
    suspension_counts = (
        new_suspension_counts(suspension["counties"]) if suspension is not None else None
    )
    address_rows, unmatched, statuses, resolutions = _rebuild_resolution(
        pool,
        evidence,
        report["address_source_commit"],
        report.get("coordinate_tolerance_m"),
        report.get("door_rule"),
        suspension,
        suspension_counts,
    )
    before = report["status_counts"]
    return _write_state(
        source,
        work_dir,
        action="import",
        batches=batches,
        queries=queries,
        results=results,
        aliases=sorted(aliases, key=lambda x: x["alias_key"]),
        alias_events=events,
        address_rows=address_rows,
        evidence_rows=evidence,
        unmatched_rows=unmatched,
        resolution_rows=resolutions,
        extra_report={
            "last_tgos_batch_id": batch_id,
            "last_tgos_response_sha256": response_hash,
            **(
                {
                    "tgos_uncarried_response_rows": {
                        **report.get("tgos_uncarried_response_rows", {}),
                        batch_id: len(uncarried),
                    }
                }
                if uncarried
                else {}
            ),
            "status_counts_before_import": before,
            "status_counts": statuses,
            **(
                {"address_suspension_counts": suspension_counts}
                if suspension_counts is not None
                else {}
            ),
            **(
                {
                    "coordinate_resolution_basis_counts": {
                        basis: sum(r["resolution_basis"] == basis for r in resolutions)
                        for basis in (WITHIN_TOLERANCE, EXCEEDS_TOLERANCE)
                    },
                    **(
                        {
                            "door_cross_check_keys": sum(
                                r["resolution_basis"] == DOOR_CROSS_CHECK for r in resolutions
                            )
                        }
                        if report.get("door_rule") is not None
                        else {}
                    ),
                }
                if resolutions is not None
                else {}
            ),
        },
        run_id=run_id,
    )
