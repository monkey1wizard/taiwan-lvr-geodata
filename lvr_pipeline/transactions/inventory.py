"""Read-only source inventory and local raw integrity checks."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

from ..storage.runs import sha256_file

TRANSACTION_MEMBER = re.compile(r"^[a-z]_lvr_land_([abc])\.csv$", re.I)
CATEGORIES = {"a": "sales", "b": "presale", "c": "rent"}


def describe_zip(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError(f"ZIP CRC failure: {path.name}")
        infos = archive.infolist()
        if len({entry.filename for entry in infos}) != len(infos):
            raise ValueError("Duplicate ZIP member names")
        members = []
        for entry in sorted(infos, key=lambda item: item.filename):
            match = TRANSACTION_MEMBER.fullmatch(entry.filename)
            member = {"path": entry.filename, "size_bytes": entry.file_size,
                      "crc32": f"{entry.CRC:08x}", "category": CATEGORIES[match[1].lower()] if match else None}
            if match:
                with archive.open(entry) as binary:
                    reader = csv.reader(io.TextIOWrapper(binary, encoding="utf-8-sig", newline=""))
                    member["header"] = next(reader, [])
                    if not member["header"]:
                        raise ValueError("Missing transaction CSV header")
            members.append(member)
    transactions = [member for member in members if member["category"]]
    known_empty = path.name == "101q1_lvr_landcsv.zip" and sorted(m["path"] for m in members) == ["build.ttt", "manifest.csv"]
    if not transactions and not known_empty:
        raise ValueError("No transaction members and no known-empty evidence")
    return {"filename": path.name, "batch": path.name.removesuffix("_lvr_landcsv.zip"),
            "size_bytes": path.stat().st_size, "sha256": sha256_file(path),
            "members": members, "known_empty": known_empty,
            "known_empty_reason": "Only manifest.csv and build.ttt; inspected local archive" if known_empty else None,
            "download_uri": None, "acquired_at": None,
            "source_page": "https://plvr.land.moi.gov.tw/DownloadOpenData",
            "categories": sorted({m["category"] for m in transactions}),
            "county_prefixes": sorted({m["path"].split("_")[0] for m in transactions})}


def verify_raw(path: Path, entry: dict) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size != entry["size_bytes"] or sha256_file(path) != entry["sha256"]:
        raise ValueError("Raw size/hash mismatch")
    observed = describe_zip(path)
    for key in ["members", "known_empty", "categories", "county_prefixes"]:
        if observed[key] != entry[key]:
            raise ValueError(f"Raw manifest mismatch: {key}")


def inventory_json(value, depth=0):
    """Keep one road/member per line, rather than expanding repeated headers."""
    indent = "  " * depth
    child = "  " * (depth + 1)
    if isinstance(value, dict):
        return "{\n" + ",\n".join(child + json.dumps(key) + ": " + inventory_json(item, depth + 1)
                                   for key, item in value.items()) + "\n" + indent + "}"
    if isinstance(value, list) and value and isinstance(value[0], dict):
        rows = [inventory_json(item, depth + 1) if "members" in item else json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in value]
        return "[\n" + ",\n".join(child + row for row in rows) + "\n" + indent + "]"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def build_raw_manifest(raw_dir: Path) -> dict:
    """Describe every `*_lvr_landcsv.zip` in raw_dir (same document as the old inventory_sources.py)."""
    raw = [describe_zip(path) for path in sorted(Path(raw_dir).glob("*_lvr_landcsv.zip"))]
    if not raw:
        raise ValueError("No raw inputs found")
    return {"schema_version": "1.0", "raw_count": len(raw), "size_bytes": sum(row["size_bytes"] for row in raw),
            "license_status": "legacy_documentation_only_pending_current_source_check",
            "license_evidence": "docs/DATA_SOURCES.md", "inputs": raw}


def write_raw_manifest(raw_dir: Path, output: Path) -> dict:
    document = build_raw_manifest(raw_dir)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(inventory_json(document) + "\n", encoding="utf-8")
    return document


def _check_entry(raw_dir: Path, entry: dict) -> list[dict]:
    """Return every failure of one manifest entry; an empty list means it passed."""
    failures: list[dict] = []

    def fail(code: str, detail: str) -> None:
        failures.append({"code": code, "detail": detail})

    name = entry.get("filename")
    if not name:
        return [{"code": "entry_invalid", "detail": "manifest entry has no filename"}]
    path = Path(raw_dir) / name
    known_empty = entry.get("known_empty")
    if known_empty is True and not entry.get("known_empty_reason"):
        fail("known_empty_reason_missing", "known_empty is true but known_empty_reason is empty")
    if not path.is_file():
        # A missing file is never treated as an empty batch, even when the manifest says known_empty.
        fail("missing_file", f"{name} not found under {raw_dir}")
        return failures
    size = path.stat().st_size
    if size != entry.get("size_bytes"):
        fail("size_mismatch", f"expected {entry.get('size_bytes')}, found {size}")
    digest = sha256_file(path)
    if digest != entry.get("sha256"):
        fail("sha256_mismatch", f"expected {entry.get('sha256')}, found {digest}")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
    except zipfile.BadZipFile as exc:
        fail("not_a_zip", str(exc))
        return failures
    observed: dict[str, dict] = {}
    for info in infos:
        if info.filename in observed:
            fail("member_duplicate", f"duplicate member name {info.filename}")
        observed[info.filename] = {"size_bytes": info.file_size, "crc32": f"{info.CRC:08x}"}
    listed = {m["path"]: m for m in entry.get("members", [])}
    for member_path in sorted(listed.keys() - observed.keys()):
        fail("member_missing", f"{member_path} is listed in the manifest but not in the ZIP")
    for member_path in sorted(observed.keys() - listed.keys()):
        fail("member_unlisted", f"{member_path} is in the ZIP but not in the manifest")
    for member_path in sorted(listed.keys() & observed.keys()):
        for key in ("size_bytes", "crc32"):
            if listed[member_path].get(key) != observed[member_path][key]:
                fail(f"member_{key}_mismatch",
                     f"{member_path}: expected {listed[member_path].get(key)}, found {observed[member_path][key]}")
    has_transactions = any(TRANSACTION_MEMBER.fullmatch(p) for p in observed)
    if known_empty is True and has_transactions:
        fail("known_empty_has_transactions", "manifest says known_empty but the ZIP has transaction members")
    if known_empty is not True and not has_transactions:
        fail("empty_without_known_empty", "no transaction members and the manifest does not mark known_empty")
    return failures


def verify_inventory(raw_dir: Path, manifest: dict) -> dict:
    """Check every manifest entry against raw_dir. Never stops at the first failure."""
    entries = manifest.get("inputs", [])
    manifest_failures: list[dict] = []
    results = []
    seen: set[str] = set()
    for entry in entries:
        failures = _check_entry(raw_dir, entry)
        batch = entry.get("batch") or entry.get("filename")
        if batch in seen:
            failures.append({"code": "batch_duplicate", "detail": f"batch {batch} listed twice"})
        seen.add(batch)
        results.append({"batch": batch, "filename": entry.get("filename"),
                        "known_empty": entry.get("known_empty") is True,
                        "passed": not failures, "failures": failures})
    if manifest.get("raw_count") != len(entries):
        manifest_failures.append({"code": "raw_count_mismatch",
                                  "detail": f"raw_count {manifest.get('raw_count')} but {len(entries)} entries"})
    listed_files = {e.get("filename") for e in entries}
    unlisted = sorted(p.name for p in Path(raw_dir).glob("*_lvr_landcsv.zip") if p.name not in listed_files) \
        if Path(raw_dir).is_dir() else []
    failed = [r for r in results if not r["passed"]]
    return {"verified": not failed and not manifest_failures,
            "batch_count": len(results), "passed_count": len(results) - len(failed),
            "failed_count": len(failed),
            "known_empty_count": sum(1 for r in results if r["known_empty"] and r["passed"]),
            "manifest_failures": manifest_failures, "unlisted_files": unlisted, "batches": results}
