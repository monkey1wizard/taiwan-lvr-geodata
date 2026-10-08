"""Durable TGOS reservations, human handoff, and strict result imports."""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
import os
import tempfile
from datetime import date
from pathlib import Path

from .results.reconcile import (
    DOOR_CROSS_CHECK,
    EXCEEDS_TOLERANCE,
    WITHIN_TOLERANCE,
    load_p2,
    resolution_row,
)
from .addresses.identity import building_key_v2, door_parts, same_door
from .transactions.normalize import normalize_address
from .storage.parquet import BatchWriter, rows
from .storage.runs import Stage, bindings, canonical_json, digest
from .storage.runs import SnapshotStore
from .storage.runs import sha256_file
from .contracts.validate import valid_coordinate

DAILY_LIMIT = 10_000
TAIWAN_BOUNDS = (118.0, 123.5, 21.5, 26.5)
DYNAMIC_SCHEMAS = {"tgos-batch", "tgos-query", "tgos-result", "alias-event"}
TGOS_CSV_FIELDS = ["id", "Address", "Response_Address", "Response_X", "Response_Y"]


def tgos_log_date(path: Path) -> str:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(value) != {"date"}:
        raise ValueError("TGOS date file must contain only the date field")
    return date.fromisoformat(value["date"]).isoformat()


def exchange_folder_name(batch_id: str, log_date: str) -> str:
    """Return the date-log-only folder name for a TGOS exchange."""
    compact_date = date.fromisoformat(log_date).strftime("%Y%m%d")
    identifier = batch_id.removeprefix("tgos-")[:8]
    return f"{compact_date}-{identifier}"


def _quality(path: Path) -> dict:
    return json.loads((Path(path) / "quality.json").read_text(encoding="utf-8"))


def load_state(path: Path):
    stage = _quality(path)["stage"]
    if stage not in {"offline-state", "tgos-state"}:
        raise ValueError("TGOS input must be an offline or TGOS state")
    return (*load_p2(path, stage), stage)


def _artifact_paths(path: Path, manifest: dict) -> dict[str, Path]:
    return {
        item["schema"]: Path(path) / item["path"]
        for item in manifest["artifacts"]
        if item["schema"]
    }


def _table(path: Path | None) -> list[dict]:
    return list(rows(path)) if path and path.exists() else []


def _require_latest(path: Path, stage: str) -> None:
    if stage != "tgos-state":
        return
    current = SnapshotStore(Path(path).parent.parent).current()
    if not current or current["snapshot_id"] != Path(path).name:
        raise ValueError("TGOS state is stale; use the latest snapshot")


def _write_table(stage: Stage, name: str, dataset: str, values: list[dict]):
    writer = BatchWriter(stage.build / name, dataset)
    for row in values:
        writer.add(row)
    writer.close()
    return name, writer.path, dataset, writer.row_count


def _write_state(
    source: Path,
    work_dir: Path,
    *,
    action: str,
    batches: list[dict],
    queries: list[dict],
    results: list[dict],
    aliases: list[dict],
    alias_events: list[dict],
    address_rows: list[dict] | None = None,
    evidence_rows: list[dict] | None = None,
    unmatched_rows: list[dict] | None = None,
    resolution_rows: list[dict] | None = None,
    extra_report: dict | None = None,
    run_id: str | None = None,
):
    manifest, report, source_stage = load_state(source)
    _require_latest(source, source_stage)
    source_hash = sha256_file(Path(source) / "manifest.json")
    binding = bindings(
        "tgos-state",
        {"action": action, "daily_limit": DAILY_LIMIT},
        [source_hash, digest(batches), digest(queries), digest(results), digest(alias_events)],
    )
    stage = Stage(Path(work_dir) / "tgos-state", "tgos-state", binding, run_id)
    if stage.reused:
        return stage.path
    source_artifacts = _artifact_paths(source, manifest)
    artifacts = []
    replacements = {
        "address-result": address_rows,
        "offline-row": evidence_rows,
        "unmatched-address": unmatched_rows,
        "coordinate-resolution": resolution_rows,
    }
    names = {
        "address-result": "address_index.parquet",
        "offline-row": "address_observations.parquet",
        "unmatched-address": "unmatched_addresses.parquet",
        "coordinate-resolution": "coordinate_resolutions.parquet",
        "address-pool": "unique_addresses.parquet",
        "address-occurrence": "address_occurrences.parquet",
        "tgos-ledger": "tgos_results.parquet",
    }
    for dataset, path in source_artifacts.items():
        if dataset in DYNAMIC_SCHEMAS or dataset == "verified-alias":
            continue
        replacement = replacements.get(dataset)
        if replacement is None:
            count = next(
                item["row_count"]
                for item in manifest["artifacts"]
                if item["schema"] == dataset
            )
            artifacts.append((names.get(dataset, path.name), path, dataset, count))
        else:
            artifacts.append(_write_table(stage, names[dataset], dataset, replacement))
    artifacts.extend(
        [
            _write_table(stage, "verified_aliases.parquet", "verified-alias", aliases),
            _write_table(stage, "tgos_batches.parquet", "tgos-batch", batches),
            _write_table(stage, "tgos_queries.parquet", "tgos-query", queries),
            _write_table(stage, "tgos_imports.parquet", "tgos-result", results),
            _write_table(stage, "alias_events.parquet", "alias-event", alias_events),
        ]
    )
    counts = {}
    for _, _, dataset, count in artifacts:
        counts[dataset] = counts.get(dataset, 0) + count
    next_report = {
        **report,
        "source_state_manifest_sha256": source_hash,
        "tgos_started": bool(batches or results),
        "tgos_batch_count": len(batches),
        "tgos_result_count": len(results),
        "dataset_counts": counts,
        **(extra_report or {}),
    }
    return stage.finish(artifacts, next_report)


def _round_robin(values: list[dict]) -> list[dict]:
    families = {}
    for row in sorted(values, key=lambda x: (x["address_family"], x["building_key"])):
        families.setdefault(row["address_family"], []).append(row)
    output = []
    while families:
        for key in sorted(list(families)):
            output.append(families[key].pop(0))
            if not families[key]:
                del families[key]
    return output


def _csv_bytes(queries: list[dict]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=TGOS_CSV_FIELDS, lineterminator="\r\n")
    writer.writeheader()
    for row in queries:
        writer.writerow(
            {
                "id": row["ordinal"],
                "Address": row["address"],
                "Response_Address": "",
                "Response_X": "",
                "Response_Y": "",
            }
        )
    return codecs.BOM_UTF8 + stream.getvalue().encode("utf-8")


def _write_exchange(
    root: Path,
    batch: dict,
    queries: list[dict],
    payload: bytes,
    *,
    replace: bool = False,
    folder_date: str | None = None,
) -> Path:
    folder = (
        exchange_folder_name(batch["batch_id"], folder_date)
        if folder_date
        else batch["batch_id"]
    )
    target = Path(root) / folder
    manifest = {
        "schema_version": "1.0",
        "batch_id": batch["batch_id"],
        "address_count": batch["address_count"],
        "csv": "addresses.csv",
        "csv_sha256": hashlib.sha256(payload).hexdigest(),
        "encoding": "UTF-8-sig",
        "csv_columns": TGOS_CSV_FIELDS,
        "upload_mode": "addrCompare",
        "coordinate_system": "WGS84",
        "return_fields": ["Address", "Response_Address", "Response_X", "Response_Y"],
        "queries": [
            {
                "ordinal": row["ordinal"],
                "query_fingerprint": row["query_fingerprint"],
                "building_key": row["building_key"],
                "address": row["address"],
            }
            for row in queries
        ],
    }
    if target.exists():
        differs = (
            sha256_file(target / "addresses.csv") != manifest["csv_sha256"]
            or json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            != manifest
        )
        if not differs:
            return target
        if not replace:
            raise ValueError("Existing TGOS exchange differs")
        with tempfile.TemporaryDirectory(prefix="tgos-repair-", dir=target) as tmp:
            staging = Path(tmp)
            (staging / "addresses.csv").write_bytes(payload)
            (staging / "manifest.json").write_text(
                canonical_json(manifest) + "\n", encoding="utf-8"
            )
            os.replace(staging / "addresses.csv", target / "addresses.csv")
            os.replace(staging / "manifest.json", target / "manifest.json")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tgos-exchange-", dir=target.parent) as tmp:
        staging = Path(tmp) / batch["batch_id"]
        staging.mkdir()
        (staging / "addresses.csv").write_bytes(payload)
        (staging / "manifest.json").write_text(
            canonical_json(manifest) + "\n", encoding="utf-8"
        )
        os.replace(staging, target)
    return target


def repair_prepared_exchange(
    source: Path,
    work_dir: Path,
    exchange_dir: Path,
    batch_id: str,
    *,
    exchange_date: str | None = None,
    run_id: str | None = None,
):
    manifest, _, stage_name = load_state(source)
    _require_latest(source, stage_name)
    paths = _artifact_paths(source, manifest)
    batches = _table(paths.get("tgos-batch"))
    queries = _table(paths.get("tgos-query"))
    results = _table(paths.get("tgos-result"))
    aliases = _table(paths.get("verified-alias"))
    events = _table(paths.get("alias-event"))
    batch = next((row for row in batches if row["batch_id"] == batch_id), None)
    if not batch or batch["status"] != "prepared":
        raise ValueError("Only a prepared TGOS batch can be repaired")
    batch_queries = sorted(
        (row for row in queries if row["batch_id"] == batch_id),
        key=lambda row: row["ordinal"],
    )
    if len(batch_queries) != batch["address_count"]:
        raise ValueError("TGOS prepared query count differs from batch")
    payload = _csv_bytes(batch_queries)
    batch["csv_sha256"] = hashlib.sha256(payload).hexdigest()
    state = _write_state(
        source,
        work_dir,
        action="repair_exchange",
        batches=batches,
        queries=queries,
        results=results,
        aliases=aliases,
        alias_events=events,
        extra_report={"last_tgos_batch_id": batch_id},
        run_id=run_id,
    )
    exchange = _write_exchange(
        exchange_dir,
        batch,
        batch_queries,
        payload,
        replace=True,
        folder_date=exchange_date,
    )
    return state, exchange


def _carry_ledger(ledger: Path) -> tuple[list[dict], list[dict], int]:
    """Carry sent batches and queries so they are not resent.

    Earlier results, evidence and aliases are dropped. Completed batches go
    back to submitted, so their stored responses can be imported again under
    the current address checks. An operator may correct the service date on
    which a carried batch actually used quota; the old date stays in reason.

    Carried queries are re-keyed with the current building_key_v2 and their
    fingerprints recomputed, so a rule change cannot hide an address already
    sent or leave a key the address pool no longer contains. The old snapshot
    is not modified. The third value is how many queries changed key.
    """
    manifest, _, stage_name = load_state(ledger)
    if stage_name != "tgos-state":
        raise ValueError("TGOS ledger must be a TGOS state")
    paths = _artifact_paths(ledger, manifest)
    batches = _table(paths.get("tgos-batch"))
    queries = _table(paths.get("tgos-query"))
    reopened = set()
    for batch in batches:
        if batch["status"] == "completed":
            batch["status"] = "submitted"
            batch["response_sha256"] = None
            reopened.add(batch["batch_id"])
    for query in queries:
        if query["batch_id"] in reopened:
            query["status"] = "submitted"
            query["result_id"] = None
    rekeyed = 0
    for query in queries:
        key = building_key_v2(query["address"])
        if key is None:
            # tgos-query.building_key is not nullable; the snapshot format must not change.
            raise ValueError(
                "Carried TGOS query has no building key under current rules: "
                + query["address"]
            )
        fingerprint = digest([key, query["address"]])
        if key != query["building_key"] or fingerprint != query["query_fingerprint"]:
            rekeyed += 1
        query["building_key"] = key
        query["query_fingerprint"] = fingerprint
    return batches, queries, rekeyed


def prepare_tgos(
    source: Path,
    work_dir: Path,
    exchange_dir: Path,
    *,
    limit: int = DAILY_LIMIT,
    retry_fingerprints: list[str] | None = None,
    retry_reason: str | None = None,
    ledger: Path | None = None,
    exchange_date: str | None = None,
    run_id: str | None = None,
):
    if isinstance(limit, bool) or not 1 <= limit <= DAILY_LIMIT:
        raise ValueError("TGOS file limit must be between 1 and 10000")
    manifest, report, stage_name = load_state(source)
    _require_latest(source, stage_name)
    if stage_name == "offline-state" and SnapshotStore(Path(work_dir) / "tgos-state").current():
        raise ValueError("TGOS work directory already has state; use its latest snapshot")
    paths = _artifact_paths(source, manifest)
    batches = _table(paths.get("tgos-batch"))
    queries = _table(paths.get("tgos-query"))
    results = _table(paths.get("tgos-result"))
    aliases = _table(paths.get("verified-alias"))
    events = _table(paths.get("alias-event"))
    rekeyed = 0
    if ledger is not None:
        if stage_name != "offline-state":
            raise ValueError("A TGOS ledger can only seed a fresh offline state")
        batches, queries, rekeyed = _carry_ledger(ledger)
    available = limit
    retry_fingerprints = set(retry_fingerprints or [])
    if retry_fingerprints and not retry_reason:
        raise ValueError("Approved TGOS retries require a reason")
    query_history = {}
    for row in queries:
        query_history.setdefault(row["query_fingerprint"], []).append(row)
    unknown_retries = retry_fingerprints - set(query_history)
    if unknown_retries:
        raise ValueError("Approved TGOS retry does not match query history")
    retryable = {"failed", "rejected", "cancelled"}
    if any(
        not any(row["status"] in retryable for row in query_history[fingerprint])
        for fingerprint in retry_fingerprints
    ):
        raise ValueError("TGOS retry is not in a retryable state")
    prior = set(query_history) - retry_fingerprints
    # The same address text must not be sent twice even when its key changed.
    sent_text = {row["address"] for row in queries if row["status"] not in retryable}
    candidates = []
    for row in rows(paths["address-result"]):
        if row["status"] not in {"unmatched", "outside_scope"}:
            continue
        fingerprint = digest([row["building_key"], row["canonical_address"]])
        if fingerprint in retry_fingerprints:
            candidates.append({**row, "query_fingerprint": fingerprint})
        elif fingerprint not in prior and row["canonical_address"] not in sent_text:
            candidates.append({**row, "query_fingerprint": fingerprint})
    retry_candidates = [
        row for row in candidates if row["query_fingerprint"] in retry_fingerprints
    ]
    if {row["query_fingerprint"] for row in retry_candidates} != retry_fingerprints:
        raise ValueError("Approved TGOS retry is no longer an eligible candidate")
    if len(retry_candidates) > available:
        raise ValueError("Remaining TGOS quota cannot fit all approved retries")
    new_candidates = [
        row for row in candidates if row["query_fingerprint"] not in retry_fingerprints
    ]
    selected = _round_robin(retry_candidates) + _round_robin(new_candidates)[
        : available - len(retry_candidates)
    ]
    if not selected:
        raise ValueError("No eligible TGOS candidates remain")
    source_hash = sha256_file(Path(source) / "manifest.json")
    batch_id = run_id or f"tgos-{digest([source_hash, [x['query_fingerprint'] for x in selected]])[:12]}"
    batch_queries = [
        {
            "batch_id": batch_id,
            "ordinal": ordinal,
            "query_fingerprint": row["query_fingerprint"],
            "key_version": "v2",
            "building_key": row["building_key"],
            "address": row["canonical_address"],
            "county_code": row["county_code"],
            "address_family": row["address_family"],
            "status": "prepared",
            "result_id": None,
        }
        for ordinal, row in enumerate(selected, 1)
    ]
    if len({row["address"] for row in batch_queries}) != len(batch_queries):
        raise ValueError("TGOS batch Address values must be one-to-one")
    payload = _csv_bytes(batch_queries)
    predecessors = {
        row["batch_id"]
        for fingerprint in retry_fingerprints
        for row in query_history[fingerprint]
        if row["status"] in retryable
    }
    batch = {
        "batch_id": batch_id,
        "status": "prepared",
        "address_count": len(batch_queries),
        "source_state_sha256": source_hash,
        "csv_sha256": hashlib.sha256(payload).hexdigest(),
        "response_sha256": None,
        "predecessor_batch_id": next(iter(predecessors)) if len(predecessors) == 1 else None,
        "reason": retry_reason,
    }
    state = _write_state(
        source,
        work_dir,
        action="prepare",
        batches=batches + [batch],
        queries=queries + batch_queries,
        results=results,
        aliases=aliases,
        alias_events=events,
        extra_report={"last_tgos_batch_id": batch_id, "carried_query_keys_recomputed": rekeyed},
        run_id=run_id,
    )
    exchange = _write_exchange(
        exchange_dir,
        batch,
        batch_queries,
        payload,
        folder_date=exchange_date,
    )
    return state, exchange


def transition_batch(
    source: Path,
    work_dir: Path,
    batch_id: str,
    status: str,
    *,
    reason: str | None = None,
    run_id: str | None = None,
):
    if status not in {"submitted", "submission_unknown", "cancelled"}:
        raise ValueError("Unsupported TGOS transition")
    manifest, _, stage_name = load_state(source)
    _require_latest(source, stage_name)
    paths = _artifact_paths(source, manifest)
    batches = _table(paths.get("tgos-batch"))
    queries = _table(paths.get("tgos-query"))
    target = next((row for row in batches if row["batch_id"] == batch_id), None)
    if not target:
        raise ValueError("Unknown TGOS batch")
    if target["status"] == status:
        return Path(source)
    allowed = {
        "prepared": {"submitted", "submission_unknown", "cancelled"},
        "submission_unknown": {"submitted", "cancelled"},
        "submitted": {"cancelled"},
    }
    if status not in allowed.get(target["status"], set()):
        raise ValueError("Invalid TGOS batch transition")
    original = target["status"]
    target["status"] = status
    target["reason"] = reason
    for row in queries:
        if row["batch_id"] == batch_id and row["status"] in {
            "prepared",
            "submission_unknown",
            "submitted",
        }:
            row["status"] = status
    return _write_state(
        source,
        work_dir,
        action="transition-" + status,
        batches=batches,
        queries=queries,
        results=_table(paths.get("tgos-result")),
        aliases=_table(paths.get("verified-alias")),
        alias_events=_table(paths.get("alias-event")),
        extra_report={"last_tgos_batch_id": batch_id},
        run_id=run_id,
    )


def _tgos_coordinate(x: str, y: str):
    if not x.strip() and not y.strip():
        return None
    if not x.strip() or not y.strip():
        raise ValueError("Partial TGOS coordinate pair")
    lng, lat = float(x), float(y)
    west, east, south, north = TAIWAN_BOUNDS
    if not valid_coordinate(lng, lat) or not (west <= lng <= east and south <= lat <= north):
        raise ValueError("TGOS coordinate is outside declared WGS84 Taiwan bounds")
    return lng, lat


def _rebuild_resolution(
    pool_rows: list[dict],
    evidence_rows: list[dict],
    source_commit: str,
    tolerance_m: float | None = None,
    door_rule: str | None = None,
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
    """
    coordinates = {}
    doors = {}
    door_cache = {}
    for row in evidence_rows:
        if row["validity"] == "valid" and row["building_key"]:
            point = (row["lng"], row["lat"])
            coordinates.setdefault(row["building_key"], {}).setdefault(
                point, []
            ).append(row["evidence_id"])
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
        if count == 1:
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
    evidence = _table(paths["offline-row"])
    prior_coordinates = {
        row["building_key"]: (row["lng"], row["lat"])
        for row in rows(paths["address-result"])
        if row["status"] == "located"
    }
    new_results = []
    result_by_query = {}
    for line, row in enumerate(returned, 2):
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
    address_rows, unmatched, statuses, resolutions = _rebuild_resolution(
        pool,
        evidence,
        report["address_source_commit"],
        report.get("coordinate_tolerance_m"),
        report.get("door_rule"),
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
            "status_counts_before_import": before,
            "status_counts": statuses,
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


def revoke_alias(
    source: Path,
    work_dir: Path,
    alias_key: str,
    *,
    reason: str,
    run_id: str | None = None,
):
    manifest, _, stage_name = load_state(source)
    _require_latest(source, stage_name)
    paths = _artifact_paths(source, manifest)
    aliases = _table(paths.get("verified-alias"))
    target = next((row for row in aliases if row["alias_key"] == alias_key), None)
    if not target:
        raise ValueError("Verified alias not found")
    aliases = [row for row in aliases if row["alias_key"] != alias_key]
    events = _table(paths.get("alias-event"))
    events.append(
        {
            "event_id": digest(["revoked", alias_key, target["target_key"], reason]),
            "key_version": "v2",
            "alias_key": alias_key,
            "target_key": target["target_key"],
            "action": "revoked",
            "evidence_ref": reason,
        }
    )
    return _write_state(
        source,
        work_dir,
        action="revoke-alias",
        batches=_table(paths.get("tgos-batch")),
        queries=_table(paths.get("tgos-query")),
        results=_table(paths.get("tgos-result")),
        aliases=aliases,
        alias_events=events,
        extra_report={"last_revoked_alias": alias_key},
        run_id=run_id,
    )
