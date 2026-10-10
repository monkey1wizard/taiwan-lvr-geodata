"""TGOS exchange files: the date log, folder names, and the CSV handed to the operator."""

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

from ..storage.runs import canonical_json, sha256_file


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
