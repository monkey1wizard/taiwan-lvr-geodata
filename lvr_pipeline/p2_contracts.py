"""Typed offline state contracts, independent of consumer geometry formats."""

import json
import math

import pyarrow as pa

from .parquet_io import _schema

S, I, F, B = pa.string(), pa.int64(), pa.float64(), pa.bool_()
SCHEMAS = {
    "offline-row": _schema(
        "offline-row",
        [
            (k, t, n)
            for k, t, n in [
                ("evidence_id", S, False),
                ("key_version", S, False),
                ("building_key", S, True),
                ("normalized_address", S, False),
                ("county_code", S, False),
                ("town_code", S, False),
                ("lng", F, True),
                ("lat", F, True),
                ("validity", S, False),
                ("source_kind", S, False),
                ("source_ref", S, False),
                ("input_sha256", S, False),
                ("source_row_number", I, False),
            ]
        ],
    ),
    "address-pool": _schema(
        "address-pool",
        [
            (k, t, n)
            for k, t, n in [
                ("key_version", S, False),
                ("building_key", S, False),
                ("canonical_address", S, False),
                ("county_code", S, False),
                ("town_code", S, False),
                ("address_family", S, False),
                ("record_count", I, False),
                ("first_seen", I, False),
                ("last_seen", I, False),
            ]
        ],
    ),
    "address-occurrence": _schema(
        "address-occurrence",
        [
            (k, t, n)
            for k, t, n in [
                ("component_id", S, False),
                ("raw_record_id", S, False),
                ("key_version", S, False),
                ("building_key", S, True),
                ("original_key", S, True),
                ("normalized_address", S, False),
                ("county_code", S, False),
                ("town_code", S, False),
                ("reason", S, False),
            ]
        ],
    ),
    "address-result": _schema(
        "address-result",
        [
            (k, t, n)
            for k, t, n in [
                ("key_version", S, False),
                ("building_key", S, False),
                ("canonical_address", S, False),
                ("county_code", S, False),
                ("town_code", S, False),
                ("address_family", S, False),
                ("status", S, False),
                ("lng", F, True),
                ("lat", F, True),
                ("evidence_id", S, True),
                ("evidence_count", I, False),
                ("coordinate_count", I, False),
                ("source_commit", S, False),
            ]
        ],
    ),
    "verified-alias": _schema(
        "verified-alias",
        [
            (k, S, False)
            for k in ["key_version", "alias_key", "target_key", "evidence_ref"]
        ],
    ),
    "tgos-ledger": _schema(
        "tgos-ledger",
        [(k, S, False) for k in ["batch_id", "query_fingerprint", "status"]],
    ),
    "tgos-batch": _schema(
        "tgos-batch",
        [
            ("batch_id", S, False),
            ("service_date", S, False),
            ("status", S, False),
            ("address_count", I, False),
            ("external_used", I, False),
            ("quota_consumed", B, False),
            ("source_state_sha256", S, False),
            ("csv_sha256", S, True),
            ("response_sha256", S, True),
            ("predecessor_batch_id", S, True),
            ("reason", S, True),
        ],
    ),
    "tgos-query": _schema(
        "tgos-query",
        [
            ("batch_id", S, False),
            ("ordinal", I, False),
            ("query_fingerprint", S, False),
            ("key_version", S, False),
            ("building_key", S, False),
            ("address", S, False),
            ("county_code", S, False),
            ("address_family", S, False),
            ("status", S, False),
            ("result_id", S, True),
        ],
    ),
    "tgos-result": _schema(
        "tgos-result",
        [
            ("result_id", S, False),
            ("batch_id", S, False),
            ("query_fingerprint", S, False),
            ("address", S, False),
            ("response_address", S, True),
            ("lng", F, True),
            ("lat", F, True),
            ("status", S, False),
            ("response_sha256", S, False),
            ("source_row_number", I, False),
            ("reason", S, True),
        ],
    ),
    "alias-event": _schema(
        "alias-event",
        [
            ("event_id", S, False),
            ("key_version", S, False),
            ("alias_key", S, False),
            ("target_key", S, False),
            ("action", S, False),
            ("evidence_ref", S, False),
        ],
    ),
}
PRIMARY = {
    "offline-row": ["evidence_id"],
    "address-pool": ["key_version", "building_key"],
    "address-occurrence": ["component_id"],
    "address-result": ["key_version", "building_key"],
    "verified-alias": ["key_version", "alias_key"],
    "tgos-ledger": ["batch_id", "query_fingerprint"],
    "tgos-batch": ["batch_id"],
    "tgos-query": ["batch_id", "query_fingerprint"],
    "tgos-result": ["result_id"],
    "alias-event": ["event_id"],
}
SCHEMAS["unmatched-address"] = _schema(
    "unmatched-address",
    [(f.name, f.type, f.nullable) for f in SCHEMAS["address-result"]],
)
PRIMARY["unmatched-address"] = PRIMARY["address-result"]


def validate_rows(values, dataset):
    if dataset == "unmatched-address":
        if any(row["status"] == "located" for row in values):
            raise ValueError("Located row in unmatched pool")
        dataset = "address-result"
    for row in values:
        schema = SCHEMAS[dataset]
        if any(row[f.name] is None and not f.nullable for f in schema):
            raise ValueError("Null in offline contract")
        if "key_version" in row and row["key_version"] != "v2":
            raise ValueError("Unsupported offline key version")
        if row.get("building_key") is not None:
            if not row["building_key"].startswith("v2:"):
                raise ValueError("Invalid offline key")
            key = json.loads(row["building_key"][3:])
            if key["county"] != row["county_code"]:
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
            expected = (
                "located"
                if row["coordinate_count"] == 1
                else "conflict"
                if row["coordinate_count"] > 1
                else None
            )
            if (
                row["coordinate_count"] < 0
                or row["evidence_count"] < row["coordinate_count"]
            ):
                raise ValueError("Invalid evidence counts")
            if (expected and row["status"] != expected) or (
                not expected and row["status"] not in {"unmatched", "outside_scope"}
            ):
                raise ValueError("Resolution and evidence counts disagree")
            if (row["status"] == "located") != (row["evidence_id"] is not None):
                raise ValueError("Located evidence required")
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
            if not 0 <= row["external_used"] <= 10_000:
                raise ValueError("Invalid external TGOS quota")
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


def valid_coordinate(lng, lat):
    return (
        math.isfinite(lng)
        and math.isfinite(lat)
        and -180 <= lng <= 180
        and -90 <= lat <= 90
    )
