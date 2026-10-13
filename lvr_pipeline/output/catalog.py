"""catalog.json: the file index and the run-wide facts of one output (docs/輸出欄位.md, 公開目錄)."""

from __future__ import annotations

import json
from pathlib import Path
import re

from ..addresses.admin import admin_towns_sha256
from ..addresses.identity import NORMALIZATION_VERSION
from ..storage.runs import sha256_file

CATALOG_NAME = "catalog.json"
CURRENCY = "TWD"
CONTRACT_VERSION = "2026-10-12-22col"
PARQUET_NAME = "taiwanlvr.parquet"
FORMATS = ("geoparquet", "gpkg", "csv", "geojson")
CUTOFF = re.compile(r"[0-9]{6}")


def cutoff_text(cutoff_yyyymm) -> str:
    text = f"{cutoff_yyyymm:06d}" if isinstance(cutoff_yyyymm, int) and not isinstance(cutoff_yyyymm, bool) else cutoff_yyyymm
    if not isinstance(text, str) or not CUTOFF.fullmatch(text):
        raise ValueError(f"Invalid cutoff_yyyymm for the catalog: {cutoff_yyyymm!r}")
    return text


def catalog_json(catalog) -> str:
    return json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _entry(relative, info, public_root: Path, release_root: Path) -> dict:
    fmt = info["format"]
    release = fmt == "geoparquet"
    path = (release_root if release else public_root) / relative
    year, month = info["year"], info["month"]
    entry = {
        "path": relative,
        "location": "release" if release else "public",
        "format": fmt,
        "period": "all" if year is None else "year" if month is None else "month",
        "year": year,
        "month": month,
        "category": info["category"],
        "rows": info["rows"],
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
    if fmt == "gpkg":
        entry["layers"] = dict(info["layers"])
    return entry


def write_catalog(public_root, release_root, summary, *, converted_report, cutoff_yyyymm) -> dict:
    """Write public_root/catalog.json from the write_output summary and return its content."""
    public_root, release_root = Path(public_root), Path(release_root)
    sources = converted_report.get("source_sha256") if isinstance(converted_report, dict) else None
    if not isinstance(sources, dict) or not sources:
        raise ValueError("The converted report has no source_sha256, so the catalog cannot record the source ZIP hashes")
    entries = sorted(
        (_entry(relative, info, public_root, release_root) for relative, info in summary["files"].items()),
        key=lambda e: e["path"],
    )
    catalog = {
        "currency": CURRENCY,
        "contract_version": CONTRACT_VERSION,
        "door_rule_version": NORMALIZATION_VERSION,
        "admin_towns_sha256": admin_towns_sha256(),
        "cutoff_yyyymm": cutoff_text(cutoff_yyyymm),
        "source_zip_sha256": {batch: sources[batch] for batch in sorted(sources)},
        "files": entries,
        "total_rows": summary["total_rows"],
    }
    (public_root / CATALOG_NAME).write_text(catalog_json(catalog), encoding="utf-8", newline="\n")
    return catalog
