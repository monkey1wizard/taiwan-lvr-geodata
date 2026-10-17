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

import duckdb
import pyarrow as pa

from ..results.reconcile import load_p2
from ..storage.parquet import duckdb_config
from ..storage.spill import measured_spill
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
# Attribute columns carried in a row dict (no geometry); CSV and GeoParquet attributes.
ATTRIBUTES = [(n, t, null) for n, t, null in COLUMNS if n != "geometry"]
ATTRIBUTE_NAMES = [n for n, _, _ in ATTRIBUTES]
# CSV: geometry becomes longitude and latitude at the same position.
CSV_COLUMNS = [x for n in COLUMN_NAMES for x in (("longitude", "latitude") if n == "geometry" else (n,))]
ARROW_SCHEMA = pa.schema([pa.field(n, t, nullable=null) for n, t, null in COLUMNS])

# One global order: trade_date (raw fixed-width ROC date, so it sorts like trade_date), category, transaction_id, door_index.
_ORDER_BY = "ORDER BY o.tx_date_raw, o.category, o.src_batch, o.member_path, o.source_row_number, c.ordinal"
# Rows are produced one partition at a time. The partition key is the first five characters of tx_date_raw
# (ROC year and month). A prefix is monotone in the string order, so sorting by (key, full order) equals
# sorting by the full order. A missing or malformed date lands in some partition (NULL last) and is yielded.
_PARTITION_KEY = "left(tx_date_raw, 5)"


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


def make_row(o, ordinal, normalized_address, door_count, lng, lat, build, land, park, address=None):
    """One output row. ``address`` is the final-state address of the door; without it the source rule applies."""
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
        "address": address if address is not None else (o["raw_address"] if door_count == 1 else normalized_address),
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
    # Coordinate sources of a final state (apply_final_state); none for an offline or TGOS state.
    db.execute(
        "CREATE OR REPLACE VIEW overrides AS SELECT NULL::VARCHAR component_id, NULL::VARCHAR address, "
        "NULL::DOUBLE lng, NULL::DOUBLE lat WHERE false"
    )
    if db.execute(
        "SELECT count(*) FROM ((SELECT component_id,raw_record_id FROM components EXCEPT SELECT component_id,raw_record_id FROM occurrences) UNION ALL (SELECT component_id,raw_record_id FROM occurrences EXCEPT SELECT component_id,raw_record_id FROM components))"
    ).fetchone()[0]:
        raise ValueError("Handoff occurrence/source component mismatch")
    if db.execute(
        "SELECT count(*) FROM (SELECT key_version,building_key FROM results GROUP BY 1,2 HAVING count(*)>1)"
    ).fetchone()[0]:
        raise ValueError("Duplicate address index key")


def doors_select(results: str, overrides: str) -> str:
    """SQL of the located doors: one row per address component with a coordinate.

    ``results`` is a relation of address-result rows and ``overrides`` a relation with the columns
    component_id, address, lng and lat (the final-state coordinate sources, or empty). A door with
    an override takes its coordinate and address from it; a door without one needs a located result.
    door_count counts ALL components of the record, located or not.
    """
    return f"""SELECT c.component_id, c.raw_record_id, c.ordinal, c.normalized_address, c.door_count,
                      coalesce(v.lng, r.lng) lng, coalesce(v.lat, r.lat) lat, v.address address_override
               FROM (SELECT component_id, raw_record_id, ordinal, normalized_address, key_version, building_key,
                            count(*) OVER (PARTITION BY raw_record_id) door_count FROM components) c
               LEFT JOIN {results} r ON r.key_version=c.key_version AND r.building_key=c.building_key
                                    AND r.lng IS NOT NULL AND r.lat IS NOT NULL
               LEFT JOIN {overrides} v ON v.component_id=c.component_id
               WHERE coalesce(v.lng, r.lng) IS NOT NULL AND coalesce(v.lat, r.lat) IS NOT NULL"""


def apply_final_state(db, converted, state, final_state):
    """Replace the views results and overrides by those of a final-state snapshot of this converted and state."""
    converted, state, final_state = Path(converted), Path(state), Path(final_state)
    manifest, report = load_p2(final_state, "final-state")
    if (
        report["converted_snapshot_sha256"] != sha256_file(converted / "manifest.json")
        or report["offline_state_manifest_sha256"] != sha256_file(state / "manifest.json")
    ):
        raise ValueError("Final state was not built from this converted snapshot and offline state")
    if json.loads((state / "quality.json").read_text(encoding="utf-8"))["stage"] != "offline-state":
        raise ValueError("Occurrences come from the offline state when a final state is given")

    def one(schema):
        found = [final_state / x["path"] for x in manifest["artifacts"] if x["schema"] == schema]
        if len(found) != 1:
            raise ValueError(f"Expected one {schema} file in the final state, found {len(found)}")
        return str(found[0])

    db.read_parquet(one("final-address-result")).create_view("results", replace=True)
    db.read_parquet(one("coordinate-source")).create_view("coordinate_sources", replace=True)
    db.execute(
        "CREATE OR REPLACE VIEW overrides AS SELECT component_id, after_address address, after_lng lng, "
        "after_lat lat FROM coordinate_sources"
    )


# Materialized once per call, in a DuckDB file under the spill directory, ordered by the partition key k so
# that the zonemaps of each table prune the per-partition reads to one month.
_MATERIALIZE = [
    # k per record; the join below keeps the cardinality of the former filter on the observations view.
    f"""CREATE TABLE ob AS SELECT raw_record_id, src_batch, member_path, source_row_number, category, tx_date_raw,
               raw_address, props_json, {_PARTITION_KEY} k FROM observations ORDER BY k""",
    "CREATE TABLE ob_key AS SELECT DISTINCT raw_record_id, k FROM ob",
    f"""CREATE TABLE doors AS
       SELECT m.k, d.raw_record_id, d.ordinal, d.normalized_address, d.door_count, d.lng, d.lat, d.address_override
       FROM ({doors_select("results", "overrides")}) d
       JOIN ob_key m USING(raw_record_id)
       ORDER BY m.k""",
    *(f"""CREATE TABLE d_{kind} AS
          SELECT m.k, d.raw_record_id, d.source_row_number, d.detail_record_id, d.raw_fields_json
          FROM details d JOIN ob_key m USING(raw_record_id) WHERE d.detail_kind='{kind}' ORDER BY m.k"""
      for kind in ("build", "land", "park")),
]


def _detail_view(kind):
    return (
        "(SELECT raw_record_id, list(raw_fields_json ORDER BY source_row_number, detail_record_id) j "
        f"FROM d_{kind} WHERE {{match}} GROUP BY 1)"
    )


_PARTITION_SQL = f"""
    SELECT o.src_batch, o.member_path, o.source_row_number, o.category, o.tx_date_raw, o.raw_address, o.props_json,
           c.ordinal, c.normalized_address, c.door_count, c.lng, c.lat, b.j, l.j, p.j, c.address_override
    FROM (SELECT * FROM ob WHERE {{match}}) o
    JOIN (SELECT * FROM doors WHERE {{match}}) c USING(raw_record_id)
    LEFT JOIN {_detail_view("build")} b USING(raw_record_id)
    LEFT JOIN {_detail_view("land")} l USING(raw_record_id)
    LEFT JOIN {_detail_view("park")} p USING(raw_record_id)
    {_ORDER_BY}"""
_NAMES = ["src_batch", "member_path", "source_row_number", "category", "tx_date_raw", "raw_address", "props_json"]


def iter_door_rows(converted, state, *, spill_dir, batch_rows=1024, final_state=None):
    """Stream one dict per located door in the global order trade_date, category, transaction_id, door_index.

    The sources are materialized once into a DuckDB file under spill_dir, then one query per trade month
    keeps memory bounded by a month. DuckDB spills under spill_dir, which is
    created here (it may exist) and emptied of this call's files afterwards. With ``final_state``, the
    located doors and the addresses of updated doors come from that snapshot (the occurrences stay
    those of ``state``, the offline state).
    """
    spill_dir = Path(spill_dir)
    spill_dir.mkdir(parents=True, exist_ok=True)
    with measured_spill(spill_dir, "rows") as spill:
        with duckdb.connect(str(spill.path / "rows.duckdb"), config=duckdb_config(spill.path)) as db:
            open_sources(db, converted, state)
            if final_state is not None:
                apply_final_state(db, converted, state, final_state)
            for sql in _MATERIALIZE:
                db.execute(sql)
            keys = [k for (k,) in db.execute("SELECT DISTINCT k FROM ob ORDER BY k NULLS LAST").fetchall()]
            for key in keys:
                match = "k IS NULL" if key is None else "k = ?"
                cursor = db.execute(_PARTITION_SQL.format(match=match), [] if key is None else [key] * 5)
                while batch := cursor.fetchmany(batch_rows):
                    for v in batch:
                        o = dict(zip(_NAMES, v[:7]))
                        yield make_row(o, v[7], v[8], v[9], v[10], v[11], _details(v[12]), _details(v[13]), _details(v[14]),
                                       address=v[15])
