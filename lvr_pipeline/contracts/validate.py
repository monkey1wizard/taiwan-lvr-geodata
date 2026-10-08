"""P0 observation identity and versioned JSON row validation.

No transaction merging or revision selection is implemented here.
"""
from __future__ import annotations

import hashlib
import json
import math
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

from .schemas import SCHEMAS as DATASET_SCHEMAS

SCHEMAS = Path(__file__).parent / "json"
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


_RECOMPUTE_KEYS: ContextVar[bool] = ContextVar("recompute_component_keys", default=True)


@contextmanager
def keys_not_recomputed():
    """Validate rows from a snapshot made under another NORMALIZATION_VERSION: check structure, skip the key rule."""
    token = _RECOMPUTE_KEYS.set(False)
    try:
        yield
    finally:
        _RECOMPUTE_KEYS.reset(token)


def validate_rows(rows: list[dict], schema_name: str) -> None:
    validator = schema_validator(schema_name)
    primary = "component_id" if schema_name == "address-component" else "raw_record_id"
    seen: set[str] = set()
    for row in rows:
        validator.validate(row)
        if schema_name == "observation":
            if row["raw_record_id"] != observation_id(row["input_sha256"], row["member_path"], row["source_row_number"]):
                raise ValueError("Observation identity does not match source lineage")
            from ..transactions.dates import validated_roc_to_tx_yyyymm
            expected_month = validated_roc_to_tx_yyyymm(row["tx_date_raw"], run_cutoff_yyyymm=row["run_cutoff_yyyymm"])
            if row["tx_yyyymm"] != expected_month:
                raise ValueError("Transaction month does not match the fixed date rule")
        if schema_name == "address-component":
            if row["component_id"] != component_id(row["raw_record_id"], row["ordinal"]):
                raise ValueError("Component identity mismatch")
            from ..addresses.identity import building_key_v2
            # A component held for review has no key until its members are proven.
            expected = building_key_v2(row["normalized_address"]) if row["expansion_status"] == "confirmed" else None
            if _RECOMPUTE_KEYS.get() and row["building_key"] != expected:
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


def _validate_coordinate_resolution(row):
    """Recompute the R05-4 adoption rule from the listed coordinates."""
    from ..results.reconcile import (DOOR_CROSS_CHECK, EXCEEDS_TOLERANCE, WITHIN_TOLERANCE,
                                     adopt_coordinates)

    basis = {"located": {WITHIN_TOLERANCE}, "conflict": {EXCEEDS_TOLERANCE, DOOR_CROSS_CHECK}}
    if row["resolution_basis"] not in basis.get(row["status"], set()):
        raise ValueError("Invalid coordinate resolution status or basis")
    if row["coordinate_count"] < 2 or row["evidence_count"] < row["coordinate_count"]:
        raise ValueError("Invalid coordinate resolution counts")
    try:
        listed = json.loads(row["coordinates_json"])
        points = {(item["lng"], item["lat"]): item for item in listed}
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("Invalid coordinate resolution list") from exc
    if len(points) != len(listed) or len(points) != row["coordinate_count"]:
        raise ValueError("Coordinate resolution list differs from counts")
    if sum(item["evidence_count"] for item in listed) != row["evidence_count"]:
        raise ValueError("Coordinate resolution list differs from counts")
    adoption = adopt_coordinates(points, row["tolerance_m"])
    # R04-12: coordinates from several doors (village, neighbourhood) are never adopted.
    if any("doors" in item for item in listed):
        doors = {(door["village"], door["neighborhood"]) for item in listed for door in item.get("doors", [])}
        if len(doors) > 1:
            adoption = {**adoption, "status": "conflict", "representative": None,
                        "resolution_basis": DOOR_CROSS_CHECK}
    elif row["resolution_basis"] == DOOR_CROSS_CHECK:
        raise ValueError("Door cross-check needs the listed doors")
    if adoption["resolution_basis"] != row["resolution_basis"]:
        raise ValueError("Coordinate resolution differs from adoption rule")
    representative = adoption["representative"]
    if (
        adoption["status"] != row["status"]
        or adoption["max_distance_m"] != row["max_distance_m"]
        or (row["lng"], row["lat"]) != (representative or (None, None))
        or row["evidence_id"]
        != (points[representative]["evidence_id"] if representative else None)
    ):
        raise ValueError("Coordinate resolution differs from adoption rule")


def validate_dataset_rows(values, dataset):
    if dataset == "unmatched-address":
        if any(row["status"] == "located" for row in values):
            raise ValueError("Located row in unmatched pool")
        dataset = "address-result"
    for row in values:
        schema = DATASET_SCHEMAS[dataset]
        if any(row[f.name] is None and not f.nullable for f in schema):
            raise ValueError("Null in offline contract")
        if "key_version" in row and row["key_version"] != "v2":
            raise ValueError("Unsupported offline key version")
        if row.get("building_key") is not None:
            if not row["building_key"].startswith("v2:"):
                raise ValueError("Invalid offline key")
            key = json.loads(row["building_key"][3:])
            row_county = row.get("county_code", row.get("county"))
            if row_county is not None and key["county"] != row_county:
                raise ValueError("Offline key county differs")
        if dataset == "offline-row" and row["validity"] not in {
            "valid",
            "invalid_address",
            "invalid_admin",
            "invalid_coordinate",
        }:
            raise ValueError("Invalid offline evidence status")
        if dataset == "address-result" and row["status"] not in {
            "located",
            "conflict",
            "unmatched",
            "outside_scope",
        }:
            raise ValueError("Invalid address resolution status")
        if dataset == "address-result":
            # R05-4: several distinct coordinates are located when all lie within the
            # tolerance; the coordinate-resolution dataset carries that evidence.
            expected = (
                {"located"}
                if row["coordinate_count"] == 1
                else {"located", "conflict"}
                if row["coordinate_count"] > 1
                else {"unmatched", "outside_scope"}
            )
            if (
                row["coordinate_count"] < 0
                or row["evidence_count"] < row["coordinate_count"]
            ):
                raise ValueError("Invalid evidence counts")
            if row["status"] not in expected:
                raise ValueError("Resolution and evidence counts disagree")
            if (row["status"] == "located") != (row["evidence_id"] is not None):
                raise ValueError("Located evidence required")
        if dataset == "coordinate-resolution":
            _validate_coordinate_resolution(row)
        if "lng" in row:
            coordinate = row["lng"], row["lat"]
            if (coordinate[0] is None) != (coordinate[1] is None):
                raise ValueError("Partial coordinate pair")
            if coordinate[0] is not None and not valid_coordinate(*coordinate):
                raise ValueError("Invalid coordinate")
            if dataset == "address-result" and (row["status"] == "located") != (
                coordinate[0] is not None
            ):
                raise ValueError("Resolution and coordinate disagree")
        if dataset == "tgos-batch":
            if row["status"] not in {
                "prepared",
                "submission_unknown",
                "submitted",
                "cancelled",
                "completed",
            }:
                raise ValueError("Invalid TGOS batch status")
            if not 1 <= row["address_count"] <= 10_000:
                raise ValueError("Invalid TGOS batch size")
        if dataset == "tgos-query" and row["status"] not in {
            "prepared",
            "submission_unknown",
            "submitted",
            "succeeded",
            "failed",
            "rejected",
            "conflict",
            "cancelled",
        }:
            raise ValueError("Invalid TGOS query status")
        if dataset == "tgos-result" and row["status"] not in {
            "succeeded",
            "failed",
            "rejected",
        }:
            raise ValueError("Invalid TGOS result status")
        if dataset == "alias-event" and row["action"] not in {"verified", "revoked"}:
            raise ValueError("Invalid alias event")
        if dataset == "address-patch":
            if row["key_version"] != "v2" or not row["building_key"].startswith("v2:"):
                raise ValueError("Invalid address patch key")
            if not all(row[key] for key in ["patch_id", "full_addr", "county", "town", "village", "number"]):
                raise ValueError("Address patch lacks required administrative evidence")
            if not valid_coordinate(row["x"], row["y"]):
                raise ValueError("Invalid address patch coordinate")
        if dataset == "address-patch-provenance":
            if row["source_kind"] != "tgos_result":
                raise ValueError("Unsupported address patch source")
        if dataset == "address-patch-quarantine" and not row["reason"]:
            raise ValueError("Address patch quarantine reason required")
        if dataset == "exception-address":
            from .schemas import EXCEPTION_REASON_CODES
            if row["reason_code"] not in EXCEPTION_REASON_CODES:
                raise ValueError("Unknown exception reason code")
            if len(row["exception_id"]) != 64 or any(c not in "0123456789abcdef" for c in row["exception_id"]):
                raise ValueError("Invalid exception identifier")
            if row["related_json"] is not None:
                json.loads(row["related_json"])


def valid_coordinate(lng, lat):
    return (
        math.isfinite(lng)
        and math.isfinite(lat)
        and -180 <= lng <= 180
        and -90 <= lat <= 90
    )
