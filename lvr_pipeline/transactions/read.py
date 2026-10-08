"""Streaming ZIP/CSV ingest, preserving source records before normalization."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import re
import shutil
import zipfile

from ..contracts import observation_id
from ..storage.parquet import BatchWriter
from ..storage.runs import Stage, bindings, canonical_json, digest
from ..storage.runs import sha256_file
from .inventory import verify_raw

FIELD_ALIASES = {
    "sales": {"date": ["交易年月日"], "amount": ["總價元"], "area": ["建物移轉總面積平方公尺"]},
    "presale": {"date": ["交易年月日", "交易年月"], "amount": ["總價元", "房地總價元"], "area": ["建物移轉總面積平方公尺"]},
    "rent": {"date": ["租賃年月日"], "amount": ["總額元", "月租金"], "area": ["建物總面積平方公尺", "建物移轉總面積平方公尺"]},
}
ENGLISH_ADDRESSES = {"address", "land sector position building sector house number plate"}


def select_inputs(manifest: dict, batches: list[str]) -> list[dict]:
    if not batches or len(set(batches)) != len(batches):
        raise ValueError("Select unique explicit input batches")
    entries = manifest["inputs"]
    if len({entry["batch"] for entry in entries}) != len(entries):
        raise ValueError("Duplicate manifest batches")
    selected = []
    for batch in sorted(batches):
        if not re.fullmatch(r"[0-9]{3}q[1-4]", batch):
            raise ValueError("Unsupported source batch label")
        found = [entry for entry in entries if entry["batch"] == batch]
        if len(found) != 1:
            raise ValueError(f"Missing manifest batch: {batch}")
        entry = found[0]
        if entry["filename"] != f"{batch}_lvr_landcsv.zip":
            raise ValueError("Unsafe or inconsistent source filename")
        selected.append(entry)
    return selected


def _strict_parses(archive: zipfile.ZipFile, path: str) -> bool:
    with archive.open(path) as binary:
        try:
            for _ in _StrictReader(binary):
                pass
        except csv.Error:
            return False
    return True


class _StrictReader:
    def __init__(self, binary):
        self._reader = csv.reader(io.TextIOWrapper(binary, encoding="utf-8-sig", newline=""), strict=True)

    @property
    def line_num(self):
        return self._reader.line_num

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._reader)


class _LineReader:
    """Parse each physical line alone, for members whose quoting is broken.

    A source field such as `"6號` opens a quote that never closes, so a strict
    reader joins every later line into one field. Here a broken line yields its
    raw text as a single value; the caller then records it as a failed row.
    """

    def __init__(self, binary):
        self._lines = io.TextIOWrapper(binary, encoding="utf-8-sig", newline="")
        self.line_num = 0

    def __iter__(self):
        return self

    def __next__(self):
        line = next(self._lines)
        self.line_num += 1
        text = line.rstrip("\r\n")
        if not text:
            return []
        try:
            values = next(csv.reader([text], strict=True))
        except csv.Error:
            return [text]
        return values


def ingest(raw_dir: Path, manifest: dict, batches: list[str], work_dir: Path, *, batch_rows=1024, run_id=None, code_commit=None) -> Path:
    selected = select_inputs(manifest, batches)
    for entry in selected:
        verify_raw(Path(raw_dir) / entry["filename"], entry)
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    # A conservative preflight, not a measured compression or performance claim.
    uncompressed = sum(member["size_bytes"] for entry in selected for member in entry["members"])
    workspace_estimate = max(256 * 2**20, uncompressed * 12)
    available = shutil.disk_usage(work_dir).free
    if available * 0.8 < workspace_estimate:
        raise ValueError("Insufficient workspace for conservative preflight with 20% headroom")
    binding = bindings("ingest", {"batch_rows": batch_rows, "selected_manifest": selected},
                       [digest(selected), *[entry["sha256"] for entry in selected]], code_commit)
    stage = Stage(Path(work_dir) / "ingested", "ingest", binding, run_id)
    if stage.reused:
        return stage.path
    artifacts, members = [], []
    report = {"batches": [entry["batch"] for entry in selected], "known_empty_batches": [],
              "input_rows": 0, "parse_failed_rows": 0, "english_rows": 0, "repeated_headers": 0,
              "blank_rows": 0, "batch_rows": batch_rows, "max_buffer_rows": 0, "max_buffer_bytes": 0, "members": members,
              "source_sha256": {entry["batch"]: entry["sha256"] for entry in selected}}
    report["resource_preflight"] = {"available_bytes": available, "uncompressed_input_bytes": uncompressed,
                                    "estimated_workspace_bytes": workspace_estimate, "headroom_fraction": 0.20,
                                    "estimate_kind": "conservative_not_measured"}
    for entry in selected:
        if entry["known_empty"]:
            report["known_empty_batches"].append(entry["batch"])
        with zipfile.ZipFile(Path(raw_dir) / entry["filename"]) as archive:
            for category in ["sales", "presale", "rent"]:
                name = f"records/{entry['batch']}/{category}.parquet"
                writer = BatchWriter(stage.build / name, "ingest-record", batch_rows)
                try:
                    for member in entry["members"]:
                        if member["category"] != category:
                            continue
                        line_mode = not _strict_parses(archive, member["path"])
                        if line_mode:
                            report.setdefault("line_mode_members", []).append(
                                {"batch": entry["batch"], "path": member["path"]})
                        with archive.open(member["path"]) as binary:
                            reader = (_LineReader if line_mode else _StrictReader)(binary)
                            header = next(reader)
                            if len(set(header)) != len(header) or any(not field for field in header):
                                raise ValueError("Duplicate/empty CSV columns")
                            required = {"土地位置建物門牌", "交易標的"}
                            if not required.issubset(header) or not any(field in header for field in FIELD_ALIASES[category]["date"]):
                                raise ValueError("Required transaction CSV columns missing")
                            mapped = required | {"編號"} | {field for values in FIELD_ALIASES[category].values() for field in values}
                            # R06-3: per-member row accounting. Blank, repeated-header and English
                            # label lines are not input rows; they are counted beside them.
                            counts = {"input_rows": 0, "parse_failed_rows": 0, "blank_rows": 0,
                                      "repeated_headers": 0, "english_rows": 0}
                            members.append({"path": member["path"], "batch": entry["batch"], "category": category,
                                            "header": header, "unmapped_columns": sorted(set(header) - mapped),
                                            "line_mode": line_mode, **counts})
                            member_counts = members[-1]
                            while True:
                                start = reader.line_num + 1
                                try:
                                    values = next(reader)
                                except StopIteration:
                                    break
                                if not values:
                                    report["blank_rows"] += 1
                                    member_counts["blank_rows"] += 1
                                    continue
                                if values == header:
                                    report["repeated_headers"] += 1
                                    member_counts["repeated_headers"] += 1
                                    continue
                                if (len(values) == len(header)
                                    and values[header.index("土地位置建物門牌")].strip().lower() in ENGLISH_ADDRESSES
                                    and values[header.index("交易標的")].strip().lower() in {"target", "transaction sign"}
                                    and all(not values[header.index(field)].strip().isdigit()
                                            for field in FIELD_ALIASES[category]["date"] if field in header)):
                                    report["english_rows"] += 1
                                    member_counts["english_rows"] += 1
                                    continue
                                valid = len(values) == len(header)
                                row = {"raw_record_id": observation_id(entry["sha256"], member["path"], start),
                                       "input_sha256": entry["sha256"], "src_batch": entry["batch"], "category": category,
                                       "member_path": member["path"], "source_row_number": start, "source_line_end": reader.line_num,
                                       "raw_fields_json": canonical_json(dict(zip(header, values))) if valid else "{}",
                                       "raw_values_json": canonical_json(values), "header_json": canonical_json(header),
                                       "row_status": "parsed" if valid else "failed"}
                                writer.add(row)
                                report["input_rows"] += 1
                                report["parse_failed_rows"] += not valid
                                member_counts["input_rows"] += 1
                                member_counts["parse_failed_rows"] += not valid
                finally:
                    writer.close()
                report["max_buffer_rows"] = max(report["max_buffer_rows"], writer.max_buffer_rows)
                report["max_buffer_bytes"] = max(report["max_buffer_bytes"], writer.max_buffer_bytes)
                artifacts.append((name, writer.path, "ingest-record", writer.row_count))
    for entry in selected:
        if sha256_file(Path(raw_dir) / entry["filename"]) != entry["sha256"]:
            raise ValueError("Raw input changed during ingest")
    return stage.finish(artifacts, report)
