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
}
PRIMARY = {
    "offline-row": ["evidence_id"],
    "address-pool": ["key_version", "building_key"],
    "address-occurrence": ["component_id"],
    "address-result": ["key_version", "building_key"],
    "verified-alias": ["key_version", "alias_key"],
    "tgos-ledger": ["batch_id", "query_fingerprint"],
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


def valid_coordinate(lng, lat):
    return (
        math.isfinite(lng)
        and math.isfinite(lat)
        and -180 <= lng <= 180
        and -90 <= lat <= 90
    )
