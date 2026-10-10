"""Maintenance handoff: write one ZIP or indexed parts, and extract and check them safely."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import zipfile

from ..results.reconcile import load_p2
from ..storage.runs import load_snapshot, sha256_file
from .monthly import artifact, safe_path, write_json, zip_files


MEMBER_BUDGET_OVERHEAD = 512
ZIP_BASE_OVERHEAD = 4096


def write_maintenance(staging, identifier, bundle, max_asset_bytes):
    """Write the maintenance handoff as one legacy ZIP when it fits, else indexed parts.

    Files are never split. A member is budgeted at its raw size plus ZIP overhead, so a
    part cannot exceed the limit even if its content does not compress.
    """
    sized = [
        (name, Path(source), Path(source).stat().st_size) for name, source in bundle
    ]
    for name, _, size in sized:
        if size + MEMBER_BUDGET_OVERHEAD + ZIP_BASE_OVERHEAD > max_asset_bytes:
            raise ValueError(
                f"Maintenance member exceeds configured asset limit: {name}"
            )
    total = (
        sum(size + MEMBER_BUDGET_OVERHEAD for _, _, size in sized) + ZIP_BASE_OVERHEAD
    )
    if total <= max_asset_bytes:
        path = staging / f"{identifier}_maintenance.zip"
        zip_files(path, [(name, source) for name, source, _ in sized])
        if path.stat().st_size > max_asset_bytes:
            raise ValueError("Maintenance asset exceeds configured limit")
        return [artifact(staging, path, kind="maintenance")]
    groups, current, used = [], [], ZIP_BASE_OVERHEAD
    for item in sorted(sized, key=lambda x: x[0]):
        budget = item[2] + MEMBER_BUDGET_OVERHEAD
        if current and used + budget > max_asset_bytes:
            groups.append(current)
            current, used = [], ZIP_BASE_OVERHEAD
        current.append(item)
        used += budget
    if current:
        groups.append(current)
    parts, members, assets = [], [], []
    for i, group in enumerate(groups, 1):
        path = staging / f"{identifier}_maintenance.part{i:03d}.zip"
        zip_files(path, [(name, source) for name, source, _ in group])
        item = artifact(staging, path, kind="maintenance", role="part")
        if item["bytes"] > max_asset_bytes:
            raise ValueError("Maintenance part exceeds configured limit")
        assets.append(item)
        parts.append(
            {"name": item["asset_name"], "bytes": item["bytes"], "sha256": item["sha256"]}
        )
        members.extend(
            {
                "path": name,
                "part": path.name,
                "bytes": size,
                "sha256": sha256_file(source),
            }
            for name, source, size in group
        )
    index = staging / f"{identifier}_maintenance_index.json"
    write_json(
        index,
        {
            "schema_version": "1.0",
            "kind": "maintenance-index",
            "snapshot_id": identifier,
            "parts": parts,
            "members": members,
            "part_count": len(parts),
            "member_count": len(members),
            "member_bytes": sum(m["bytes"] for m in members),
        },
    )
    return [artifact(staging, index, kind="maintenance", role="index")] + assets


def extract_zip(archive_path, target, budget=8 * 2**30):
    """Extract a handoff ZIP safely; returns the extracted entry names."""
    target = Path(target)
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names):
            raise ValueError("Duplicate handoff ZIP entry")
        if sum(i.file_size for i in archive.infolist()) > budget:
            raise ValueError("Handoff exceeds 8GiB extraction budget")
        for info in archive.infolist():
            safe_path(info.filename)
            if info.external_attr >> 16 & 0o170000 == 0o120000:
                raise ValueError("Handoff symlink forbidden")
            destination = target / info.filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise FileExistsError(destination)
            with archive.open(info) as source, destination.open("xb") as stream:
                shutil.copyfileobj(source, stream, 2**20)
    return names


def extract_indexed_handoff(index_path, target):
    index_path, target = Path(index_path), Path(target)
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if index.get("schema_version") != "1.0" or index.get("kind") != "maintenance-index":
        raise ValueError("Unsupported maintenance index")
    parts = index["parts"]
    members = index["members"]
    if (
        len({p["name"] for p in parts}) != len(parts)
        or len({m["path"] for m in members}) != len(members)
        or index["part_count"] != len(parts)
        or index["member_count"] != len(members)
        or index["member_bytes"] != sum(m["bytes"] for m in members)
    ):
        raise ValueError("Maintenance index totals mismatch")
    if index["member_bytes"] > 8 * 2**30:
        raise ValueError("Handoff exceeds 8GiB extraction budget")
    for part in parts:
        safe_path(part["name"])
        if "/" in part["name"]:
            raise ValueError("Unsafe artifact path")
        path = index_path.parent / part["name"]
        if not path.is_file():
            raise ValueError("Maintenance part missing")
        if path.stat().st_size != part["bytes"] or sha256_file(path) != part["sha256"]:
            raise ValueError("Maintenance part hash/size mismatch")
    for member in members:
        safe_path(member["path"])
    part_names = {p["name"] for p in parts}
    if any(m["part"] not in part_names for m in members):
        raise ValueError("Maintenance member refers to unknown part")
    for part in parts:
        names = extract_zip(index_path.parent / part["name"], target)
        if sorted(names) != sorted(
            m["path"] for m in members if m["part"] == part["name"]
        ):
            raise ValueError("Maintenance part member list differs from index")
    extracted = {
        p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()
    }
    if extracted != {m["path"] for m in members}:
        raise ValueError("Extracted maintenance files differ from index")
    for member in members:
        path = target / member["path"]
        if (
            path.stat().st_size != member["bytes"]
            or sha256_file(path) != member["sha256"]
        ):
            raise ValueError("Maintenance member hash/size mismatch")


def extract_handoff(archive_path: Path, target: Path):
    """Extract a legacy single maintenance ZIP, or an indexed set of parts (.json)."""
    target = Path(target)
    if Path(archive_path).suffix == ".json":
        extract_indexed_handoff(archive_path, target)
    else:
        extract_zip(archive_path, target)
    handoff = json.loads((target / "handoff.json").read_text(encoding="utf-8"))
    if (
        handoff["schema_version"] != "1.0"
        or handoff["key_version"] != "v2"
        or not isinstance(handoff["tgos_started"], bool)
    ):
        raise ValueError(
            "Incomplete or incompatible handoff; rebuild required when source fields are unavailable"
        )
    for key in ["converted", "state"]:
        safe_path(handoff[key])
        if (
            sha256_file(target / handoff[key] / "manifest.json")
            != handoff[key + "_manifest_sha256"]
        ):
            raise ValueError("Handoff manifest differs")
    cm, _ = load_snapshot(target / handoff["converted"], "converted")
    state_stage = handoff.get("state_stage", "offline-state")
    if state_stage not in {"offline-state", "tgos-state"}:
        raise ValueError("Unsupported handoff state stage")
    _, sr = load_p2(target / handoff["state"], state_stage)
    required = {
        "address-result",
        "address-pool",
        "address-occurrence",
        "offline-row",
        "unmatched-address",
        "verified-alias",
        "tgos-ledger",
    }
    if not required.issubset(sr["dataset_counts"]):
        raise ValueError(
            "Missing maintenance state; compatible snapshot or raw rebuild required"
        )
    if sr["converted_snapshot_sha256"] != handoff["converted_manifest_sha256"]:
        raise ValueError("Handoff source/state mismatch")
    return handoff
