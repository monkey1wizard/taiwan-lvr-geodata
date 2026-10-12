"""Door rows of the public output: one row per located address component (docs/輸出欄位.md)."""

from __future__ import annotations

import csv
import datetime
import json
import math
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
import re
import tempfile

import duckdb
import pyarrow as pa

from ..results.reconcile import load_p2
from ..storage.parquet import duckdb_config
from ..storage.runs import canonical_json, load_snapshot, sha256_file


COUNTY_LETTERS = (
    Path(__file__).resolve().parent.parent.parent / "config" / "reference" / "lvr_county_letters.csv"
)
CATEGORIES = ("sales", "presale", "rent")
BATCH = re.compile(r"[0-9]{3}q[1-4]")

# Contract table "欄位", numbered 1..22 with geometry at 18. Name, Arrow type, nullable.
COLUMNS = [
    ("transaction_id", pa.string(), False),
    ("door_index", pa.int64(), False),
    ("door_count", pa.int64(), False),
    ("address", pa.string(), False),
    ("transaction_addresses", pa.string(), False),
    ("category", pa.string(), False),
    ("trade_date", pa.string(), True),
    ("county", pa.string(), True),
    ("district", pa.string(), True),
    ("transaction_target", pa.string(), True),
    ("building_type", pa.string(), True),
    ("price_twd", pa.int64(), True),
    ("unit_price_sqm", pa.int64(), True),
    ("area_m2_decimal", pa.float64(), True),
    ("parking_type", pa.string(), True),
    ("parking_price_twd", pa.int64(), True),
    ("parking_area_m2", pa.float64(), True),
    ("geometry", pa.binary(), False),
    ("props_json", pa.string(), False),
    ("build_json", pa.string(), False),
    ("land_json", pa.string(), False),
    ("park_json", pa.string(), False),
]
COLUMN_NAMES = [name for name, _, _ in COLUMNS]
# Attribute columns carried in a row dict (no geometry); GeoJSON properties and GPKG attributes.
ATTRIBUTES = [(n, t, null) for n, t, null in COLUMNS if n != "geometry"]
ATTRIBUTE_NAMES = [n for n, _, _ in ATTRIBUTES]
# CSV: geometry becomes longitude and latitude at the same position.
CSV_COLUMNS = [x for n in COLUMN_NAMES for x in (("longitude", "latitude") if n == "geometry" else (n,))]
ARROW_SCHEMA = pa.schema([pa.field(n, t, nullable=null) for n, t, null in COLUMNS])

# SQL expressions behind the order_by names; tx_date_raw is a fixed-width ROC date, so it sorts like trade_date.
_ORDER = {
    "trade_date": "o.tx_date_raw",
    "category": "o.category",
    "transaction_id": "o.src_batch, o.member_path, o.source_row_number",
    "door_index": "c.ordinal",
}
DEFAULT_ORDER = ("trade_date", "category", "transaction_id", "door_index")


@lru_cache(maxsize=1)
def county_letters():
    with COUNTY_LETTERS.open(encoding="utf-8", newline="") as stream:
        return {r["letter"]: r["county"] for r in csv.DictReader(stream)}


def county_of(member_path):
    base = (member_path or "").replace("\\", "/").rsplit("/", 1)[-1]
    if len(base) > 1 and base[1] == "_":
        return county_letters().get(base[0].lower())
    return None


def roc_date(value):
    value = (value or "").strip()
    if len(value) != 7 or not value.isdigit():
        return None
    try:
        return datetime.date(int(value[:3]) + 1911, int(value[3:5]), int(value[5:])).isoformat()
    except ValueError:
        return None


_NUMBER = re.compile(r"\d+(\.\d+)?")


def _decimal(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        text = repr(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    if not _NUMBER.fullmatch(text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_int(value):
    number = _decimal(value)
    if number is None or number != number.to_integral_value():
        return None
    number = int(number)
    return number if number < 2**63 else None


def parse_float(value):
    number = _decimal(value)
    if number is None:
        return None
    number = float(number)
    return number if math.isfinite(number) else None


def make_transaction_id(src_batch, member_path, source_row_number):
    """`{src_batch}/{member_path}/{source_row_number}`; a violation is a program error."""
    if not isinstance(src_batch, str) or not BATCH.fullmatch(src_batch):
        raise ValueError(f"Invalid src_batch for transaction_id: {src_batch!r}")
    if not isinstance(member_path, str) or not member_path or "/" in member_path:
        raise ValueError(f"Invalid member_path for transaction_id: {member_path!r}")
    if isinstance(source_row_number, bool) or not isinstance(source_row_number, int) or source_row_number < 1:
        raise ValueError(f"Invalid source_row_number for transaction_id: {source_row_number!r}")
    return f"{src_batch}/{member_path}/{source_row_number}"


def _text(props, key):
    value = props.get(key)
    return value if isinstance(value, str) and value else None


def _first_present(props, keys):
    for key in keys:
        value = props.get(key)
        if value not in (None, ""):
            return value
    return None


def _details(values):
    """JSON array of the parsed detail objects, already ordered by the query."""
    if not values:
        return "[]"
    return canonical_json([json.loads(v) for v in values])


def make_row(o, ordinal, normalized_address, door_count, lng, lat, build, land, park):
    try:
        props = json.loads(o["props_json"])
    except (TypeError, ValueError):
        props = {}
    if not isinstance(props, dict):
        props = {}
    rent = o["category"] == "rent"
    return {
        "transaction_id": make_transaction_id(o["src_batch"], o["member_path"], o["source_row_number"]),
        "door_index": ordinal + 1,
        "door_count": door_count,
        "address": o["raw_address"] if door_count == 1 else normalized_address,
        "transaction_addresses": o["raw_address"],
        "category": o["category"],
        "trade_date": roc_date(o["tx_date_raw"]),
        "county": county_of(o["member_path"]),
        "district": _text(props, "鄉鎮市區"),
        "transaction_target": _text(props, "交易標的"),
        "building_type": _text(props, "建物型態"),
        "price_twd": parse_int(props.get("總額元" if rent else "總價元")),
        "unit_price_sqm": parse_int(props.get("單價元平方公尺")),
        "area_m2_decimal": parse_float(props.get("建物總面積平方公尺" if rent else "建物移轉總面積平方公尺")),
        "parking_type": _text(props, "車位類別"),
        "parking_price_twd": parse_int(props.get("車位總額元" if rent else "車位總價元")),
        "parking_area_m2": parse_float(
            _first_present(props, ["車位移轉總面積平方公尺", "車位移轉總面積(平方公尺)", "車位面積平方公尺"])
        ),
        "longitude": lng,
        "latitude": lat,
        "props_json": o["props_json"],
        "build_json": build,
        "land_json": land,
        "park_json": park,
    }


def open_sources(db, converted, state):
    """Create views observations, components, details, occurrences and results."""
    converted, state = Path(converted), Path(state)
    cm, _ = load_snapshot(converted, "converted")
    stage = json.loads((state / "quality.json").read_text(encoding="utf-8"))["stage"]
    if stage not in {"offline-state", "tgos-state"}:
        raise ValueError("Unsupported address state stage")
    _, sr = load_p2(state, stage)
    if sr["converted_snapshot_sha256"] != sha256_file(converted / "manifest.json"):
        raise ValueError("Offline state refers to different converted observations")

    def files(schema):
        return [str(converted / x["path"]) for x in cm["artifacts"] if x["schema"] == schema]

    db.read_parquet(files("observation")).create_view("observations")
    db.read_parquet(files("address-component")).create_view("components")
    db.read_parquet(files("detail")).create_view("details")
    db.read_parquet(str(state / "address_occurrences.parquet")).create_view("occurrences")
    db.read_parquet(str(state / "address_index.parquet")).create_view("results")
    if db.execute(
        "SELECT count(*) FROM ((SELECT component_id,raw_record_id FROM components EXCEPT SELECT component_id,raw_record_id FROM occurrences) UNION ALL (SELECT component_id,raw_record_id FROM occurrences EXCEPT SELECT component_id,raw_record_id FROM components))"
    ).fetchone()[0]:
        raise ValueError("Handoff occurrence/source component mismatch")
    if db.execute(
        "SELECT count(*) FROM (SELECT key_version,building_key FROM results GROUP BY 1,2 HAVING count(*)>1)"
    ).fetchone()[0]:
        raise ValueError("Duplicate address index key")


def _detail_view(kind):
    return (
        "(SELECT raw_record_id, list(raw_fields_json ORDER BY source_row_number, detail_record_id) j "
        f"FROM details WHERE detail_kind='{kind}' GROUP BY 1)"
    )


def iter_door_rows(converted, state, *, order_by=DEFAULT_ORDER, batch_rows=1024):
    """Stream one dict per located door, in `order_by` order (names from trade_date, category, transaction_id, door_index)."""
    unknown = [x for x in order_by if x not in _ORDER]
    if unknown:
        raise ValueError(f"Unknown order_by names: {unknown}")
    order = " ORDER BY " + ", ".join(_ORDER[x] for x in order_by) if order_by else ""
    sql = f"""
        SELECT o.src_batch, o.member_path, o.source_row_number, o.category, o.tx_date_raw, o.raw_address, o.props_json,
               c.ordinal, c.normalized_address, c.door_count, r.lng, r.lat, b.j, l.j, p.j
        FROM observations o
        JOIN (SELECT raw_record_id, ordinal, normalized_address, key_version, building_key,
                     count(*) OVER (PARTITION BY raw_record_id) door_count FROM components) c USING(raw_record_id)
        JOIN results r ON r.key_version=c.key_version AND r.building_key=c.building_key
                      AND r.lng IS NOT NULL AND r.lat IS NOT NULL
        LEFT JOIN {_detail_view("build")} b USING(raw_record_id)
        LEFT JOIN {_detail_view("land")} l USING(raw_record_id)
        LEFT JOIN {_detail_view("park")} p USING(raw_record_id)
        {order}"""
    names = ["src_batch", "member_path", "source_row_number", "category", "tx_date_raw", "raw_address", "props_json"]
    with tempfile.TemporaryDirectory(prefix="rows-") as spill:
        with duckdb.connect(config=duckdb_config(spill)) as db:
            open_sources(db, converted, state)
            cursor = db.execute(sql)
            while batch := cursor.fetchmany(batch_rows):
                for v in batch:
                    o = dict(zip(names, v[:7]))
                    yield make_row(o, v[7], v[8], v[9], v[10], v[11], _details(v[12]), _details(v[13]), _details(v[14]))
