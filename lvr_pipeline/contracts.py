"""P0 observation identity and versioned JSON row validation.

No transaction merging or revision selection is implemented here.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"
SCHEMA_VERSION = "1.0"


def observation_id(input_sha256: str, member_path: str, row_number: int) -> str:
    if len(input_sha256) != 64 or any(c not in "0123456789abcdef" for c in input_sha256):
        raise ValueError("Expected lowercase SHA-256")
    if not member_path or isinstance(row_number, bool) or not isinstance(row_number, int) or row_number < 1:
        raise ValueError("Expected a member path and positive physical row number")
    identity = json.dumps([input_sha256, member_path, row_number], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def component_id(raw_record_id: str, ordinal: int) -> str:
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise ValueError("Expected nonnegative expansion ordinal")
    return hashlib.sha256(json.dumps([raw_record_id, ordinal], separators=(",", ":")).encode()).hexdigest()


@lru_cache(maxsize=8)
def schema_validator(schema_name: str) -> Draft202012Validator:
    if schema_name not in {"observation", "address-component", "exclusion", "diagnostic"}:
        raise ValueError("Unknown schema")
    schema = json.loads((SCHEMAS / f"{schema_name}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_rows(rows: list[dict], schema_name: str) -> None:
    validator = schema_validator(schema_name)
    primary = "component_id" if schema_name == "address-component" else "raw_record_id"
    seen: set[str] = set()
    for row in rows:
        validator.validate(row)
        if schema_name == "observation":
            if row["raw_record_id"] != observation_id(row["input_sha256"], row["member_path"], row["source_row_number"]):
                raise ValueError("Observation identity does not match source lineage")
            from .tx_date import validated_roc_to_tx_yyyymm
            expected_month = validated_roc_to_tx_yyyymm(row["tx_date_raw"], run_cutoff_yyyymm=row["run_cutoff_yyyymm"])
            if row["tx_yyyymm"] != expected_month:
                raise ValueError("Transaction month does not match the fixed date rule")
        if schema_name == "address-component":
            if row["component_id"] != component_id(row["raw_record_id"], row["ordinal"]):
                raise ValueError("Component identity mismatch")
            from .address import building_key_v2
            # A component held for review has no key until its members are proven.
            expected = building_key_v2(row["normalized_address"]) if row["expansion_status"] == "confirmed" else None
            if row["building_key"] != expected:
                raise ValueError("Component key does not match v2 rule")
        if schema_name != "diagnostic" and row[primary] in seen:
            raise ValueError(f"Duplicate {primary}")
        seen.add(row[primary])


def validate_relations(observations: list[dict], components: list[dict], exclusions: list[dict]) -> None:
    for name, rows in [("observation", observations), ("address-component", components), ("exclusion", exclusions)]:
        validate_rows(rows, name)
    ids = {row["raw_record_id"] for row in observations}
    if ids & {row["raw_record_id"] for row in exclusions}:
        raise ValueError("An observation cannot also be excluded")
    ordinals: set[tuple[str, int]] = set()
    for row in components:
        if row["raw_record_id"] not in ids:
            raise ValueError("Orphan address component")
        identity = row["raw_record_id"], row["ordinal"]
        if identity in ordinals:
            raise ValueError("Duplicate expansion ordinal")
        ordinals.add(identity)
