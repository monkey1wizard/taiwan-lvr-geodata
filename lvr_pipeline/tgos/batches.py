"""TGOS state, quota, batch reservations, and status transitions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import NamedTuple

import duckdb

from ..addresses.admin import county_name
from ..addresses import identity
from ..addresses.identity import building_key
from ..addresses.pool import district_query, lane_free_road
from ..results.reconcile import check_suspended_counties, load_p2
from ..storage.parquet import BatchWriter, rows
from .resolution import current_address_rows
from ..storage.runs import SnapshotStore, Stage, _identifier, bindings, digest, sha256_file
from .exchange import _csv_bytes, _write_exchange


DAILY_LIMIT = 10_000
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


# B-4 (b14), data contract "TGOS 帳本快照與 run-full 帶入": a TGOS state snapshot written by prepare,
# set-status, repair, revoke-alias and import holds only these tables plus quality.json.
LEDGER_FILES = {
    "verified-alias": "verified_aliases.parquet",
    "tgos-batch": "tgos_batches.parquet",
    "tgos-query": "tgos_queries.parquet",
    "tgos-result": "tgos_imports.parquet",
    "alias-event": "alias_events.parquet",
}
SOURCE_STATE_KEYS = {"run_id", "stage", "snapshot_id", "manifest_sha256", "index_manifest_sha256"}


def source_state_record(offline: Path) -> dict:
    """The location and hashes of an offline-state snapshot, as recorded in a ledger's quality.json.

    The snapshot must lie at <runs root>/<run id>/offline-state/snapshots/<snapshot id>.
    """
    offline = Path(offline)
    if offline.parent.name != "snapshots" or offline.parent.parent.name != "offline-state":
        raise ValueError("TGOS source state must be a committed offline-state snapshot of a run")
    return {
        "run_id": _identifier(offline.parent.parent.parent.name),
        "stage": "offline-state",
        "snapshot_id": _identifier(offline.name),
        "manifest_sha256": sha256_file(offline / "manifest.json"),
        "index_manifest_sha256": _quality(offline)["index_manifest_sha256"],
    }


def _source_path(record: dict, runs_root: Path) -> Path:
    if not isinstance(record, dict) or set(record) != SOURCE_STATE_KEYS or record["stage"] != "offline-state":
        raise ValueError("TGOS ledger records no valid source state")
    return (
        Path(runs_root)
        / _identifier(record["run_id"])
        / "offline-state"
        / "snapshots"
        / _identifier(record["snapshot_id"])
    )


def resolve_source_state(ledger_path: Path, runs_root: Path) -> Path:
    """Find, hash-check and fully integrity-check the offline state a TGOS ledger snapshot refers to.

    Contract rule 3: located by the run ID and snapshot ID in the ledger's quality.json under
    ``runs_root``; the manifest hash must equal the recorded one; then SnapshotStore.verify_integrity
    runs, which includes the rebuild epoch and the address rule version. A missing source, a hash
    mismatch or another rule version is refused.
    """
    record = _quality(ledger_path).get("source_state")
    path = _source_path(record, runs_root)
    if not (path / "manifest.json").is_file():
        raise ValueError(f"TGOS source state is missing: {path}")
    if sha256_file(path / "manifest.json") != record["manifest_sha256"]:
        raise ValueError("TGOS source state manifest hash differs from the ledger record")
    SnapshotStore(path.parent.parent).verify_integrity(path)
    source = _quality(path)
    if source.get("stage") != "offline-state" or source.get("index_manifest_sha256") != record["index_manifest_sha256"]:
        raise ValueError("TGOS source state differs from the ledger record")
    return path


class Ledger(NamedTuple):
    batches: list
    queries: list
    results: list
    aliases: list
    events: list


class TgosInput(NamedTuple):
    """The input snapshot of a TGOS command: its stage, its ledger tables and the offline state under it."""

    path: Path
    stage: str
    report: dict
    ledger: Ledger
    offline: Path
    offline_manifest: dict
    offline_report: dict
    source_record: dict


def open_input(path: Path, work_dir: Path, *, writing: bool = True) -> TgosInput:
    """Load an offline state (first command of a TGOS run) or a ledger snapshot with its source state.

    The source is searched under the parent of ``work_dir``, the runs root of the command. A command
    that writes the next ledger snapshot must start from the latest one (``writing``); a read-only
    merge may start from any committed snapshot.
    """
    path = Path(path)
    manifest, report, stage = load_state(path)
    if writing:
        _require_latest(path, stage)
    runs_root = Path(work_dir).parent
    if stage == "offline-state":
        offline, offline_manifest, offline_report = path, manifest, report
        record = source_state_record(path)
        if _source_path(record, runs_root).resolve() != path.resolve():
            raise ValueError("TGOS source state must lie under the runs root of the TGOS run")
    else:
        offline = resolve_source_state(path, runs_root)
        offline_manifest = SnapshotStore(offline.parent.parent).verify_integrity(offline)
        offline_report = _quality(offline)
        record = report["source_state"]
    paths = _artifact_paths(path, manifest)
    ledger = Ledger(
        *(
            _table(paths.get(name))
            for name in ("tgos-batch", "tgos-query", "tgos-result", "verified-alias", "alias-event")
        )
    )
    return TgosInput(path, stage, report, ledger, offline, offline_manifest, offline_report, record)


def _write_table(stage: Stage, name: str, dataset: str, values: list[dict]):
    writer = BatchWriter(stage.build / name, dataset)
    for row in values:
        writer.add(row)
    writer.close()
    return name, writer.path, dataset, writer.row_count


def _ledger_artifacts(stage: Stage, batches, queries, results, aliases, alias_events) -> list:
    return [
        _write_table(stage, LEDGER_FILES["verified-alias"], "verified-alias", aliases),
        _write_table(stage, LEDGER_FILES["tgos-batch"], "tgos-batch", batches),
        _write_table(stage, LEDGER_FILES["tgos-query"], "tgos-query", queries),
        _write_table(stage, LEDGER_FILES["tgos-result"], "tgos-result", results),
        _write_table(stage, LEDGER_FILES["alias-event"], "alias-event", alias_events),
    ]


def _dataset_counts(artifacts: list) -> dict:
    counts = {}
    for _, _, dataset, count in artifacts:
        counts[dataset] = counts.get(dataset, 0) + count
    return counts


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
    extra_report: dict | None = None,
    extra_inputs: list[str] | None = None,
    run_id: str | None = None,
    inp: TgosInput | None = None,
):
    """Write a ledger-only TGOS state snapshot (b14): the five ledger tables and quality.json.

    ``source`` is the input snapshot, an offline state or the previous ledger snapshot. The offline
    state under it is recorded in quality.json as ``source_state`` and its manifest hash is a binding
    input; no offline table is copied.
    """
    inp = inp or open_input(source, work_dir)
    source_hash = sha256_file(Path(source) / "manifest.json")
    binding = bindings(
        "tgos-state",
        {"action": action, "daily_limit": DAILY_LIMIT, "ledger_only": True,
         "normalization_version": identity.NORMALIZATION_VERSION},
        [
            source_hash,
            inp.source_record["manifest_sha256"],
            digest(batches),
            digest(queries),
            digest(results),
            digest(alias_events),
            *(extra_inputs or []),
        ],
    )
    stage = Stage(Path(work_dir) / "tgos-state", "tgos-state", binding, run_id)
    if stage.reused:
        return stage.path
    artifacts = _ledger_artifacts(stage, batches, queries, results, aliases, alias_events)
    next_report = {
        **inp.report,
        "source_state": inp.source_record,
        "source_state_manifest_sha256": source_hash,
        "tgos_started": bool(batches or results),
        "tgos_batch_count": len(batches),
        "tgos_result_count": len(results),
        "dataset_counts": _dataset_counts(artifacts),
        **(extra_report or {}),
    }
    return stage.finish(artifacts, next_report)


def _write_merged_state(
    offline: Path,
    stage: Stage,
    *,
    source_record: dict,
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
):
    """Write the full merged tgos-state of run-full (b13): the offline tables, replaced where the
    TGOS responses changed them, plus the ledger tables. ``stage`` carries the binding."""
    manifest, report, _ = load_state(offline)
    offline_hash = sha256_file(Path(offline) / "manifest.json")
    source_artifacts = _artifact_paths(offline, manifest)
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
        if dataset in LEDGER_FILES:
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
    artifacts.extend(_ledger_artifacts(stage, batches, queries, results, aliases, alias_events))
    next_report = {
        **report,
        "source_state": source_record,
        "source_state_manifest_sha256": offline_hash,
        "tgos_started": bool(batches or results),
        "tgos_batch_count": len(batches),
        "tgos_result_count": len(results),
        "dataset_counts": _dataset_counts(artifacts),
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
    inp = open_input(source, work_dir)
    batches, queries, results, aliases, events = inp.ledger
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
        inp=inp,
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


def _door_text(locality_road: str, door: str) -> str:
    return locality_road + door + ("" if "號" in door else "號")


def key_query_text(key: str) -> str:
    """R09-7a: door-level query text built from the key fields; never the original address.

    County, town, village, neighbourhood (stored without 鄰), locality road and door, so floors,
    rooms, basements and bracket notes are never sent. The text must give the same key back.
    """
    value = json.loads(key[3:])
    text = (
        county_name(value["county"])
        + value["town"]
        + value.get("village", "")
        + (value["neighborhood"] + "鄰" if "neighborhood" in value else "")
        + _door_text(value["locality_road"], value["door"])
    )
    if building_key(text) != key:
        raise ValueError(f"TGOS query text does not give back its building key: {key}")
    return text


def district_query_text(county_code: str, parsed: tuple) -> str:
    """R09-7a: door-level text of a district_missing address: county, village, neighbourhood,
    road and door; no town and no floor. Its parse must equal the parse of the original."""
    _, _, locality_road, door, village, neighborhood = parsed
    text = (
        county_name(county_code)
        + (village or "")
        + (neighborhood + "鄰" if neighborhood else "")
        + _door_text(locality_road, door)
    )
    again = district_query(text)
    if again is None or (again[0], *again[2:]) != (county_code, *parsed[2:]):
        raise ValueError(f"TGOS district query text does not give back its door: {text}")
    return text


def district_text_or_none(county_code: str, parsed: tuple) -> str | None:
    """`district_query_text`, or None when the text does not give back its door (such an address is not sent)."""
    try:
        return district_query_text(county_code, parsed)
    except ValueError:
        return None


def _district_door(county_code: str, parsed: tuple) -> tuple:
    return (county_code, *parsed[2:])


class Carried(NamedTuple):
    batches: list
    queries: list
    rekeyed: int
    # batch_id -> the response hash recorded when the batch was completed, for batches put back to submitted.
    reopened: dict
    keyless: int


def read_ledger(path: Path) -> tuple[dict, dict, list[dict], list[dict]]:
    """The batches and queries of a TGOS ledger snapshot, after its own integrity and epoch check.

    Contract rule 4: only the ledger snapshot itself is checked (SnapshotStore.verify_ledger: structure,
    hashes, rebuild epoch); the offline state it refers to is not read, and the address rule version
    it was made under is not judged, because batches, query texts and response hashes are evidence.
    """
    path = Path(path)
    manifest = SnapshotStore(path.parent.parent).verify_ledger(path)
    report = _quality(path)
    if report.get("stage") != "tgos-state" or report.get("internal_only") is not True:
        raise ValueError("TGOS ledger must be a TGOS state")
    if digest(report["producer_config"]) != manifest["bindings"]["config_sha256"]:
        raise ValueError("Producer configuration fingerprint mismatch")
    paths = _artifact_paths(path, manifest)
    if "tgos-batch" not in paths or "tgos-query" not in paths:
        raise ValueError("TGOS ledger lacks batches or queries")
    return manifest, report, _table(paths["tgos-batch"]), _table(paths["tgos-query"])


def _carry_ledger(ledger: Path) -> Carried:
    """Carry sent batches and queries so they are not resent.

    Earlier results, evidence and aliases are dropped. Completed batches go
    back to submitted, so their stored responses can be imported again under
    the current address checks; ``reopened`` keeps the response hash each one
    was completed with. An operator may correct the service date on
    which a carried batch actually used quota; the old date stays in reason.

    Carried queries are re-keyed with the current building_key and their
    fingerprints recomputed, so a rule change cannot hide an address already
    sent or leave a key the address pool no longer contains. The old snapshot
    is not modified. ``rekeyed`` is how many queries changed key.
    A district_missing query has no key; its fingerprint is recomputed from the
    county and address.

    Contract rule 5 (owner option A): a query whose text gives no key under the current rules is
    kept as evidence with query_kind ``keyless`` and a null building_key, and its old fingerprint.
    It is never sent again, still counts as sent, and its response is not adopted. ``keyless``
    counts them. Only an epoch mismatch or structural corruption refuses a ledger.
    """
    _, _, batches, queries = read_ledger(ledger)
    reopened = {}
    for batch in batches:
        if batch["status"] == "completed":
            if not batch["response_sha256"]:
                raise ValueError(f"Completed TGOS batch {batch['batch_id']} records no response hash")
            reopened[batch["batch_id"]] = batch["response_sha256"]
            batch["status"] = "submitted"
            batch["response_sha256"] = None
    for query in queries:
        if query["batch_id"] in reopened:
            query["status"] = "submitted"
            query["result_id"] = None
    rekeyed = keyless = 0
    for query in queries:
        if query["query_kind"] == "district_missing":
            fingerprint = digest(["district_missing", query["county_code"], query["address"]])
            rekeyed += fingerprint != query["query_fingerprint"]
            query["query_fingerprint"] = fingerprint
            continue
        key = building_key(query["address"])
        if key is None:
            query["query_kind"] = "keyless"
            query["building_key"] = None
            keyless += 1
            continue
        fingerprint = digest([key, query["address"]])
        if query["query_kind"] != "building_key" or key != query["building_key"] or fingerprint != query["query_fingerprint"]:
            rekeyed += 1
        query["query_kind"] = "building_key"
        query["building_key"] = key
        query["query_fingerprint"] = fingerprint
    return Carried(batches, queries, rekeyed, reopened, keyless)


def select_unmatched_candidates(
    address_rows,
    *,
    retry_fingerprints: set[str] = frozenset(),
    prior: set[str] = frozenset(),
    sent_text: set[str] = frozenset(),
    queried_keys: set[str] = frozenset(),
    suspended_codes: set[str] = frozenset(),
    suspended_excluded: dict[str, int] | None = None,
    counts: dict[str, int] | None = None,
) -> list[dict]:
    """Pure TGOS candidate selection: unmatched or outside-scope rows not yet sent.

    Before ledger carry-over and quota; suspended counties are counted into
    `suspended_excluded` (when given) and left out. R09-7a: the query text is built from the
    key (`query_text`); a key in `queried_keys` is never selected again, and such skips are
    counted in `counts["already_queried"]`. A key whose text does not give it back is not sent;
    it is counted in `counts["text_not_round_trip"]`.
    """
    candidates = []
    for row in address_rows:
        if row["status"] not in {"unmatched", "outside_scope"}:
            continue
        if row["building_key"] in queried_keys:
            if counts is not None:
                counts["already_queried"] += 1
            continue
        try:
            text = key_query_text(row["building_key"])
        except ValueError:
            # R09-7a: an unverified text is never sent; the key waits for a normalization fix.
            if counts is not None:
                counts["text_not_round_trip"] += 1
            continue
        fingerprint = digest([row["building_key"], text])
        if fingerprint in retry_fingerprints or (
            fingerprint not in prior and text not in sent_text
        ):
            if row["county_code"] in suspended_codes:
                if suspended_excluded is not None:
                    suspended_excluded[row["county_code"]] += 1
                continue
            candidates.append({**row, "query_text": text, "query_fingerprint": fingerprint})
    return candidates


def select_district_candidates(
    occurrences: Path,
    offline_rows: Path,
    *,
    retry_fingerprints: set[str] = frozenset(),
    prior: set[str] = frozenset(),
    sent_text: set[str] = frozenset(),
    queried_doors: set[tuple] = frozenset(),
    suspended_codes: set[str] = frozenset(),
    suspended_excluded: dict[str, int] | None = None,
    counts: dict[str, int] | None = None,
) -> tuple[list[dict], int]:
    """R09-6: district_missing addresses not yet sent, with their candidate towns.

    A candidate town is a town of the offline index, in the same county, that has a valid
    key on the same road (locality road before its lane). An address with no candidate
    town is left out and counted in the second value. Rows are sorted by county and address.
    R09-7a: the query text is door-level (`district_query_text`); addresses with the same text are
    one query (`counts["merged"]`), and a door in `queried_doors` is skipped (`counts["already_queried"]`).
    """
    with duckdb.connect() as db:
        pending = db.execute(
            "SELECT DISTINCT county_code, normalized_address FROM read_parquet(?) "
            "WHERE reason = 'district_missing' AND county_code <> '' ORDER BY 1, 2",
            [str(occurrences)],
        ).fetchall()
        eligible, seen = [], set()
        for county, address in pending:
            parsed = district_query(address)
            if parsed is None:
                eligible.append((county, address, None, None))
                continue
            text = district_text_or_none(county, parsed)
            if text is None:
                # Like a key whose text does not round trip: not sent, counted, waits for a normalization fix.
                if counts is not None:
                    counts["text_not_round_trip"] += 1
                continue
            if (county, text) in seen:
                if counts is not None:
                    counts["merged"] += 1
                continue
            seen.add((county, text))
            if _district_door(county, parsed) in queried_doors:
                if counts is not None:
                    counts["already_queried"] += 1
                continue
            fingerprint = digest(["district_missing", county, text])
            if not (
                fingerprint in retry_fingerprints
                or (fingerprint not in prior and text not in sent_text)
            ):
                continue
            if county in suspended_codes:
                if suspended_excluded is not None:
                    suspended_excluded[county] += 1
                continue
            eligible.append((county, text, fingerprint, lane_free_road(parsed[2])))
        towns: dict[tuple[str, str], set[str]] = {}
        counties = sorted({county for county, _, fingerprint, _ in eligible if fingerprint is not None})
        if counties:
            found = db.execute(
                "SELECT DISTINCT county_code, json_extract_string(substr(building_key, 4), '$.town'), "
                "json_extract_string(substr(building_key, 4), '$.locality_road') FROM read_parquet(?) "
                "WHERE validity = 'valid' AND building_key IS NOT NULL AND county_code IN ("
                + ", ".join("?" * len(counties))
                + ")",
                [str(offline_rows), *counties],
            ).fetchall()
            for county, town, locality_road in found:
                towns.setdefault((county, lane_free_road(locality_road)), set()).add(town)
    candidates, without_candidates = [], 0
    for county, address, fingerprint, road in eligible:
        if fingerprint is None:
            without_candidates += 1
            continue
        candidate_towns = sorted(towns.get((county, road), ())) if road else []
        if not candidate_towns:
            without_candidates += 1
            continue
        candidates.append(
            {
                "building_key": None,
                "canonical_address": address,
                "query_text": address,
                "county_code": county,
                "address_family": digest([county, "", road]),
                "query_kind": "district_missing",
                "candidate_towns": json.dumps(candidate_towns, ensure_ascii=False),
                "query_fingerprint": fingerprint,
            }
        )
    return candidates, without_candidates


def prepare_tgos(
    source: Path,
    work_dir: Path,
    exchange_dir: Path,
    *,
    index: Path,
    limit: int = DAILY_LIMIT,
    retry_fingerprints: list[str] | None = None,
    retry_reason: str | None = None,
    ledger: Path | None = None,
    exchange_date: str | None = None,
    run_id: str | None = None,
):
    if isinstance(limit, bool) or not 1 <= limit <= DAILY_LIMIT:
        raise ValueError("TGOS file limit must be between 1 and 10000")
    inp = open_input(source, work_dir)
    stage_name, report = inp.stage, inp.offline_report
    if stage_name == "offline-state" and SnapshotStore(Path(work_dir) / "tgos-state").current():
        raise ValueError("TGOS work directory already has state; use its latest snapshot")
    paths = _artifact_paths(inp.offline, inp.offline_manifest)
    # R09-6: the index must be the one this state was built from (same address source commit and manifest).
    index_manifest, index_report = load_p2(index, "offline-index")
    index_hash = sha256_file(Path(index) / "manifest.json")
    if (
        index_report["address_source_commit"] != report["address_source_commit"]
        or index_hash != report["index_manifest_sha256"]
    ):
        raise ValueError("Offline index is not the one this state was built from")
    batches, queries, results, aliases, events = inp.ledger
    rekeyed = keyless = 0
    if ledger is not None:
        if stage_name != "offline-state":
            raise ValueError("A TGOS ledger can only seed a fresh offline state")
        carried = _carry_ledger(ledger)
        batches, queries, rekeyed, keyless = carried.batches, carried.queries, carried.rekeyed, carried.keyless
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
    if any(row["query_kind"] == "keyless" for fingerprint in retry_fingerprints for row in query_history[fingerprint]):
        raise ValueError("A TGOS query without a building key is evidence only and cannot be retried")
    if any(
        not any(row["status"] in retryable for row in query_history[fingerprint])
        for fingerprint in retry_fingerprints
    ):
        raise ValueError("TGOS retry is not in a retryable state")
    prior = set(query_history) - retry_fingerprints
    # The same address text must not be sent twice even when its key changed. A keyless query
    # (contract rule 5) was sent whatever its status.
    sent_text = {row["address"] for row in queries if row["status"] not in retryable or row["query_kind"] == "keyless"}
    # R09-7a: a door already queried under any text is not queried again, unless an approved retry names it.
    queried_keys, retried_keys, queried_doors, retried_doors = set(), set(), set(), set()
    for row in queries:
        if row["query_kind"] == "district_missing":
            parsed = district_query(row["address"])
            door = _district_door(row["county_code"], parsed) if parsed else None
            queried, retried = queried_doors, retried_doors
        else:
            door = row["building_key"]
            queried, retried = queried_keys, retried_keys
        if door is None:
            continue
        if row["status"] != "cancelled":
            queried.add(door)
        if row["query_fingerprint"] in retry_fingerprints:
            retried.add(door)
    queried_keys -= retried_keys
    queried_doors -= retried_doors
    key_counts = {"already_queried": 0, "text_not_round_trip": 0}
    district_counts = {"merged": 0, "already_queried": 0, "text_not_round_trip": 0}
    candidates = select_unmatched_candidates(
        current_address_rows(paths, report, results, queries),
        retry_fingerprints=retry_fingerprints,
        prior=prior,
        sent_text=sent_text,
        queried_keys=queried_keys,
        suspended_codes=suspended_codes,
        suspended_excluded=suspended_excluded,
        counts=key_counts,
    )
    key_text_merged = len(candidates) - len({row["query_text"] for row in candidates})
    district, without_candidates = select_district_candidates(
        paths["address-occurrence"],
        _artifact_paths(index, index_manifest)["offline-row"],
        retry_fingerprints=retry_fingerprints,
        prior=prior,
        sent_text=sent_text,
        queried_doors=queried_doors,
        suspended_codes=suspended_codes,
        suspended_excluded=suspended_excluded,
        counts=district_counts,
    )
    candidates = [
        {**row, "query_kind": "building_key", "candidate_towns": None} for row in candidates
    ] + district
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
    # R09-6: district_missing candidates follow the key candidates, sorted by county and address;
    # they take no part in the family round-robin.
    def ordered(values):
        return _round_robin([r for r in values if r["building_key"] is not None]) + [
            r for r in values if r["building_key"] is None
        ]

    selected = ordered(retry_candidates) + ordered(new_candidates)[
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
            "query_kind": row["query_kind"],
            "building_key": row["building_key"],
            "candidate_towns": row["candidate_towns"],
            "address": row["query_text"],
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
            "tgos_carried_keyless_queries": keyless,
            "tgos_district_missing_without_candidates": without_candidates,
            "tgos_key_text_merged": key_text_merged,
            "tgos_key_already_queried": key_counts["already_queried"],
            "tgos_key_text_not_round_trip": key_counts["text_not_round_trip"],
            "tgos_district_text_merged": district_counts["merged"],
            "tgos_district_already_queried": district_counts["already_queried"],
            "tgos_district_text_not_round_trip": district_counts["text_not_round_trip"],
            **(
                {"tgos_suspended_candidates_excluded": suspended_excluded}
                if suspension is not None
                else {}
            ),
        },
        extra_inputs=[index_hash],
        run_id=run_id,
        inp=inp,
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
    inp = open_input(source, work_dir)
    batches, queries, results, aliases, events = inp.ledger
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
        results=results,
        aliases=aliases,
        alias_events=events,
        extra_report={"last_tgos_batch_id": batch_id},
        run_id=run_id,
        inp=inp,
    )


def revoke_alias(
    source: Path,
    work_dir: Path,
    alias_key: str,
    *,
    reason: str,
    run_id: str | None = None,
):
    inp = open_input(source, work_dir)
    batches, queries, results, aliases, events = inp.ledger
    target = next((row for row in aliases if row["alias_key"] == alias_key), None)
    if not target:
        raise ValueError("Verified alias not found")
    aliases = [row for row in aliases if row["alias_key"] != alias_key]
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
        batches=batches,
        queries=queries,
        results=results,
        aliases=aliases,
        alias_events=events,
        extra_report={"last_revoked_alias": alias_key},
        run_id=run_id,
        inp=inp,
    )
