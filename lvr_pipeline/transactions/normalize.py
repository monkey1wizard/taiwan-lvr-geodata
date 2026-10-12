"""Per-observation normalization without guessing transaction identity or doors."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re

import pyarrow.parquet as pq

from ..addresses import identity
from ..addresses.identity import building_key
from ..addresses.parse import arab_to_cjk, is_garbled, norm
from ..contracts import component_id
from ..addresses.characters import fix_garbled, load_garbled
from .read import DETAIL_CATEGORIES, DETAIL_MEMBER, FIELD_ALIASES
from ..storage.parquet import BatchWriter, rows
from ..storage.runs import Stage, bindings, canonical_json, load_snapshot
from ..storage.runs import sha256_file
from .dates import validated_roc_to_tx_yyyymm

DATASETS = {"observation": "observations", "address-component": "address_components",
            "exclusion": "exclusions", "diagnostic": "diagnostics", "disposition": "dispositions"}
DETAIL_KINDS = ("build", "land", "park")
DETAIL_COUNTERS = ("serial_missing", "serial_duplicated", "detail_unmatched", "detail_ambiguous", "detail_member_missing",
                   "detail_serial_column_missing", "detail_row_failed", "linked_rows")
MAIN_MEMBER = re.compile(r"^([a-z])_lvr_land_([abc])\.csv$", re.I)
LAND = re.compile(r"地號|地段|等[0-9０-９]+筆")


def field_value(fields: dict, aliases: list[str]) -> str:
    values = {fields[key].strip() for key in aliases if fields.get(key, "").strip()}
    if len(values) > 1:
        raise ValueError("Conflicting source aliases")
    return next(iter(values), "")


def normalize_address(value: str) -> str:
    value = arab_to_cjk(norm(value.strip()))
    if re.search(r"[0-9]\s+[0-9]", value):
        return value  # Do not join two ambiguous number tokens.
    return re.sub(r"\s+", "", value)


def explicit_components(address: str) -> tuple[list[str], bool]:
    parts = re.split(r"[、,，]", address)
    if len(parts) == 1:
        return [address], building_key(address) is not None
    if len(parts) > 64 or any(not part for part in parts):
        return [address], False
    first = parts[0]
    if "號" not in first:
        first += "號"
    match = re.fullmatch(r"(.+?)([0-9]+(?:之[0-9]+)*)號(?:之[0-9]+)*", first)
    if not match or building_key(first) is None:
        return [address], False
    result = [first]
    for part in parts[1:]:
        if re.fullmatch(r"[0-9]+(?:之[0-9]+)*號?(?:之[0-9]+)*", part):
            part = match[1] + part
            if "號" not in part:
                part += "號"
        if building_key(part) is None:
            return [address], False
        result.append(part)
    return result, True


def normalize_record(raw: dict, cutoff: int, char_map: dict, token_patterns: list) -> dict[str, list[dict]]:
    output = {name: [] for name in DATASETS}
    raw_id = raw["raw_record_id"]
    source = f"{raw['src_batch']}/{raw['member_path']}:{raw['source_row_number']}"
    disposition = {**raw, "outcome": "failed", "reason": ""}
    output["disposition"].append(disposition)

    def diagnostic(code: str, detail: str):
        output["diagnostic"].append({"raw_record_id": raw_id, "code": code, "detail": detail, "source_ref": source})

    def exclude(reason):
        disposition.update(outcome="excluded", reason=reason)
        output["exclusion"].append({"raw_record_id": raw_id, "reason": reason, "source_ref": source})
        return output

    if raw["row_status"] == "failed":
        disposition["reason"] = "csv_column_count"
        diagnostic("csv_column_count", raw["raw_values_json"])
        return output
    fields = json.loads(raw["raw_fields_json"])
    address = fields["土地位置建物門牌"]
    target = fields["交易標的"].strip()
    if target == "土地" or LAND.search(address):
        return exclude("land")
    if target == "車位":
        return exclude("parking")
    corrected = normalize_address(fix_garbled(address, char_map, token_patterns))
    if "號" not in corrected:
        return exclude("no_doorplate")
    aliases = FIELD_ALIASES[raw["category"]]
    try:
        date_raw = field_value(fields, aliases["date"])
    except ValueError as exc:
        diagnostic("conflicting_date_columns", str(exc))
        return exclude("invalid_date")
    month = validated_roc_to_tx_yyyymm(date_raw, run_cutoff_yyyymm=cutoff)
    if month is None:
        diagnostic("invalid_date", date_raw)
        return exclude("invalid_date")
    amount = area = None
    for kind in ["amount", "area"]:
        try:
            value = field_value(fields, aliases[kind])
            if not value:
                continue
            if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
                raise ValueError("Expected unsigned decimal source value")
            decimal = Decimal(value)
            if kind == "amount":
                whole, _, fraction = value.partition(".")
                if any(character != "0" for character in fraction[2:]):
                    raise ValueError("Amount does not fit exact integer cents")
                scaled = int(whole) * 100 + int(fraction[:2].ljust(2, "0"))
                if scaled > 2**63 - 1:
                    raise ValueError("Amount does not fit exact integer cents")
                amount = scaled
            else:
                area = format(decimal, "f")
        except (ValueError, InvalidOperation) as exc:
            diagnostic("invalid_" + kind, str(exc))
    addresses, confirmed = explicit_components(corrected)
    if is_garbled(corrected):
        diagnostic("unresolved_garbled_address", corrected)
    if not confirmed:
        diagnostic("address_review", corrected)
    for ordinal, member_address in enumerate(addresses):
        output["address-component"].append({"raw_record_id": raw_id, "component_id": component_id(raw_id, ordinal),
            "ordinal": ordinal, "normalized_address": member_address, "key_version": "v2",
            "building_key": building_key(member_address) if confirmed else None,
            "expansion_status": "confirmed" if confirmed else "review"})
    output["observation"].append({"raw_record_id": raw_id, "schema_version": "1.0", "category": raw["category"],
        "src_batch": raw["src_batch"], "source_serial": fields.get("編號", "").strip() or None,
        "input_sha256": raw["input_sha256"], "member_path": raw["member_path"], "source_row_number": raw["source_row_number"],
        "record_grain": "source_observation", "transaction_key": None, "raw_address": address, "tx_date_raw": date_raw,
        "tx_yyyymm": month, "run_cutoff_yyyymm": cutoff, "amount_minor": amount, "area_m2_decimal": area,
        "props_json": canonical_json(fields), "parse_status": "review" if output["diagnostic"] else "retained",
        "currency": "TWD", "amount_scale": 100})
    disposition.update(outcome="retained", reason="review" if output["diagnostic"] else "normalized")
    return output


def member_dispositions(ingest_members: list[dict], tallies: dict) -> list[dict]:
    """R06-3 destination report: per (batch, member), input = retained + excluded + failed.

    Blank, repeated-header and English label lines are not input rows; they are
    listed in their own columns. Raises when any equation fails.
    """
    result = []
    for member in ingest_members:
        key = (member["batch"], member["path"])
        tally = tallies.pop(key, None) or {"retained": 0, "excluded": 0, "failed": 0, "excluded_by_reason": {}}
        entry = {"batch": member["batch"], "path": member["path"], "category": member["category"],
                 "input_rows": member.get("input_rows"), "retained": tally["retained"],
                 "excluded": tally["excluded"], "excluded_by_reason": dict(sorted(tally["excluded_by_reason"].items())),
                 "failed": tally["failed"], "line_mode": member.get("line_mode"),
                 "blank_rows": member.get("blank_rows"), "repeated_headers": member.get("repeated_headers"),
                 "english_rows": member.get("english_rows")}
        if entry["input_rows"] is None:  # ingest snapshot written before R06-3
            entry["equation"] = "not_checkable_legacy_ingest"
        else:
            if entry["input_rows"] != entry["retained"] + entry["excluded"] + entry["failed"]:
                raise ValueError(f"Member row accounting mismatch: {member['batch']}/{member['path']}")
            if entry["failed"] != member.get("parse_failed_rows", entry["failed"]):
                raise ValueError(f"Member failed-row mismatch: {member['batch']}/{member['path']}")
            entry["equation"] = "holds"
        result.append(entry)
    if tallies:
        raise ValueError("Rows belong to a member absent from the ingest report")
    return result


def _group_key(batch: str, letter: str, category: str) -> tuple:
    return (batch, letter.lower(), category)


def main_serials(path: Path, batch_rows: int) -> dict:
    """R07-7: per file group, serial -> raw_record_ids over parsed main rows (every disposition)."""
    groups: dict[tuple, dict] = {}
    columns = ["raw_record_id", "src_batch", "category", "member_path", "raw_fields_json", "row_status"]
    for batch in pq.ParquetFile(path).iter_batches(batch_size=batch_rows, columns=columns):
        for row in batch.to_pylist():
            if row["row_status"] != "parsed":
                continue
            letter = MAIN_MEMBER.fullmatch(row["member_path"])[1]
            group = groups.setdefault(_group_key(row["src_batch"], letter, row["category"]),
                                      {"serials": {}, "missing": 0})
            serial = json.loads(row["raw_fields_json"]).get("編號", "").strip()
            if serial:
                group["serials"].setdefault(serial, []).append(row["raw_record_id"])
            else:
                group["missing"] += 1
    return groups


def link_details(detail_path: Path, groups: dict, serial_less_members: set, writer: BatchWriter, counters: dict, batch_rows: int) -> None:
    """R07-7: link ingest-detail rows to main rows by serial inside one file group; count every exception."""
    for batch in pq.ParquetFile(detail_path).iter_batches(batch_size=batch_rows):
        for row in batch.to_pylist():
            match = DETAIL_MEMBER.fullmatch(row["member_path"])
            key = _group_key(row["src_batch"], match[1], row["category"])
            kind = row["detail_kind"]
            if (row["src_batch"], row["member_path"]) in serial_less_members:
                continue  # counted once per member as detail_serial_column_missing
            counter = counters[(*key, kind)]
            if row["row_status"] == "failed":
                counter["detail_row_failed"] += 1
                continue
            ids = groups.get(key, {"serials": {}})["serials"].get(row["serial"]) if row["serial"] else None
            if not ids:
                counter["detail_unmatched"] += 1
            elif len(ids) > 1:
                counter["detail_ambiguous"] += 1
            else:
                counter["linked_rows"] += 1
                writer.add({"raw_record_id": ids[0], "detail_kind": kind, "detail_record_id": row["detail_record_id"],
                            "member_path": row["member_path"], "source_row_number": row["source_row_number"],
                            "raw_fields_json": row["raw_fields_json"]})


def new_counters(ingest_report: dict) -> dict:
    """R07-7: one counter set per (batch, county letter, category, kind) of every file group.

    A group exists when its main member or any of its detail members is in the ingest report.
    Member level exceptions are counted here, row level exceptions in ``link_details``.
    """
    counters, mains = {}, set()
    for member in ingest_report["members"]:
        mains.add(_group_key(member["batch"], MAIN_MEMBER.fullmatch(member["path"])[1], member["category"]))
    present = {}
    for member in ingest_report["detail_members"]:
        key = _group_key(member["batch"], DETAIL_MEMBER.fullmatch(member["path"])[1], member["category"])
        present[(*key, member["detail_kind"])] = member
    for key in sorted(mains | {key[:3] for key in present}):
        for kind in DETAIL_KINDS:
            counter = counters[(*key, kind)] = dict.fromkeys(DETAIL_COUNTERS, 0)
            member = present.get((*key, kind))
            if member is None:
                counter["detail_member_missing"] += key in mains
            elif "編號" not in member["header"]:
                counter["detail_serial_column_missing"] += 1
    return counters


def detail_link_report(group_totals: dict, counters: dict) -> list[dict]:
    result = []
    for key in sorted({key[:3] for key in counters}):
        missing, duplicated = group_totals.get(key, (0, 0))
        result.append({"batch": key[0], "county_letter": key[1], "category": key[2], "detail_kind": None,
                       **dict.fromkeys(DETAIL_COUNTERS, 0), "serial_missing": missing, "serial_duplicated": duplicated})
        for kind in DETAIL_KINDS:
            result.append({"batch": key[0], "county_letter": key[1], "category": key[2], "detail_kind": kind,
                           **counters[(*key, kind)]})
    return result


def normalize(ingest_snapshot: Path, work_dir: Path, *, cutoff: int, rules_path: Path, batch_rows=1024, run_id=None, code_commit=None) -> Path:
    # Validate cutoff even for known-empty source scopes.
    validated_roc_to_tx_yyyymm("0010101", run_cutoff_yyyymm=cutoff)
    rules_path = Path(rules_path)
    if not rules_path.is_file():
        raise FileNotFoundError("Explicit garbled rules file required")
    manifest, previous = load_snapshot(ingest_snapshot, "ingest")
    rules_hash = sha256_file(rules_path)
    binding = bindings("normalize", {"cutoff": cutoff, "batch_rows": batch_rows,
                                "normalization_version": identity.NORMALIZATION_VERSION},
                       [sha256_file(Path(ingest_snapshot) / "manifest.json"), rules_hash], code_commit)
    stage = Stage(Path(work_dir) / "normalized", "normalize", binding, run_id)
    if stage.reused:
        return stage.path
    char_map, patterns = load_garbled(str(rules_path))
    report = {"batches": previous["batches"], "known_empty_batches": previous["known_empty_batches"],
              "input_snapshot_sha256": binding["input_sha256"][0], "garbled_rules_sha256": rules_hash,
              "source_sha256": previous["source_sha256"], "input_rows": previous["input_rows"],
              "cutoff": cutoff, "batch_rows": batch_rows, "max_buffer_rows": 0, "max_buffer_bytes": 0,
              "retained_rows": 0, "excluded_rows": 0, "failed_rows": 0, "diagnostic_rows": 0,
              "component_rows": 0, "amount_minor_sum": 0}
    artifacts = []
    tallies = {}
    counters = new_counters(previous)
    serial_less = {(member["batch"], member["path"]) for member in previous["detail_members"] if "編號" not in member["header"]}
    detail_items = {item["path"]: item for item in manifest["artifacts"] if item["schema"] == "ingest-detail"}
    group_totals, detail_rows = {}, 0
    for item in manifest["artifacts"]:
        if item["schema"] != "ingest-record":
            continue
        partition = Path(item["path"]).relative_to("records")
        detail_item = detail_items.get(f"details/{partition.as_posix()}")
        if detail_item is None:
            raise ValueError("Ingest snapshot lacks the detail partition of a record partition")
        groups = main_serials(Path(ingest_snapshot) / item["path"], batch_rows)
        for key, group in groups.items():
            group_totals[key] = (group["missing"], sum(len(ids) for ids in group["serials"].values() if len(ids) > 1))
        detail_writer = BatchWriter(stage.build / "details" / partition, "detail", batch_rows)
        try:
            link_details(Path(ingest_snapshot) / detail_item["path"], groups, serial_less, detail_writer, counters, batch_rows)
        finally:
            detail_writer.close()
        artifacts.append((f"details/{partition.as_posix()}", detail_writer.path, "detail", detail_writer.row_count))
        detail_rows += detail_writer.row_count
        report["max_buffer_rows"] = max(report["max_buffer_rows"], detail_writer.max_buffer_rows)
        report["max_buffer_bytes"] = max(report["max_buffer_bytes"], detail_writer.max_buffer_bytes)
        writers = {name: BatchWriter(stage.build / DATASETS[name] / partition, name, batch_rows) for name in DATASETS}
        try:
            for raw in rows(Path(ingest_snapshot) / item["path"], batch_rows):
                output = normalize_record(raw, cutoff, char_map, patterns)
                outcome = output["disposition"][0]["outcome"]
                report[outcome + "_rows"] += 1
                disposition = output["disposition"][0]
                tally = tallies.setdefault((raw["src_batch"], raw["member_path"]),
                                           {"retained": 0, "excluded": 0, "failed": 0, "excluded_by_reason": {}})
                tally[outcome] += 1
                if outcome == "excluded":
                    reasons = tally["excluded_by_reason"]
                    reasons[disposition["reason"]] = reasons.get(disposition["reason"], 0) + 1
                report["diagnostic_rows"] += len(output["diagnostic"])
                report["component_rows"] += len(output["address-component"])
                if output["observation"]:
                    report["amount_minor_sum"] += output["observation"][0]["amount_minor"] or 0
                for name, records in output.items():
                    for record in records:
                        writers[name].add(record)
        finally:
            for writer in writers.values():
                writer.close()
        for name, writer in writers.items():
            relative = f"{DATASETS[name]}/{partition.as_posix()}"
            artifacts.append((relative, writer.path, name, writer.row_count))
            report["max_buffer_rows"] = max(report["max_buffer_rows"], writer.max_buffer_rows)
            report["max_buffer_bytes"] = max(report["max_buffer_bytes"], writer.max_buffer_bytes)
    if report["retained_rows"] + report["excluded_rows"] + report["failed_rows"] != report["input_rows"]:
        raise ValueError("Normalization source accounting mismatch")
    report["member_dispositions"] = member_dispositions(previous["members"], tallies)
    report["detail_rows"] = detail_rows
    report["detail_link"] = detail_link_report(group_totals, counters)
    report["line_mode_members"] = previous.get("line_mode_members", [])
    if sha256_file(rules_path) != rules_hash:
        raise ValueError("Garbled rules changed during normalization")
    for item in manifest["artifacts"]:
        if sha256_file(Path(ingest_snapshot) / item["path"]) != item["sha256"]:
            raise ValueError("Ingest snapshot changed during normalization")
    return stage.finish(artifacts, report)
