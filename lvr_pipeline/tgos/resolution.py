"""TGOS evidence and the merged address state, rebuilt in memory from an offline state and a ledger."""

from __future__ import annotations

import json

from ..addresses.identity import building_key, door_parts
from ..results.reconcile import (
    check_suspended_counties,
    new_suspension_counts,
    resolution_row,
    suspended_row,
)
from ..storage.parquet import rows
from ..storage.runs import digest


def tgos_evidence_row(query: dict, result: dict) -> dict:
    """The offline-row evidence of one succeeded door-level TGOS result."""
    return {
        "evidence_id": digest(["tgos", result["result_id"]]),
        "key_version": "v2",
        "building_key": query["building_key"],
        "normalized_address": query["address"],
        "county_code": query["county_code"],
        "town_code": json.loads(query["building_key"][3:])["town"],
        "lng": result["lng"],
        "lat": result["lat"],
        "validity": "valid",
        "source_kind": "tgos_result",
        "source_ref": result["batch_id"],
        "input_sha256": result["response_sha256"],
        "source_row_number": result["source_row_number"],
    }


def evidence_from_results(results: list[dict], queries: list[dict]) -> list[dict]:
    """The evidence rows every succeeded door-level result in the ledger stands for, in result order."""
    by_query = {(q["batch_id"], q["query_fingerprint"]): q for q in queries}
    found = []
    for result in results:
        query = by_query[(result["batch_id"], result["query_fingerprint"])]
        if result["status"] == "succeeded" and query["query_kind"] == "building_key":
            found.append(tgos_evidence_row(query, result))
    return found


def district_fill_of(query: dict, result: dict):
    """(filled address, building_key) a succeeded district-fill result stands for, else None.

    The key is that of the queried text with the answered town after its county (village and
    neighbourhood come from the query only), the key the address pool gives the filled occurrences.
    """
    from ..addresses.pool import district_fill_key

    if query["query_kind"] != "district_missing" or result["status"] != "succeeded" or not result["response_building_key"]:
        return None
    return district_fill_key(query["county_code"], query["address"], json.loads(result["response_building_key"][3:])["town"])


def district_fill_evidence(results: list[dict], queries: list[dict], pool_keys) -> list[dict]:
    """The evidence rows of succeeded district-fill results whose filled key is a pool key (E-00).

    ``pool_keys`` is a callable giving the building keys of the address pool; it is called only when a
    district-fill result exists. A fill whose key the pool does not hold (the pool was built without
    this ledger) adds nothing. The row is a TGOS evidence row of the filled key, mapped by the query's
    fingerprint, not by the key of the TGOS response address.
    """
    by_query = {(q["batch_id"], q["query_fingerprint"]): q for q in queries}
    fills = []
    for result in results:
        query = by_query[(result["batch_id"], result["query_fingerprint"])]
        found = district_fill_of(query, result)
        if found is not None:
            fills.append((query, result, *found))
    if not fills:
        return []
    known = set(pool_keys())
    return [
        {
            "evidence_id": digest(["tgos", result["result_id"]]),
            "key_version": "v2",
            "building_key": key,
            "normalized_address": address,
            "county_code": query["county_code"],
            "town_code": json.loads(key[3:])["town"],
            "lng": result["lng"],
            "lat": result["lat"],
            "validity": "valid",
            "source_kind": "tgos_result",
            "source_ref": result["batch_id"],
            "input_sha256": result["response_sha256"],
            "source_row_number": result["source_row_number"],
        }
        for query, result, address, key in fills
        if key in known
    ]


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
                        own = building_key(text)
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


def rebuild(paths: dict, report: dict, evidence: list[dict], pool: list[dict] | None = None):
    """Rebuild the address status of an offline state from ``evidence`` (offline rows plus TGOS rows).

    Returns (address rows, unmatched rows, status counts, resolution rows, suspension counts). ``paths``
    and ``report`` are those of the offline state; the pool is read from it unless given.
    """
    if pool is None:
        pool = list(rows(paths["address-pool"]))
    suspension = report.get("address_suspension")
    counts = new_suspension_counts(suspension["counties"]) if suspension is not None else None
    address_rows, unmatched, statuses, resolutions = _rebuild_resolution(
        pool,
        evidence,
        report["address_source_commit"],
        report.get("coordinate_tolerance_m"),
        report.get("door_rule"),
        suspension,
        counts,
    )
    return address_rows, unmatched, statuses, resolutions, counts


def ledger_evidence(paths: dict, results: list[dict], queries: list[dict]) -> list[dict]:
    """Every evidence row the ledger stands for: succeeded door-level results and district fills (c55).

    ``paths`` are the artifact paths of the offline state; its address pool decides which fills count.
    """
    return evidence_from_results(results, queries) + district_fill_evidence(
        results, queries, lambda: (row["building_key"] for row in rows(paths["address-pool"])))


def current_address_rows(paths: dict, report: dict, results: list[dict], queries: list[dict]):
    """The address status rows of the offline state after every succeeded result of the ledger.

    A key located by a district fill is located here too (c55), so prepare-tgos does not select it
    and an import sees its coordinate. When the ledger adds no evidence the offline rows are the
    answer and are streamed from the state.
    """
    adopted = ledger_evidence(paths, results, queries)
    if not adopted:
        return rows(paths["address-result"])
    evidence = list(rows(paths["offline-row"])) + adopted
    return rebuild(paths, report, evidence)[0]
