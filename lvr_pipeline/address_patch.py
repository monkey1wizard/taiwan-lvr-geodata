"""Export verified TGOS door-level evidence for review by taiwan-address-data."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
import re

from .address import building_key_v2, norm
from .parquet_io import BatchWriter, rows
from .processing import Stage, bindings, digest, load_snapshot
from .sources import sha256_file
from .tgos import load_state


_ADMIN_RE = re.compile(r"^(?P<county>.+?[縣市])(?P<town>.+?[市區鎮鄉])(?P<rest>.+)$")
_VILLAGE_RE = re.compile(r"^(?P<village>.+?[里村])(?P<neighborhood>\d+鄰)?(?P<rest>.*)$")
_NUMBER_RE = re.compile(r"(?P<number>\d+(?:之\d+)*號)$")
_ALLEY_RE = re.compile(r"(?P<alley>\d+(?:之\d+)*弄)$")
_LANE_RE = re.compile(r"(?P<lane>\d+(?:之\d+)*巷)$")
_SECTION_RE = re.compile(r"^(?P<road>.*?)(?P<section>[一二三四五六七八九十\d]+段)$")


def _artifact_paths(source: Path, manifest: dict) -> dict[str, Path]:
    return {
        item["schema"]: Path(source) / item["path"]
        for item in manifest["artifacts"]
        if item.get("schema")
    }


def _area_index(paths: list[Path]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames or not {"name", "dgbas_id"}.issubset(reader.fieldnames):
                raise ValueError("Area file must contain name and dgbas_id")
            for row in reader:
                name = row["name"].strip().replace("　", "")
                code = row["dgbas_id"].strip()
                if name and code and "-" in code:
                    found[name].add(code)
    return found


def _split_legacy(response_address: str, town_code: str, areas: dict[str, set[str]]) -> tuple[dict | None, str | None]:
    value = norm(response_address).replace("　", "")
    admin = _ADMIN_RE.match(value)
    if not admin:
        return None, "response_address_missing_county_or_town"
    rest = admin["rest"]
    village_match = _VILLAGE_RE.match(rest)
    if not village_match:
        return None, "response_address_missing_village"
    village_name = village_match["village"]
    area_name = f"{admin['county']}{admin['town']}{village_name}"
    village_codes = {
        code for code in areas.get(area_name, set()) if code.startswith(f"{town_code}-")
    }
    if len(village_codes) != 1:
        return None, "village_code_missing_or_ambiguous"
    village_code = next(iter(village_codes))
    street = village_match["rest"]
    number_match = _NUMBER_RE.search(street)
    if not number_match:
        return None, "response_address_missing_door_number"
    number = number_match["number"]
    street = street[: number_match.start()]
    alley = lane = section = ""
    alley_match = _ALLEY_RE.search(street)
    if alley_match:
        alley = alley_match["alley"]
        street = street[: alley_match.start()]
    lane_match = _LANE_RE.search(street)
    if lane_match:
        lane = lane_match["lane"]
        street = street[: lane_match.start()]
    section_match = _SECTION_RE.match(street)
    if section_match:
        road, section = section_match["road"], section_match["section"]
    else:
        road = street
    if not road and not village_name:
        return None, "response_address_missing_road_or_locality"
    return {
        "full_addr": value,
        "county": json.loads(building_key_v2(value)[3:])["county"],
        "town": town_code,
        "village": village_code,
        "neighborhood": village_match["neighborhood"] or "",
        "road": road,
        "section": section,
        "lane": lane,
        "alley": alley,
        "sub_alley": "",
        "tong": "",
        "number": number,
    }, None


def _write(stage: Stage, name: str, dataset: str, values: list[dict]):
    path = stage.build / name
    writer = BatchWriter(path, dataset)
    for row in values:
        writer.add(row)
    writer.close()
    return name, path, dataset, writer.row_count


def export_address_patch(state: Path, area_files: list[Path], work_dir: Path, *, run_id: str | None = None) -> Path:
    manifest, report, stage_name = load_state(state)
    if stage_name != "tgos-state":
        raise ValueError("Address patch requires a TGOS state")
    paths = _artifact_paths(state, manifest)
    state_hash = sha256_file(Path(state) / "manifest.json")
    if not area_files:
        raise ValueError("At least one area file is required")
    area_hashes = [sha256_file(path) for path in area_files]
    binding = bindings("address-patch", {"area_sha256": area_hashes}, [state_hash, *area_hashes])
    stage = Stage(Path(work_dir) / "address-patch", "address-patch", binding, run_id)
    if stage.reused:
        return stage.path

    pools = {row["building_key"]: row for row in rows(paths["address-pool"])}
    queries = {row["query_fingerprint"]: row for row in rows(paths["tgos-query"])}
    results = list(rows(paths["tgos-result"]))
    evidence = {
        row["building_key"]: row
        for row in rows(paths["offline-row"])
        if row["source_kind"] == "tgos_result" and row["validity"] == "valid"
    }
    areas = _area_index(area_files)
    grouped: dict[str, list[tuple[dict, dict, dict]]] = defaultdict(list)
    quarantined = []

    for result in results:
        if result["status"] != "succeeded":
            continue
        query = queries[result["query_fingerprint"]]
        target_key = building_key_v2(result["response_address"])
        candidate_id = digest(["address-patch-candidate", result["result_id"]])
        if not target_key:
            quarantined.append({"candidate_id": candidate_id, "batch_id": result["batch_id"], "query_fingerprint": result["query_fingerprint"], "submitted_address": result["address"], "response_address": result["response_address"], "reason": "response_address_not_complete"})
            continue
        pool = pools.get(query["building_key"])
        proof = evidence.get(query["building_key"])
        if not pool or not proof or proof["evidence_id"] != digest(["tgos", result["result_id"]]):
            quarantined.append({"candidate_id": candidate_id, "batch_id": result["batch_id"], "query_fingerprint": result["query_fingerprint"], "submitted_address": result["address"], "response_address": result["response_address"], "reason": "verified_tgos_evidence_missing"})
            continue
        grouped[target_key].append((result, query, pool))

    patch_rows, provenance_rows = [], []
    for target_key, candidates in sorted(grouped.items()):
        coordinates = {(item[0]["lng"], item[0]["lat"]) for item in candidates}
        responses = {norm(item[0]["response_address"]) for item in candidates}
        town_codes = {item[2]["town_code"] for item in candidates}
        reason = None
        if len(coordinates) != 1 or len(responses) != 1:
            reason = "target_has_conflicting_response"
        elif len(town_codes) != 1:
            reason = "target_has_conflicting_town_code"
        legacy = None
        if reason is None:
            legacy, reason = _split_legacy(next(iter(responses)), next(iter(town_codes)), areas)
        if reason:
            for result, query, _ in candidates:
                quarantined.append({"candidate_id": digest(["address-patch-candidate", result["result_id"]]), "batch_id": result["batch_id"], "query_fingerprint": query["query_fingerprint"], "submitted_address": result["address"], "response_address": result["response_address"], "reason": reason})
            continue
        lng, lat = next(iter(coordinates))
        patch_id = digest(["address-patch", target_key, lng, lat])
        patch_rows.append({"patch_id": patch_id, "key_version": "v2", "building_key": target_key, **legacy, "x": lng, "y": lat})
        for result, query, _ in candidates:
            proof = evidence[query["building_key"]]
            provenance_rows.append({"patch_id": patch_id, "evidence_id": proof["evidence_id"], "result_id": result["result_id"], "batch_id": result["batch_id"], "query_fingerprint": result["query_fingerprint"], "submitted_address": result["address"], "response_address": result["response_address"], "response_sha256": result["response_sha256"], "source_row_number": result["source_row_number"], "source_kind": "tgos_result"})

    artifacts = [
        _write(stage, "address_patch.parquet", "address-patch", patch_rows),
        _write(stage, "provenance.parquet", "address-patch-provenance", provenance_rows),
        _write(stage, "quarantine.parquet", "address-patch-quarantine", sorted(quarantined, key=lambda x: x["candidate_id"])),
    ]
    return stage.finish(artifacts, {
        "source_state_manifest_sha256": state_hash,
        "source_response_sha256": report.get("last_tgos_response_sha256"),
        "area_file_sha256": area_hashes,
        "successful_tgos_results": sum(row["status"] == "succeeded" for row in results),
        "patch_rows": len(patch_rows),
        "provenance_rows": len(provenance_rows),
        "quarantine_rows": len(quarantined),
        "non_success_tgos_results": sum(row["status"] != "succeeded" for row in results),
    })


def verify_address_patch(path: Path) -> dict:
    manifest, report = load_snapshot(path, "address-patch")
    return {"snapshot_id": manifest["snapshot_id"], **report}
