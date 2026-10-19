"""TGOS result validation: coordinates, address matching, and resolution rebuild."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import NamedTuple

from ..addresses import identity
from ..addresses.identity import building_key, door_parts
from ..addresses.pool import district_query
from ..contracts.validate import within_taiwan_bounds
from ..results.reconcile import DOOR_CROSS_CHECK, EXCEEDS_TOLERANCE, WITHIN_TOLERANCE
from ..storage.parquet import rows
from ..storage.runs import Stage, _identifier, bindings, digest, sha256_file
from ..transactions.normalize import normalize_address
from .adopt import (
    NEIGHBORHOOD,
    NEIGHBORHOOD_OUTSIDE_REASON,
    VILLAGE_AS_PLACE,
    VILLAGE_DOOR,
    BaseGroups,
    adoption_route,
    base_town,
)
from .batches import (
    DAILY_LIMIT,
    Carried,
    _artifact_paths,
    _carry_ledger,
    _source_path,
    _write_merged_state,
    _write_state,
    load_state,
    open_input,
    source_state_record,
)
from .resolution import (  # noqa: F401
    _rebuild_resolution,
    current_address_rows,
    evidence_from_results,
    rebuild,
    tgos_evidence_row,
)


def _tgos_coordinate(x: str, y: str):
    if not x.strip() and not y.strip():
        return None
    if not x.strip() or not y.strip():
        raise ValueError("Partial TGOS coordinate pair")
    try:
        lng, lat = float(x), float(y)
    except ValueError:
        # R09-2: every rejection reason has a review code.
        raise ValueError("TGOS coordinate is not numeric") from None
    if not within_taiwan_bounds(lng, lat):
        raise ValueError("TGOS coordinate is outside declared WGS84 Taiwan bounds")
    return lng, lat


def _layer_is_ambiguous(query_key: str, door_layers: dict) -> bool:
    """R09-2: the pool holds the same door under two villages (or neighbourhoods) the query leaves open.

    Only pool keys compatible with every layer the query writes count.
    """
    signature, village, neighborhood = door_parts(query_key)
    compatible = [
        (key_village, key_neighborhood)
        for key, key_village, key_neighborhood in door_layers.get(signature, ())
        if key != query_key
        and (village is None or key_village is None or key_village == village)
        and (neighborhood is None or key_neighborhood is None or key_neighborhood == neighborhood)
    ]
    if village is None and len({v for v, _ in compatible if v is not None}) >= 2:
        return True
    return neighborhood is None and len({n for _, n in compatible if n is not None}) >= 2


DISTRICT_FILL_REASON = "tgos_district_fill"


def _check_district_fill(query: dict, target_key: str) -> None:
    """R09-6: a response to a district_missing query is adopted only inside its candidate towns
    and only for the queried road and door, with any village or neighbourhood the query writes."""
    value = json.loads(target_key[3:])
    if value["county"] != query["county_code"] or value["town"] not in json.loads(query["candidate_towns"]):
        raise ValueError("TGOS response district is not a candidate district")
    asked = district_query(query["address"])
    if asked is None:
        raise ValueError("TGOS response address differs from submitted address")
    _, _, locality_road, door, village, neighborhood = asked
    if (
        value["locality_road"] != locality_road
        or value["door"] != door
        or (village is not None and value.get("village") != village)
        or (neighborhood is not None and value.get("neighborhood") != neighborhood)
    ):
        raise ValueError("TGOS response address differs from submitted address")


KEYLESS_REASON = "TGOS query has no building key under the current rules"
RESPONSE_FILE = "Address_Finish.csv"


def _response_coordinate(x: str, y: str):
    """The returned pair as parsed, for every row whose X and Y are both finite numbers; else None.

    This is the raw evidence kept beside the adopted lng and lat, so a rejected row keeps it too.
    """
    try:
        lng, lat = float(x), float(y)
    except ValueError:
        return None
    return (lng, lat) if math.isfinite(lng) and math.isfinite(lat) else None


def _group_towns(submitted: dict, returned: list[dict]) -> set:
    """(county, town) of every row that can reach rule 2, 4 or 5 of the adoption check."""
    towns = set()
    for row in returned:
        query = submitted[row["Address"]]
        if query["query_kind"] in {"district_missing", "keyless"} or not query["building_key"]:
            continue
        try:
            if not row["Response_Address"].strip() or _tgos_coordinate(row["Response_X"], row["Response_Y"]) is None:
                continue
            target_key = building_key(normalize_address(row["Response_Address"]))
        except (TypeError, ValueError):
            continue
        if target_key and adoption_route(query["building_key"], target_key) in {NEIGHBORHOOD, VILLAGE_DOOR, VILLAGE_AS_PLACE}:
            towns.add(base_town(query["building_key"]))
    return towns


def apply_response(
    batches: list[dict],
    queries: list[dict],
    results: list[dict],
    evidence: list[dict],
    current_rows,
    batch_id: str,
    response: Path,
    *,
    base: BaseGroups,
    expected_sha256: str | None = None,
) -> str:
    """Import one response file into the in-memory ledger tables and the evidence list, in place.

    ``current_rows`` are the address status rows before this response: they give the coordinates
    already located and the villages and neighbourhoods of the pool. Returns the response hash.
    ``expected_sha256`` is the hash recorded for a carried batch; a different file is refused.
    ``base`` gives the offline-index groups that rules 2, 4 and 5 of the adoption check need; it reads
    the index once for the towns of the rows that reach those rules.
    """
    batch = next((row for row in batches if row["batch_id"] == batch_id), None)
    if not batch or batch["status"] not in {"submitted", "submission_unknown"}:
        raise ValueError("TGOS batch is not importable")
    response_hash = sha256_file(response)
    if expected_sha256 is not None and response_hash != expected_sha256:
        raise ValueError(
            f"TGOS response file for batch {batch_id} has sha256 {response_hash}, the ledger recorded {expected_sha256}"
        )
    submitted = {row["address"]: row for row in queries if row["batch_id"] == batch_id}
    if len(submitted) != batch["address_count"]:
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
    if set(addresses) != set(submitted):
        raise ValueError("TGOS response Address set differs from submitted batch")
    prior_coordinates = {}
    # R09-2: village and neighbourhood values of every pool key, by door signature.
    door_layers = {}
    for row in current_rows:
        if row["status"] == "located":
            prior_coordinates[row["building_key"]] = (row["lng"], row["lat"])
        signature, village, neighborhood = door_parts(row["building_key"])
        door_layers.setdefault(signature, []).append((row["building_key"], village, neighborhood))
    base.load(_group_towns(submitted, returned))
    new_results = []
    result_by_query = {}
    for line, row in enumerate(returned, 2):
        query = submitted[row["Address"]]
        reason = None
        response_key = None
        district = query["query_kind"] == "district_missing"
        keyless = query["query_kind"] == "keyless"
        try:
            coordinate = _tgos_coordinate(row["Response_X"], row["Response_Y"])
            status = "succeeded" if coordinate else "failed"
            if coordinate and not row["Response_Address"].strip():
                raise ValueError("Successful TGOS row lacks Response_Address")
            if coordinate:
                response_address = normalize_address(row["Response_Address"])
                target_key = building_key(response_address)
                if not target_key:
                    raise ValueError("TGOS response is not a complete address")
                if district:
                    _check_district_fill(query, target_key)
                    response_key = target_key
                    reason = DISTRICT_FILL_REASON
                elif not keyless:
                    # A success flag or a shared district does not prove the same
                    # door number; only the same door is adopted. R04-12: a village or
                    # neighbourhood the query writes must not differ in the response.
                    route = adoption_route(query["building_key"], target_key)
                    if route is None:
                        raise ValueError("TGOS response address differs from submitted address")
                    if route == NEIGHBORHOOD:
                        verdict = base.neighborhood_group(query["building_key"])
                        if not verdict.present:
                            raise ValueError("TGOS response address differs from submitted address")
                        if not verdict.single:
                            raise ValueError("TGOS neighbourhood differs and the base group has several locations")
                        if json.loads(target_key[3:]).get("neighborhood") not in verdict.neighborhoods:
                            reason = NEIGHBORHOOD_OUTSIDE_REASON
                    elif route == VILLAGE_DOOR:
                        verdict = base.village_door_group(query["building_key"])
                        if not verdict.present:
                            raise ValueError("TGOS response address differs from submitted address")
                        if not verdict.single:
                            raise ValueError("TGOS village and door have several locations in the base")
                    elif route == VILLAGE_AS_PLACE:
                        verdict = base.place_door_group(query["building_key"])
                        if not verdict.present:
                            raise ValueError("TGOS response address differs from submitted address")
                        if not verdict.single:
                            raise ValueError("TGOS village and door have several locations in the base")
                    # R09-2: the same door under two villages or neighbourhoods is not decided here.
                    if _layer_is_ambiguous(query["building_key"], door_layers):
                        raise ValueError("TGOS response village or neighbourhood is ambiguous")
        except (TypeError, ValueError) as exc:
            coordinate = None
            status = "rejected"
            reason = str(exc)
            response_key = None
        if keyless and coordinate:
            # Contract rule 5: a carried query without a key under the current rules is evidence only.
            coordinate, status, reason, response_key = None, "rejected", KEYLESS_REASON, None
        raw = _response_coordinate(row["Response_X"], row["Response_Y"])
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
            "response_building_key": response_key,
            "response_lng": raw[0] if raw else None,
            "response_lat": raw[1] if raw else None,
        }
        new_results.append(result)
        result_by_query[query["query_fingerprint"]] = result
        # R09-6: a district fill adds no offline evidence; R09-8 brings it into the pool.
        if status == "succeeded" and not district:
            evidence.append(tgos_evidence_row(query, result))
    results.extend(new_results)
    for row in queries:
        result = result_by_query.get(row["query_fingerprint"])
        if result and row["batch_id"] == batch_id:
            if (
                result["status"] == "succeeded"
                and row["building_key"] in prior_coordinates
                and prior_coordinates[row["building_key"]] != (result["lng"], result["lat"])
            ):
                row["status"] = "conflict"
            else:
                row["status"] = result["status"]
            row["result_id"] = result["result_id"]
    batch["status"] = "completed"
    batch["response_sha256"] = response_hash
    return response_hash


def _resolution_report(statuses, resolutions, suspension_counts, door_rule) -> dict:
    return {
        "status_counts": statuses,
        **({"address_suspension_counts": suspension_counts} if suspension_counts is not None else {}),
        **(
            {
                "coordinate_resolution_basis_counts": {
                    basis: sum(r["resolution_basis"] == basis for r in resolutions)
                    for basis in (WITHIN_TOLERANCE, EXCEEDS_TOLERANCE)
                },
                **(
                    {"door_cross_check_keys": sum(r["resolution_basis"] == DOOR_CROSS_CHECK for r in resolutions)}
                    if door_rule is not None
                    else {}
                ),
            }
            if resolutions is not None
            else {}
        ),
    }


def _open_base(index: Path, offline_report: dict) -> BaseGroups:
    """The adoption base of an import: the offline index this state was built from (C-3, c23)."""
    if sha256_file(Path(index) / "manifest.json") != offline_report["index_manifest_sha256"]:
        raise ValueError("Offline index is not the one this state was built from")
    return BaseGroups(index)


def import_tgos(
    source: Path,
    work_dir: Path,
    batch_id: str,
    response: Path,
    *,
    index: Path,
    run_id: str | None = None,
):
    """Import one response into a ledger-only TGOS state (b14).

    The merged address state is computed in memory for the same-door check and the report; only
    the ledger tables are written. ``index`` is the offline-index snapshot the state was built from;
    rules 2 and 4 of the adoption check read their base groups from it.
    """
    inp = open_input(source, work_dir)
    batches, queries, results, aliases, events = inp.ledger
    batch = next((row for row in batches if row["batch_id"] == batch_id), None)
    if not batch or batch["status"] not in {"submitted", "submission_unknown", "completed"}:
        raise ValueError("TGOS batch is not importable")
    response_hash = sha256_file(response)
    if batch["status"] == "completed" and batch["response_sha256"] == response_hash:
        return Path(source)
    # R09-3: a completed batch never takes a second, different response.
    if batch["status"] == "completed":
        raise ValueError(f"TGOS batch {batch_id} was already imported with response {batch['response_sha256']}; refusing response {response_hash}")
    paths = _artifact_paths(inp.offline, inp.offline_manifest)
    report = inp.offline_report
    base = _open_base(index, report)
    evidence = list(rows(paths["offline-row"])) + evidence_from_results(results, queries)
    current = current_address_rows(paths, report, results, queries)
    apply_response(batches, queries, results, evidence, current, batch_id, response, base=base)
    _, _, statuses, resolutions, suspension_counts = rebuild(paths, report, evidence)
    return _write_state(
        source,
        work_dir,
        action="import",
        batches=batches,
        queries=queries,
        results=results,
        aliases=sorted(aliases, key=lambda x: x["alias_key"]),
        alias_events=events,
        extra_report={
            "last_tgos_batch_id": batch_id,
            "last_tgos_response_sha256": response_hash,
            "tgos_base_lookup_seconds": round(base.seconds, 3),
            "status_counts_before_import": inp.report["status_counts"],
            **_resolution_report(statuses, resolutions, suspension_counts, report.get("door_rule")),
        },
        run_id=run_id,
        inp=inp,
    )


class TgosPlan(NamedTuple):
    """What the run-full tgos-state stage reads, checked before the stage runs, and its binding."""

    ledger: Path | None
    ledger_sha256: str | None
    carried: Carried | None
    responses: list  # (batch_id, response file, sha256) of every batch the ledger had completed
    binding: dict
    source_record: dict


def response_file(exchange_dir: Path, batch_id: str) -> Path:
    """The returned file of a batch: Address_Finish.csv in its exchange folder under data/tgos."""
    suffix = _identifier(batch_id).removeprefix("tgos-")[:8]
    folders = sorted(path for path in Path(exchange_dir).glob(f"*-{suffix}") if path.is_dir())
    if len(folders) != 1:
        raise ValueError(f"TGOS exchange folder of batch {batch_id} is missing or ambiguous under {exchange_dir}")
    exchange_manifest = folders[0] / "manifest.json"
    if not exchange_manifest.is_file() or json.loads(exchange_manifest.read_text(encoding="utf-8")).get("batch_id") != batch_id:
        raise ValueError(f"TGOS exchange folder {folders[0]} is not the folder of batch {batch_id}")
    file = folders[0] / RESPONSE_FILE
    if not file.is_file():
        raise ValueError(f"TGOS response file is missing: {file}")
    return file


def ledger_responses(ledger: Path, exchange_dir: Path):
    """Carry a ledger and find, hash-check against the ledger record, every response file it needs.

    Returns (carried ledger, [(batch id, response file, sha256)], ledger manifest sha256). Refuses when
    a completed batch's folder or file is missing or the file hash differs from the recorded one.
    Needs no offline state, so run-full can check it before it builds anything.
    """
    ledger = Path(ledger)
    carried = _carry_ledger(ledger)
    responses = []
    for batch in carried.batches:
        recorded = carried.reopened.get(batch["batch_id"])
        if recorded is None:
            continue
        file = response_file(exchange_dir, batch["batch_id"])
        found = sha256_file(file)
        if found != recorded:
            raise ValueError(
                f"TGOS response file {file} has sha256 {found}, the ledger recorded {recorded} for batch {batch['batch_id']}"
            )
        responses.append((batch["batch_id"], file, found))
    return carried, responses, sha256_file(ledger / "manifest.json")


def plan_tgos_state(offline: Path, ledger: Path | None, exchange_dir: Path, runs_root: Path) -> TgosPlan:
    """Check the carried ledger and every response file it needs, and make the stage binding.

    The binding lists the offline state manifest, the ledger manifest and each response hash
    (contract rule 4), so a different ledger or a changed response gives a different binding.
    """
    offline = Path(offline)
    record = source_state_record(offline)
    if _source_path(record, runs_root).resolve() != offline.resolve():
        raise ValueError("TGOS source state must lie under the runs root of the run")
    carried, ledger_hash, responses = None, None, []
    if ledger is None:
        inputs = [record["manifest_sha256"], digest(["no-tgos-ledger"])]
    else:
        ledger = Path(ledger)
        carried, responses, ledger_hash = ledger_responses(ledger, exchange_dir)
        inputs = [record["manifest_sha256"], ledger_hash, *(item[2] for item in responses)]
    binding = bindings(
        "tgos-state",
        {"action": "run-full", "daily_limit": DAILY_LIMIT, "ledger": ledger is not None,
         "normalization_version": identity.NORMALIZATION_VERSION},
        inputs,
    )
    return TgosPlan(ledger, ledger_hash, carried, responses, binding, record)


def build_tgos_state(offline: Path, work_dir: Path, plan: TgosPlan, *, index: Path, run_id: str | None = None):
    """The run-full tgos-state stage (b13): re-import every carried response under the current rules
    and write the full merged snapshot. Without a ledger the merged tables equal the offline ones."""
    stage = Stage(Path(work_dir) / "tgos-state", "tgos-state", plan.binding, run_id)
    if stage.reused:
        return stage.path
    manifest, report, _ = load_state(offline)
    paths = _artifact_paths(offline, manifest)
    base = _open_base(index, report)
    batches, queries, rekeyed, keyless = [], [], 0, 0
    if plan.carried is not None:
        batches, queries, rekeyed, keyless = plan.carried.batches, plan.carried.queries, plan.carried.rekeyed, plan.carried.keyless
    results: list[dict] = []
    replaced = {}
    if plan.responses:
        evidence = list(rows(paths["offline-row"]))
        pool = list(rows(paths["address-pool"]))
        current = rows(paths["address-result"])
        for batch_id, file, response_hash in plan.responses:
            apply_response(
                batches, queries, results, evidence, current, batch_id, file, base=base, expected_sha256=response_hash
            )
            address_rows, unmatched, statuses, resolutions, suspension_counts = rebuild(paths, report, evidence, pool)
            current = address_rows
        replaced = {
            "address_rows": address_rows,
            "evidence_rows": evidence,
            "unmatched_rows": unmatched,
            "resolution_rows": resolutions,
        }
    extra = {
        "carried_query_keys_recomputed": rekeyed,
        "tgos_carried_keyless_queries": keyless,
        "tgos_ledger_snapshot": plan.ledger.name if plan.ledger is not None else None,
        "tgos_ledger_manifest_sha256": plan.ledger_sha256,
        "tgos_reimported_batches": [{"batch_id": b, "response_sha256": h} for b, _, h in plan.responses],
    }
    if plan.responses:
        extra.update(
            {
                "last_tgos_batch_id": plan.responses[-1][0],
                "last_tgos_response_sha256": plan.responses[-1][2],
                "status_counts_before_import": report["status_counts"],
                **_resolution_report(statuses, resolutions, suspension_counts, report.get("door_rule")),
            }
        )
    return _write_merged_state(
        offline,
        stage,
        source_record=plan.source_record,
        batches=batches,
        queries=queries,
        results=results,
        aliases=[],
        alias_events=[],
        extra_report=extra,
        **replaced,
    )


def merge_ledger(source: Path, work_dir: Path, merged_dir: Path, *, run_id: str | None = None):
    """Write the merged state (contract rule 3) of a ledger snapshot as a full tgos-state under ``merged_dir``.

    For commands that read address tables, such as build-review and export-address-patch. The offline
    state is found, hash-checked and integrity-checked through the ledger's source_state; the results
    already in the ledger give the TGOS evidence, and no response file is read. The ledger snapshot is
    not changed; the merged snapshot is never a ledger for prepare-tgos or import-tgos.
    """
    inp = open_input(source, work_dir, writing=False)
    if inp.stage != "tgos-state":
        raise ValueError("Only a TGOS ledger snapshot can be merged")
    batches, queries, results, aliases, events = inp.ledger
    paths = _artifact_paths(inp.offline, inp.offline_manifest)
    adopted = evidence_from_results(results, queries)
    replaced = {}
    extra = {"tgos_merged_from_ledger": inp.path.name}
    if adopted:
        evidence = list(rows(paths["offline-row"])) + adopted
        address_rows, unmatched, statuses, resolutions, suspension_counts = rebuild(paths, inp.offline_report, evidence)
        replaced = {
            "address_rows": address_rows,
            "evidence_rows": evidence,
            "unmatched_rows": unmatched,
            "resolution_rows": resolutions,
        }
        extra.update(_resolution_report(statuses, resolutions, suspension_counts, inp.offline_report.get("door_rule")))
    ledger_hash = sha256_file(Path(source) / "manifest.json")
    binding = bindings(
        "tgos-state",
        {"action": "merge-ledger", "daily_limit": DAILY_LIMIT, "normalization_version": identity.NORMALIZATION_VERSION},
        [inp.source_record["manifest_sha256"], ledger_hash],
    )
    stage = Stage(Path(merged_dir) / "tgos-state", "tgos-state", binding, run_id)
    if stage.reused:
        return stage.path
    return _write_merged_state(
        inp.offline,
        stage,
        source_record=inp.source_record,
        batches=batches,
        queries=queries,
        results=results,
        aliases=aliases,
        alias_events=events,
        extra_report={
            **{
                key: inp.report[key]
                for key in (
                    "last_tgos_batch_id",
                    "last_tgos_response_sha256",
                    "status_counts_before_import",
                    "carried_query_keys_recomputed",
                    "tgos_carried_keyless_queries",
                )
                if key in inp.report
            },
            **extra,
        },
        **replaced,
    )
