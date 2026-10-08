"""Single source of Arrow dataset schemas and primary keys.

``P1_DATASETS`` names the transaction datasets; every other name is an offline,
address or TGOS dataset. No name appears in both groups.
"""
from __future__ import annotations

import pyarrow as pa

def _schema(name, fields):
    return pa.schema([pa.field(key, kind, nullable=nullable) for key, kind, nullable in fields],
                     metadata={b"lvr.dataset": name.encode(), b"lvr.schema_version": b"1.0"})


S, I, F, B = pa.string(), pa.int64(), pa.float64(), pa.bool_()
P1_SCHEMAS = {
    "ingest-record": _schema("ingest-record", [(key, kind, False) for key, kind in [
        ("raw_record_id", S), ("input_sha256", S), ("src_batch", S), ("category", S),
        ("member_path", S), ("source_row_number", I), ("source_line_end", I),
        ("raw_fields_json", S), ("raw_values_json", S), ("header_json", S), ("row_status", S)]]),
    "observation": _schema("observation", [
        ("raw_record_id", S, False), ("schema_version", S, False), ("category", S, False),
        ("src_batch", S, False), ("source_serial", S, True), ("input_sha256", S, False),
        ("member_path", S, False), ("source_row_number", I, False), ("record_grain", S, False),
        ("transaction_key", S, True), ("raw_address", S, False), ("tx_date_raw", S, True),
        ("tx_yyyymm", I, True), ("run_cutoff_yyyymm", I, False), ("amount_minor", I, True),
        ("area_m2_decimal", S, True), ("props_json", S, False), ("parse_status", S, False),
        ("currency", S, False), ("amount_scale", I, False)]),
    "address-component": _schema("address-component", [
        ("raw_record_id", S, False), ("component_id", S, False), ("ordinal", I, False),
        ("normalized_address", S, False), ("key_version", S, False), ("building_key", S, True),
        ("legacy_building_key", S, True), ("expansion_status", S, False)]),
    "exclusion": _schema("exclusion", [("raw_record_id", S, False), ("reason", S, False), ("source_ref", S, False)]),
    "diagnostic": _schema("diagnostic", [("raw_record_id", S, False), ("code", S, False),
                                           ("detail", S, False), ("source_ref", S, False)]),
    "disposition": _schema("disposition", [(key, kind, False) for key, kind in [
        ("raw_record_id", S), ("input_sha256", S), ("src_batch", S), ("category", S),
        ("member_path", S), ("source_row_number", I), ("source_line_end", I),
        ("raw_fields_json", S), ("raw_values_json", S), ("header_json", S), ("row_status", S),
        ("outcome", S), ("reason", S)]])
}

OFFLINE_SCHEMAS = {
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
            ("status", S, False),
            ("address_count", I, False),
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
    "address-patch": _schema(
        "address-patch",
        [
            ("patch_id", S, False),
            ("key_version", S, False),
            ("building_key", S, False),
            ("full_addr", S, False),
            ("county", S, False),
            ("town", S, False),
            ("village", S, False),
            ("neighborhood", S, False),
            ("road", S, False),
            ("section", S, False),
            ("lane", S, False),
            ("alley", S, False),
            ("sub_alley", S, False),
            ("tong", S, False),
            ("number", S, False),
            ("x", F, False),
            ("y", F, False),
        ],
    ),
    "address-patch-provenance": _schema(
        "address-patch-provenance",
        [
            ("patch_id", S, False),
            ("evidence_id", S, False),
            ("result_id", S, False),
            ("batch_id", S, False),
            ("query_fingerprint", S, False),
            ("submitted_address", S, False),
            ("response_address", S, False),
            ("response_sha256", S, False),
            ("source_row_number", I, False),
            ("source_kind", S, False),
        ],
    ),
    "address-patch-quarantine": _schema(
        "address-patch-quarantine",
        [
            ("candidate_id", S, False),
            ("batch_id", S, False),
            ("query_fingerprint", S, False),
            ("submitted_address", S, False),
            ("response_address", S, True),
            ("reason", S, False),
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
    "address-patch": ["patch_id"],
    "address-patch-provenance": ["patch_id", "evidence_id"],
    "address-patch-quarantine": ["candidate_id"],
}
OFFLINE_SCHEMAS["unmatched-address"] = _schema(
    "unmatched-address",
    [(f.name, f.type, f.nullable) for f in OFFLINE_SCHEMAS["address-result"]],
)
PRIMARY["unmatched-address"] = PRIMARY["address-result"]

# Coordinate adoption detail (R05-4): one row per address key with two or more
# distinct valid coordinates. coordinates_json lists every distinct coordinate
# with its source row count, smallest evidence_id and distance sum (metres).
OFFLINE_SCHEMAS["coordinate-resolution"] = _schema(
    "coordinate-resolution",
    [
        ("key_version", S, False),
        ("building_key", S, False),
        ("status", S, False),
        ("resolution_basis", S, False),
        ("tolerance_m", F, False),
        ("max_distance_m", F, False),
        ("coordinate_count", I, False),
        ("evidence_count", I, False),
        ("lng", F, True),
        ("lat", F, True),
        ("evidence_id", S, True),
        ("coordinates_json", S, False),
    ],
)
PRIMARY["coordinate-resolution"] = ["key_version", "building_key"]

# Review table (R05-8): every address that has no key, no location or needs a person's review.
# All reason codes are defined here; a stage that starts producing a new kind of review row
# appends its code to this tuple. Rows never remove or replace the source observation.
EXCEPTION_REASON_CODES = (
    "invalid_admin",
    "district_missing",
    "district_ambiguous",
    "road_only_not_unique",
    "garbled_pending",
    "subdoor_variant_pair",
    "offline_unmatched",
    "coordinate_conflict",
    "tgos_isolated",
    # R05-8b
    "address_review",
    "tgos_response_incomplete",
    "tgos_coordinate_invalid",
)
OFFLINE_SCHEMAS["exception-address"] = _schema(
    "exception-address",
    [
        ("exception_id", S, False),
        ("reason_code", S, False),
        ("first_run_id", S, False),
        ("component_id", S, False),
        ("raw_record_id", S, False),
        ("src_batch", S, False),
        ("category", S, False),
        ("raw_address", S, False),
        ("normalized_address", S, False),
        ("building_key", S, True),
        ("county_code", S, True),
        ("town_code", S, True),
        ("source_district", S, True),
        ("related_json", S, True),
    ],
)
PRIMARY["exception-address"] = ["exception_id"]

overlap = set(P1_SCHEMAS) & set(OFFLINE_SCHEMAS)
if overlap:
    raise RuntimeError(f"Dataset defined twice: {sorted(overlap)}")
P1_DATASETS = frozenset(P1_SCHEMAS)
SCHEMAS = {**P1_SCHEMAS, **OFFLINE_SCHEMAS}
del P1_SCHEMAS, OFFLINE_SCHEMAS


def dataset_schema(name):
    if name not in SCHEMAS:
        raise ValueError("Unknown dataset schema")
    return SCHEMAS[name]
