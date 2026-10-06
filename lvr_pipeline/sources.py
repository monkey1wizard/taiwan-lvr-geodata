"""Read-only source inventory and local raw integrity checks."""
from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from pathlib import Path

from .storage.runs import sha256_file

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
