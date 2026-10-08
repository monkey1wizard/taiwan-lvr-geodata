"""Per-observation normalization without guessing transaction identity or doors."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re

from ..addresses import identity
from ..addresses.identity import building_key, building_key_v2
from ..addresses.parse import arab_to_cjk, is_garbled, norm
from ..contracts import component_id
from ..addresses.characters import fix_garbled, load_garbled
from .read import FIELD_ALIASES
from ..storage.parquet import BatchWriter, rows
from ..storage.runs import Stage, bindings, canonical_json, load_snapshot
from ..storage.runs import sha256_file
from .dates import validated_roc_to_tx_yyyymm

DATASETS = {"observation": "observations", "address-component": "address_components",
            "exclusion": "exclusions", "diagnostic": "diagnostics", "disposition": "dispositions"}
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
        return [address], building_key_v2(address) is not None
    if len(parts) > 64 or any(not part for part in parts):
        return [address], False
    first = parts[0]
    if "號" not in first:
        first += "號"
    match = re.fullmatch(r"(.+?)([0-9]+(?:之[0-9]+)*)號(?:之[0-9]+)*", first)
    if not match or building_key_v2(first) is None:
        return [address], False
    result = [first]
    for part in parts[1:]:
        if re.fullmatch(r"[0-9]+(?:之[0-9]+)*號?(?:之[0-9]+)*", part):
            part = match[1] + part
            if "號" not in part:
                part += "號"
        if building_key_v2(part) is None:
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
            "building_key": building_key_v2(member_address) if confirmed else None,
            "legacy_building_key": building_key(member_address), "expansion_status": "confirmed" if confirmed else "review"})
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
    for item in manifest["artifacts"]:
        if item["schema"] != "ingest-record":
            continue
        partition = Path(item["path"]).relative_to("records")
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
    report["line_mode_members"] = previous.get("line_mode_members", [])
    if sha256_file(rules_path) != rules_hash:
        raise ValueError("Garbled rules changed during normalization")
    for item in manifest["artifacts"]:
        if sha256_file(Path(ingest_snapshot) / item["path"]) != item["sha256"]:
            raise ValueError("Ingest snapshot changed during normalization")
    return stage.finish(artifacts, report)
