"""TGOS state, quota, batch reservations, and status transitions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..addresses.identity import building_key_v2
from ..results.reconcile import check_suspended_counties, load_p2
from ..storage.parquet import BatchWriter, rows
from ..storage.runs import SnapshotStore, Stage, bindings, digest, sha256_file
from .exchange import _csv_bytes, _write_exchange


DAILY_LIMIT = 10_000
# R09-0: carried queries with no key under the current rules stay in the report, not in tgos-query.
UNCARRIED = "tgos_uncarried_queries"
UNCARRIED_REASON = "no_building_key_current_rules"
DYNAMIC_SCHEMAS = {"tgos-batch", "tgos-query", "tgos-result", "alias-event"}


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
    extra_inputs: list[str] | None = None,
    run_id: str | None = None,
):
    manifest, report, source_stage = load_state(source)
    _require_latest(source, source_stage)
    source_hash = sha256_file(Path(source) / "manifest.json")
    binding = bindings(
        "tgos-state",
        {"action": action, "daily_limit": DAILY_LIMIT},
        [source_hash, digest(batches), digest(queries), digest(results), digest(alias_events),
         *(extra_inputs or [])],
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


def _carry_ledger(ledger: Path) -> tuple[list[dict], list[dict], int, list[dict]]:
    """Carry sent batches and queries so they are not resent.

    Earlier results, evidence and aliases are dropped. Completed batches go
    back to submitted, so their stored responses can be imported again under
    the current address checks. An operator may correct the service date on
    which a carried batch actually used quota; the old date stays in reason.

    Carried queries are re-keyed with the current building_key_v2 and their
    fingerprints recomputed, so a rule change cannot hide an address already
    sent or leave a key the address pool no longer contains. The old snapshot
    is not modified. The third value is how many queries changed key.

    R09-0 (owner decision 2026-10-09, option A): a query with no key under the
    current rules is not carried into tgos-query, whose key is not nullable.
    It is returned in the fourth value with the ledger's own values, for the
    state report and the review table. Queries already set aside in the
    ledger's report are carried the same way.
    """
    manifest, ledger_report, stage_name = load_state(ledger)
    if stage_name != "tgos-state":
        raise ValueError("TGOS ledger must be a TGOS state")
    paths = _artifact_paths(ledger, manifest)
    batches = _table(paths.get("tgos-batch"))
    queries = _table(paths.get("tgos-query"))
    uncarried = [dict(row) for row in ledger_report.get(UNCARRIED, [])]
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
    for query in uncarried:
        if query["batch_id"] in reopened:
            query["status"] = "submitted"
    rekeyed = 0
    carried = []
    for query in queries:
        key = building_key_v2(query["address"])
        if key is None:
            uncarried.append(
                {
                    "batch_id": query["batch_id"],
                    "ordinal": query["ordinal"],
                    "query_fingerprint": query["query_fingerprint"],
                    "building_key": query["building_key"],
                    "address": query["address"],
                    "county_code": query["county_code"],
                    "address_family": query["address_family"],
                    "status": query["status"],
                    "reason": UNCARRIED_REASON,
                }
            )
            continue
        carried.append(query)
        fingerprint = digest([key, query["address"]])
        if key != query["building_key"] or fingerprint != query["query_fingerprint"]:
            rekeyed += 1
        query["building_key"] = key
        query["query_fingerprint"] = fingerprint
    uncarried.sort(key=lambda row: (row["batch_id"], row["ordinal"]))
    return batches, carried, rekeyed, uncarried


def select_unmatched_candidates(
    address_rows,
    *,
    retry_fingerprints: set[str] = frozenset(),
    prior: set[str] = frozenset(),
    sent_text: set[str] = frozenset(),
    suspended_codes: set[str] = frozenset(),
    suspended_excluded: dict[str, int] | None = None,
) -> list[dict]:
    """Pure TGOS candidate selection: unmatched or outside-scope rows not yet sent.

    Before ledger carry-over and quota; suspended counties are counted into
    `suspended_excluded` (when given) and left out.
    """
    candidates = []
    for row in address_rows:
        if row["status"] not in {"unmatched", "outside_scope"}:
            continue
        fingerprint = digest([row["building_key"], row["canonical_address"]])
        if fingerprint in retry_fingerprints or (
            fingerprint not in prior and row["canonical_address"] not in sent_text
        ):
            if row["county_code"] in suspended_codes:
                if suspended_excluded is not None:
                    suspended_excluded[row["county_code"]] += 1
                continue
            candidates.append({**row, "query_fingerprint": fingerprint})
    return candidates


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
    uncarried = report.get(UNCARRIED, [])
    if ledger is not None:
        if stage_name != "offline-state":
            raise ValueError("A TGOS ledger can only seed a fresh offline state")
        batches, queries, rekeyed, uncarried = _carry_ledger(ledger)
    # R09-0 (owner decision 2026-10-09, option A): keys of suspended counties are not sent.
    suspension = report.get("address_suspension")
    suspended_codes = (
        {item["code"] for item in check_suspended_counties(suspension["counties"])}
        if suspension is not None
        else set()
    )
    suspended_excluded = {code: 0 for code in sorted(suspended_codes)}
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
    sent_text = {
        row["address"] for row in [*queries, *uncarried] if row["status"] not in retryable
    }
    candidates = select_unmatched_candidates(
        rows(paths["address-result"]),
        retry_fingerprints=retry_fingerprints,
        prior=prior,
        sent_text=sent_text,
        suspended_codes=suspended_codes,
        suspended_excluded=suspended_excluded,
    )
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
        extra_report={
            "last_tgos_batch_id": batch_id,
            "carried_query_keys_recomputed": rekeyed,
            **(
                {UNCARRIED: uncarried, "tgos_uncarried_query_count": len(uncarried)}
                if ledger is not None
                else {}
            ),
            **(
                {"tgos_suspended_candidates_excluded": suspended_excluded}
                if suspension is not None
                else {}
            ),
        },
        extra_inputs=[digest(uncarried)] if uncarried else None,
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
