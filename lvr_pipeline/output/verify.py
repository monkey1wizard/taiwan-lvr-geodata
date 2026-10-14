"""Verify a written output against docs/輸出欄位.md and its catalog.json.

verify_output collects every failure before it raises OutputVerificationError. GeoParquet is the
reference: every other format must hold the same rows with the same values, year by year and
category by category. Large state lives in DuckDB (GeoParquet) or in digest sets (one year at a time).
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import struct
import tempfile

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from ..addresses.admin import admin_towns_sha256
from ..addresses.identity import NORMALIZATION_VERSION
from ..storage.parquet import duckdb_config
from ..storage.runs import sha256_file
from .catalog import CATALOG_NAME, CONTRACT_VERSION, CURRENCY, FORMATS, PARQUET_NAME, cutoff_text
from .gpkg import GEOMETRY_COLUMN, SRS_ID, _sqlite_type
from .rows import ATTRIBUTE_NAMES, ATTRIBUTES, CATEGORIES, COLUMNS, CSV_COLUMNS

FILE_LIMIT_BYTES = 100 * 1024 * 1024  # GitHub hard limit for a file in a repository
WARN_BYTES = 50 * 1024 * 1024  # GitHub warns above this size
MAX_FAILURES_PER_CHECK = 50
CHECKS = ("catalog", "columns", "metadata", "keys", "cross-format", "transaction", "month", "resources")
ID_PATTERN = re.compile(r"[0-9]{3}q[1-4]/[^/]+/[1-9][0-9]*")
SHA256 = re.compile(r"[0-9a-f]{64}")
QUALITY_COLUMNS = ("price_twd", "unit_price_sqm", "area_m2_decimal", "trade_date")



def _kind(arrow_type):
    if pa.types.is_string(arrow_type):
        return "string"
    return "int" if pa.types.is_integer(arrow_type) else "float"


KINDS = [_kind(t) for _, t, _ in ATTRIBUTES]
INDEX = {n: i for i, n in enumerate(ATTRIBUTE_NAMES)}
NONDOOR = [i for n, i in INDEX.items() if n not in ("door_index", "address")]
NONDOOR_NAMES = [n for n in ATTRIBUTE_NAMES if n not in ("door_index", "address")]
CSV_POSITION = {n: i for i, n in enumerate(CSV_COLUMNS)}
GPKG_TABLES = [("fid", "INTEGER"), (GEOMETRY_COLUMN, "POINT")] + [(n, _sqlite_type(t)) for n, t, _ in ATTRIBUTES]
GPKG_BLOB_HEAD = b"GP\x00\x01" + struct.pack("<i", SRS_ID)
WKB_POINT = struct.pack("<BI", 1, 1)


class OutputVerificationError(ValueError):
    def __init__(self, report):
        self.report = report
        failures = report["failures"]
        first = failures[0] if failures else {"check": "?", "path": "", "detail": ""}
        names = sorted({f["check"] for f in failures})
        super().__init__(
            f"output verification failed: {len(failures)} failures in {names}; "
            f"first: {first['check']} {first['path']}: {first['detail']}"
        )


class _Unreadable(Exception):
    pass


class _Report:
    def __init__(self):
        self.failures = []
        self.counts = dict.fromkeys(CHECKS, 0)
        self.warnings = []

    def fail(self, check, path, detail):
        self.counts[check] += 1
        if self.counts[check] <= MAX_FAILURES_PER_CHECK:
            self.failures.append({"check": check, "path": str(path), "detail": str(detail)})


class _Notes:
    """Per-file problem collector: first three examples per check, then a count."""

    def __init__(self, report, label):
        self.report, self.label, self.counts = report, label, {}

    def note(self, check, detail):
        n = self.counts[check] = self.counts.get(check, 0) + 1
        if n <= 3:
            self.report.fail(check, self.label, detail)

    def flush(self):
        for check, n in self.counts.items():
            if n > 3:
                self.report.fail(check, self.label, f"{n - 3} more of the same check")


class _Acc:
    """Digest set of the rows of one source (GeoParquet subset, or all files of one format group)."""

    def __init__(self):
        self.digests, self.keys, self.count, self.unreadable = set(), set(), 0, False


def _digest(text, size=16):
    return hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=size).digest()


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _bad(value):
    return ("!type", repr(value))


def _norm(kind, value):
    """Typed value as the contract defines it. Empty string equals null. A wrong type becomes a marker."""
    if value is None:
        return None
    if kind == "string":
        return (value or None) if isinstance(value, str) else _bad(value)
    if isinstance(value, bool):
        return _bad(value)
    if kind == "int":
        return value if isinstance(value, int) else _bad(value)
    return float(value) if isinstance(value, (int, float)) else _bad(value)


def _norm_text(kind, text):
    if text == "":
        return None
    if kind == "string":
        return text
    try:
        return int(text) if kind == "int" else float(text)
    except ValueError:
        return _bad(text)


def _wkb_point(blob):
    if isinstance(blob, (bytes, bytearray)) and len(blob) == 21 and blob[:5] == WKB_POINT:
        return struct.unpack("<dd", blob[5:])
    return _bad(blob), _bad(blob)


def _gpkg_point(blob):
    if isinstance(blob, (bytes, bytearray)) and len(blob) == 29 and blob[:8] == GPKG_BLOB_HEAD:
        return _wkb_point(bytes(blob[8:]))
    return _bad(blob), _bad(blob)


# --- readers: each yields tuples of the 21 attribute values followed by longitude and latitude ---

def _read_parquet(con, year):
    cols = ", ".join(f'"{n}"' for n in ATTRIBUTE_NAMES)
    cursor = con.cursor().execute(
        f"SELECT {cols}, geometry FROM p WHERE trade_date >= ? AND trade_date < ?",
        [f"{year:04d}-01-01", f"{year + 1:04d}-01-01"],
    )
    while batch := cursor.fetchmany(4096):
        for row in batch:
            x, y = _wkb_point(row[-1])
            yield tuple(None if v == "" else v for v in row[:-1]) + (x, y)


def _read_gpkg(path, category):
    def reader(note):
        db = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
        try:
            cols = ", ".join(f'"{n}"' for n in ATTRIBUTE_NAMES)
            for row in db.execute(f'SELECT {GEOMETRY_COLUMN}, {cols} FROM "{category}" ORDER BY fid'):
                x, y = _gpkg_point(row[0])
                yield tuple(_norm(k, v) for k, v in zip(KINDS, row[1:])) + (x, y)
        finally:
            db.close()

    return reader


def _read_csv(path):
    def reader(note):
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:
            rows = csv.reader(stream)
            header = next(rows, None)
            if header != CSV_COLUMNS:
                note("columns", f"CSV header differs from the contract: {header!r}")
                raise _Unreadable
            positions = [CSV_POSITION[n] for n in ATTRIBUTE_NAMES]
            lng, lat = CSV_POSITION["longitude"], CSV_POSITION["latitude"]
            for number, row in enumerate(rows, 2):
                if len(row) != len(CSV_COLUMNS):
                    note("columns", f"line {number} has {len(row)} cells, expected {len(CSV_COLUMNS)}")
                    continue
                yield (
                    tuple(_norm_text(k, row[p]) for k, p in zip(KINDS, positions))
                    + tuple(_norm_text("float", row[p]) for p in (lng, lat))
                )

    return reader


def _read_geojson(path):
    def reader(note):
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("type") != "FeatureCollection" or not isinstance(
            document.get("features"), list
        ):
            note("columns", "not a FeatureCollection with a features array")
            raise _Unreadable
        if "crs" in document:
            note("metadata", "GeoJSON has a crs member (RFC 7946 forbids it)")
        for number, feature in enumerate(document["features"], 1):
            props = feature.get("properties") if isinstance(feature, dict) else None
            if not isinstance(props, dict) or feature.get("type") != "Feature":
                note("columns", f"feature {number} is not a Feature with properties")
                continue
            if set(feature) != {"type", "id", "geometry", "properties"}:
                note("columns", f"feature {number} members are {list(feature)}")
            if list(props) != ATTRIBUTE_NAMES:
                note("columns", f"feature {number} property keys or order differ from the contract: {list(props)}")
            if feature.get("id") != f"{props.get('transaction_id')}#{props.get('door_index')}":
                note("keys", f"feature {number} id {feature.get('id')!r} is not transaction_id#door_index")
            geometry = feature.get("geometry")
            coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
            if (
                geometry.get("type") != "Point" if isinstance(geometry, dict) else True
            ) or not isinstance(coords, list) or len(coords) != 2:
                note("columns", f"feature {number} geometry is not a Point with two coordinates")
                x = y = _bad(geometry)
            else:
                x, y = (_norm("float", c) for c in coords)
            yield tuple(_norm(k, props.get(n)) for k, n in zip(KINDS, ATTRIBUTE_NAMES)) + (x, y)

    return reader


# --- scanning ---

def _scan(report, label, reader, acc, *, year=None, month=None, category=None):
    """Read one file (or layer) into acc, apply the per-row checks and return its row count or None."""
    notes = _Notes(report, label)
    count, last_tid, last_nd, seen = 0, None, None, {}
    date_i, cat_i = INDEX["trade_date"], INDEX["category"]
    try:
        for v in reader(notes.note):
            count += 1
            tid, door, door_count = v[0], v[1], v[2]
            if not isinstance(tid, str) or not ID_PATTERN.fullmatch(tid):
                notes.note("keys", f"transaction_id {tid!r} does not match the contract format")
            if not (_is_int(door) and _is_int(door_count) and 1 <= door <= door_count):
                notes.note("keys", f"door_index {door!r} is not within 1..door_count {door_count!r} in {tid}")
            key = _digest(repr((tid, door)), 8)
            if key in acc.keys:
                notes.note("keys", f"duplicate key {tid}#{door}")
            acc.keys.add(key)
            nd = tuple(v[i] for i in NONDOOR)
            if tid == last_tid:
                if nd != last_nd:
                    names = [n for n, a, b in zip(NONDOOR_NAMES, nd, last_nd) if a != b]
                    notes.note("transaction", f"{tid} differs between its rows in {names}")
            else:
                digest = _digest(repr(nd), 8)
                if seen.setdefault(tid, digest) != digest:
                    notes.note("transaction", f"{tid} differs between non-adjacent rows")
            last_tid, last_nd = tid, nd
            if year is not None:
                date, row_category = v[date_i], v[cat_i]
                prefix = f"{year:04d}" if month is None else f"{year:04d}-{month:02d}"
                if not isinstance(date, str) or not date.startswith(prefix):
                    notes.note("month", f"{tid}#{door} has trade_date {date!r}, outside {prefix}")
                if category is not None and row_category != category:
                    notes.note("month", f"{tid}#{door} has category {row_category!r}, file holds {category}")
            acc.digests.add(_digest(repr(v)))
            acc.count += 1
    except _Unreadable:
        acc.unreadable = True
        return None
    except (OSError, ValueError, UnicodeError, sqlite3.Error, csv.Error, AttributeError, TypeError) as exc:
        acc.unreadable = True
        report.fail("columns", label, f"unreadable: {type(exc).__name__}: {exc}")
        return None
    finally:
        notes.flush()
    return count


def _stream(factories):
    for factory in factories:
        yield from factory(lambda *a: None)


def _fetch(factories, wanted):
    found = {}
    for v in _stream(factories):
        if (v[0], v[1]) in wanted:
            found[(v[0], v[1])] = v
    return found


def _explain(expected, actual):
    """Name missing, extra and differing keys between two row streams (called only after a mismatch)."""
    def digests(factories):
        result = {}
        for v in _stream(factories):
            result[(v[0], v[1])] = _digest(repr(v))
        return result

    de, da = digests(expected), digests(actual)
    missing = [k for k in de if k not in da]
    extra = [k for k in da if k not in de]
    differ = [k for k in de if k in da and de[k] != da[k]]
    parts = [f"missing {len(missing)}, extra {len(extra)}, differing {len(differ)}"]
    if missing:
        parts.append("e.g. missing " + ", ".join(f"{a}#{b}" for a, b in missing[:3]))
    if extra:
        parts.append("e.g. extra " + ", ".join(f"{a}#{b}" for a, b in extra[:3]))
    if differ:
        wanted = set(differ[:3])
        left, right = _fetch(expected, wanted), _fetch(actual, wanted)
        names = ATTRIBUTE_NAMES + ["longitude", "latitude"]
        for key in sorted(wanted):
            columns = [n for n, a, b in zip(names, left[key], right[key]) if a != b]
            parts.append(f"e.g. {key[0]}#{key[1]} differs in {columns}")
    return "; ".join(parts)


# --- entry validation ---

def _expected_path(e):
    fmt, y, m, c = e["format"], e["year"], e["month"], e["category"]
    if fmt == "geoparquet":
        return PARQUET_NAME
    if fmt == "gpkg":
        return f"gpkg/taiwanlvr_{y:04d}.gpkg"
    if fmt == "csv":
        stem = f"{y:04d}" if m is None else f"{y:04d}{m:02d}"
        return f"csv/{y:04d}/taiwanlvr_{stem}_{c}.csv"
    return f"geojson/{y:04d}/taiwanlvr_{y:04d}{m:02d}_{c}.geojson"


def _entry_problem(e):
    if not isinstance(e, dict):
        return "entry is not an object"
    fmt, y, m, c = e.get("format"), e.get("year"), e.get("month"), e.get("category")
    if fmt not in FORMATS:
        return f"unknown format {fmt!r}"
    for name in ("rows", "size_bytes"):
        if not _is_int(e.get(name)) or e[name] < 0:
            return f"{name} is not a non-negative integer"
    if not isinstance(e.get("sha256"), str) or not SHA256.fullmatch(e["sha256"]):
        return "sha256 is not 64 lowercase hex digits"
    if fmt == "geoparquet":
        want = ("all", "release", None, None, None)
    elif fmt == "gpkg":
        want = ("year", "public", y, None, None)
    elif fmt == "csv":
        want = ("year" if m is None else "month", "public", y, m, c)
    else:
        want = ("month", "public", y, m, c)
    if (e.get("period"), e.get("location")) != want[:2]:
        return f"period/location {(e.get('period'), e.get('location'))} does not fit format {fmt}"
    if fmt != "geoparquet":
        if not _is_int(y) or not 1 <= y <= 9998:
            return f"year {y!r} is invalid"
        if m is not None and not (_is_int(m) and 1 <= m <= 12):
            return f"month {m!r} is invalid"
        if fmt != "gpkg" and c not in CATEGORIES:
            return f"category {c!r} is invalid"
    elif (y, m, c) != (None, None, None):
        return "GeoParquet entry must have null year, month and category"
    if fmt == "gpkg":
        layers = e.get("layers")
        if c is not None or not isinstance(layers, dict) or set(layers) != set(CATEGORIES) or not all(
            _is_int(n) and n >= 0 for n in layers.values()
        ):
            return "GeoPackage entry needs a null category and layers for every category"
        if sum(layers.values()) != e["rows"]:
            return "GeoPackage layers do not add up to rows"
    elif "layers" in e:
        return "only a GeoPackage entry has layers"
    if e.get("path") != _expected_path(e):
        return f"path {e.get('path')!r} does not match its format, year, month and category"
    return None


# --- per-format structure checks ---

def _parquet_columns(report, path, label):
    """Columns, Arrow types and nullability of the GeoParquet. False when the file cannot be read."""
    try:
        schema = pq.read_schema(path)
    except Exception as exc:  # noqa: BLE001 - any read error is a verification failure
        report.fail("columns", label, f"unreadable GeoParquet: {type(exc).__name__}: {exc}")
        return False
    actual = [(f.name, str(f.type), f.nullable) for f in schema]
    wanted = [(n, str(t), null) for n, t, null in COLUMNS]
    if actual != wanted:
        report.fail("columns", label, f"columns, types or nullability differ from the contract: {actual}")
    return True


def _parquet_geo(report, path, label, bbox):
    """GeoParquet 1.1.0 metadata: Point, WKB, CRS84 and the bbox of the data."""
    try:
        geo = json.loads((pq.read_schema(path).metadata or {})[b"geo"])
        column = geo["columns"]["geometry"]
        problems = []
        if geo.get("version") != "1.1.0" or geo.get("primary_column") != "geometry":
            problems.append(f"version/primary_column {geo.get('version')!r}/{geo.get('primary_column')!r}")
        if column.get("encoding") != "WKB" or column.get("geometry_types") != ["Point"]:
            problems.append(f"encoding/geometry_types {column.get('encoding')!r}/{column.get('geometry_types')!r}")
        if (column.get("crs") or {}).get("id") != {"authority": "OGC", "code": "CRS84"}:
            problems.append("crs is not OGC:CRS84")
        if bbox is not None and column.get("bbox") != list(bbox):
            problems.append(f"bbox {column.get('bbox')} differs from the data {list(bbox)}")
        for problem in problems:
            report.fail("metadata", label, f"geo metadata: {problem}")
    except (KeyError, TypeError, ValueError, OSError) as exc:
        report.fail("metadata", label, f"geo metadata is missing or invalid: {type(exc).__name__}: {exc}")


def _check_gpkg_structure(report, path, label):
    try:
        db = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        report.fail("columns", label, f"unreadable GeoPackage: {exc}")
        return
    try:
        if db.execute("PRAGMA application_id").fetchone()[0] != 0x47504B47:
            report.fail("metadata", label, "application_id is not GPKG")
        if db.execute("PRAGMA user_version").fetchone()[0] != 10400:
            report.fail("metadata", label, "user_version is not 10400")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            report.fail("metadata", label, "SQLite quick_check failed")
        srs = db.execute("SELECT organization, organization_coordsys_id FROM gpkg_spatial_ref_sys WHERE srs_id=?", (SRS_ID,)).fetchall()
        if srs != [("EPSG", SRS_ID)]:
            report.fail("metadata", label, "gpkg_spatial_ref_sys has no EPSG:4326 row with srs_id 4326")
        contents = {r[0]: r[1:] for r in db.execute("SELECT table_name, data_type, srs_id FROM gpkg_contents")}
        if contents != {c: ("features", SRS_ID) for c in CATEGORIES}:
            report.fail("metadata", label, f"gpkg_contents is {contents}, expected one features layer per category in 4326")
        geometry = {tuple(r) for r in db.execute("SELECT table_name, column_name, geometry_type_name, srs_id, z, m FROM gpkg_geometry_columns")}
        if geometry != {(c, GEOMETRY_COLUMN, "POINT", SRS_ID, 0, 0) for c in CATEGORIES}:
            report.fail("metadata", label, "gpkg_geometry_columns is not POINT/4326/no Z/no M for every category")
        for category in CATEGORIES:
            info = db.execute(f'PRAGMA table_info("{category}")').fetchall()
            if [(r[1], r[2]) for r in info] != GPKG_TABLES:
                report.fail("columns", f"{label}[{category}]", f"columns or declared types differ from the contract: {[(r[1], r[2]) for r in info]}")
                continue
            bad = db.execute(
                f'SELECT count(*) FROM "{category}" WHERE {GEOMETRY_COLUMN} IS NULL '
                f"OR typeof({GEOMETRY_COLUMN}) != 'blob' OR length({GEOMETRY_COLUMN}) != 29 "
                f"OR hex(substr({GEOMETRY_COLUMN}, 1, 8)) != ? OR hex(substr({GEOMETRY_COLUMN}, 9, 5)) != ?",
                (GPKG_BLOB_HEAD.hex().upper(), WKB_POINT.hex().upper()),
            ).fetchone()[0]
            if bad:
                report.fail("metadata", f"{label}[{category}]", f"{bad} geometry blobs lack the GP header with srs 4326 and a WKB point")
    except sqlite3.Error as exc:
        report.fail("columns", label, f"GeoPackage structure unreadable: {exc}")
    finally:
        db.close()


# --- GeoParquet checks in DuckDB ---

def _parquet_sql_checks(report, con, path):
    def count(sql):
        return con.execute(sql).fetchone()[0]

    def examples(sql):
        return ", ".join(str(r[0]) if len(r) == 1 else "#".join(str(x) for x in r) for r in con.execute(sql).fetchall())

    n = count("SELECT count(*) FROM (SELECT 1 FROM p GROUP BY transaction_id, door_index HAVING count(*) > 1)")
    if n:
        sample = examples(
            "SELECT transaction_id, door_index FROM p GROUP BY transaction_id, door_index HAVING count(*) > 1 LIMIT 3"
        )
        report.fail("keys", path, f"{n} duplicate (transaction_id, door_index) keys, e.g. {sample}")
    where = "door_index IS NULL OR door_count IS NULL OR door_index < 1 OR door_index > door_count"
    n = count(f"SELECT count(*) FROM p WHERE {where}")
    if n:
        sample = examples(f"SELECT transaction_id, door_index FROM p WHERE {where} LIMIT 3")
        report.fail("keys", path, f"{n} rows with door_index outside 1..door_count, e.g. {sample}")
    where = r"transaction_id IS NULL OR NOT regexp_full_match(transaction_id, '[0-9]{3}q[1-4]/[^/]+/[1-9][0-9]*')"
    n = count(f"SELECT count(*) FROM p WHERE {where}")
    if n:
        sample = examples(f"SELECT transaction_id FROM p WHERE {where} LIMIT 3")
        report.fail("keys", path, f"{n} transaction_id values do not match the contract format, e.g. {sample}")
    hashed = ", ".join(f'"{n}"' for n in NONDOOR_NAMES if n != "transaction_id")
    group = f"SELECT transaction_id, min(hash({hashed})) a, max(hash({hashed})) b FROM p GROUP BY transaction_id"
    n = count(f"SELECT count(*) FROM ({group}) WHERE a != b")
    if n:
        sample = examples(f"SELECT transaction_id FROM ({group}) WHERE a != b LIMIT 3")
        report.fail("transaction", path, f"{n} transactions differ between their rows in non-door columns, e.g. {sample}")


def _quality(con):
    quality = {c: {"rows": 0, **{f"null_{n}": 0 for n in QUALITY_COLUMNS}} for c in CATEGORIES}
    filters = ", ".join(f"count(*) FILTER (WHERE {n} IS NULL)" for n in QUALITY_COLUMNS)
    for row in con.execute(f"SELECT category, count(*), {filters} FROM p GROUP BY category").fetchall():
        if row[0] in quality:
            quality[row[0]] = {"rows": row[1], **{f"null_{n}": v for n, v in zip(QUALITY_COLUMNS, row[2:])}}
    return quality


def _parquet_years(con):
    years = set()
    for (text,) in con.execute("SELECT DISTINCT substr(trade_date, 1, 4) FROM p WHERE trade_date IS NOT NULL").fetchall():
        if isinstance(text, str) and text.isdigit() and 1 <= int(text) <= 9998:
            years.add(int(text))
    return years


# --- main ---

def verify_output(public_root, release_root) -> dict:
    """Check the written output. Return the report, or raise OutputVerificationError(report)."""
    public_root, release_root = Path(public_root), Path(release_root)
    csv.field_size_limit(2**30)
    report = _Report()
    summary = {"quality": {}, "bytes_by_format": dict.fromkeys(FORMATS, 0), "warnings": report.warnings}

    def finish():
        result = {
            "passed": not report.failures,
            "checks": {c: ("fail" if report.counts[c] else "pass") for c in CHECKS},
            "failures": report.failures,
            "suppressed": {c: n - MAX_FAILURES_PER_CHECK for c, n in report.counts.items() if n > MAX_FAILURES_PER_CHECK},
            "warnings": report.warnings,
            "quality": summary["quality"],
            "bytes_by_format": summary["bytes_by_format"],
            "file_count": summary.get("file_count", 0),
            "total_rows": summary.get("total_rows"),
            "catalog_sha256": summary.get("catalog_sha256"),
        }
        if report.failures:
            raise OutputVerificationError(result)
        return result

    catalog_path = public_root / CATALOG_NAME
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        summary["catalog_sha256"] = sha256_file(catalog_path)
        if not isinstance(catalog, dict) or not isinstance(catalog.get("files"), list):
            raise ValueError("no files array")
    except (OSError, ValueError) as exc:
        report.fail("catalog", CATALOG_NAME, f"catalog.json is missing or invalid: {type(exc).__name__}")
        return finish()

    # a. catalog constants, entries, files, hashes and extra files
    try:
        cutoff_ok = cutoff_text(catalog.get("cutoff_yyyymm")) == catalog.get("cutoff_yyyymm")
    except ValueError:
        cutoff_ok = False
    sources = catalog.get("source_zip_sha256")
    for ok, detail in [
        (catalog.get("currency") == CURRENCY, f"currency is {catalog.get('currency')!r}, expected {CURRENCY!r}"),
        (catalog.get("contract_version") == CONTRACT_VERSION, f"contract_version is {catalog.get('contract_version')!r}"),
        (catalog.get("door_rule_version") == NORMALIZATION_VERSION, f"door_rule_version is {catalog.get('door_rule_version')!r}"),
        (catalog.get("admin_towns_sha256") == admin_towns_sha256(), f"admin_towns_sha256 is {catalog.get('admin_towns_sha256')!r}"),
        (isinstance(catalog.get("char_fixes_sha256"), str) and bool(SHA256.fullmatch(catalog["char_fixes_sha256"])),
         f"char_fixes_sha256 is {catalog.get('char_fixes_sha256')!r}, expected a SHA-256"),
        (cutoff_ok, f"cutoff_yyyymm is {catalog.get('cutoff_yyyymm')!r}, expected a YYYYMM string"),
        (
            isinstance(sources, dict) and bool(sources) and all(
                re.fullmatch(r"[0-9]{3}q[1-4]", b) and isinstance(h, str) and SHA256.fullmatch(h) for b, h in sources.items()
            ),
            "source_zip_sha256 must map every batch to a SHA-256",
        ),
    ]:
        if not ok:
            report.fail("catalog", catalog_path, detail)

    entries, seen_paths = [], set()
    for position, entry in enumerate(catalog["files"]):
        problem = _entry_problem(entry)
        if problem is None and entry["path"] in seen_paths:
            problem = "path is listed twice"
        if problem:
            report.fail("catalog", f"files[{position}]", problem)
            continue
        seen_paths.add(entry["path"])
        entries.append(entry)
    entries.sort(key=lambda e: e["path"])
    if catalog["files"] != sorted(catalog["files"], key=lambda e: e.get("path", "") if isinstance(e, dict) else ""):
        report.fail("catalog", catalog_path, "files are not sorted by path")

    def location(entry):
        return (release_root if entry["location"] == "release" else public_root) / entry["path"]

    present = {}
    for entry in entries:
        path = location(entry)
        if not path.is_file():
            report.fail("catalog", entry["path"], "listed file does not exist")
            continue
        present[entry["path"]] = path
        size = path.stat().st_size
        summary["bytes_by_format"][entry["format"]] += size
        if size != entry["size_bytes"]:
            report.fail("catalog", entry["path"], f"size is {size}, catalog says {entry['size_bytes']}")
        digest = sha256_file(path)
        if digest != entry["sha256"]:
            report.fail("catalog", entry["path"], f"sha256 is {digest}, catalog says {entry['sha256']}")
        if entry["location"] == "public":
            if size >= FILE_LIMIT_BYTES:
                report.fail("resources", entry["path"], f"{size} bytes reaches the {FILE_LIMIT_BYTES} byte limit of a public file")
            elif size >= WARN_BYTES:
                report.warnings.append({"path": entry["path"], "size_bytes": size, "detail": f"at least {WARN_BYTES} bytes"})
    listed = {e["path"] for e in entries if e["location"] == "public"} | {CATALOG_NAME}
    if public_root.is_dir():
        for file in sorted(public_root.rglob("*")):
            if file.is_file() and file.relative_to(public_root).as_posix() not in listed:
                report.fail("catalog", file.relative_to(public_root).as_posix(), "file under the public directory is not listed in the catalog")
    else:
        report.fail("catalog", public_root, "public directory does not exist")
    release_files = sorted(f.relative_to(release_root).as_posix() for f in release_root.rglob("*") if f.is_file()) if release_root.is_dir() else []
    if release_files != [PARQUET_NAME]:
        report.fail("catalog", release_root, f"release directory must hold exactly {PARQUET_NAME}, found {release_files}")
    parquet_entries = [e for e in entries if e["format"] == "geoparquet"]
    if len(parquet_entries) != 1:
        report.fail("catalog", catalog_path, f"expected exactly one GeoParquet entry, found {len(parquet_entries)}")
    summary["file_count"] = len(entries)
    summary["total_rows"] = catalog.get("total_rows")
    if parquet_entries and catalog.get("total_rows") != parquet_entries[0]["rows"]:
        report.fail("catalog", catalog_path, f"total_rows {catalog.get('total_rows')!r} differs from the GeoParquet rows {parquet_entries[0]['rows']}")

    # structure of the GeoPackages
    for entry in entries:
        path = present.get(entry["path"])
        if path is not None and entry["format"] == "gpkg":
            _check_gpkg_structure(report, path, entry["path"])

    # GeoParquet: set-based checks in DuckDB, then the reference rows year by year
    parquet_path = present.get(PARQUET_NAME)
    con, years, bbox, tracked = None, set(), None, 0
    with tempfile.TemporaryDirectory(prefix="verify-") as spill:
        try:
            if parquet_path is not None and _parquet_columns(report, parquet_path, PARQUET_NAME):
                con = duckdb.connect(config=duckdb_config(spill))
                con.read_parquet(str(parquet_path)).create_view("p")
                rows = pq.ParquetFile(parquet_path).metadata.num_rows
                if rows != parquet_entries[0]["rows"]:
                    report.fail("catalog", PARQUET_NAME, f"file holds {rows} rows, catalog says {parquet_entries[0]['rows']}")
                _parquet_sql_checks(report, con, PARQUET_NAME)
                summary["quality"] = _quality(con)
                years = _parquet_years(con)
        except duckdb.Error as exc:
            report.fail("columns", PARQUET_NAME, f"GeoParquet cannot be queried: {exc}")
            if con is not None:
                con.close()
            con = None
        try:
            for year in sorted(years | {e["year"] for e in entries if e["year"] is not None}):
                expected = {}
                if con is not None:
                    expected = {category: _Acc() for category in CATEGORIES}
                    for v in _read_parquet(con, year):
                        acc = expected.get(v[INDEX["category"]])
                        if acc is None:
                            report.fail("cross-format", PARQUET_NAME, f"row {v[0]}#{v[1]} has category {v[INDEX['category']]!r}")
                            continue
                        acc.digests.add(_digest(repr(v)))
                        acc.count += 1
                        x, y = v[-2], v[-1]
                        if isinstance(x, float) and isinstance(y, float):
                            bbox = (x, y, x, y) if bbox is None else (min(bbox[0], x), min(bbox[1], y), max(bbox[2], x), max(bbox[3], y))
                    tracked += sum(a.count for a in expected.values())
                for category in CATEGORIES:
                    _compare_group(report, entries, present, con, year, category, expected.get(category))
            if con is not None and tracked != parquet_entries[0]["rows"]:
                report.fail(
                    "cross-format", PARQUET_NAME,
                    f"{parquet_entries[0]['rows'] - tracked} GeoParquet rows have no valid trade_date year or category",
                )
        except duckdb.Error as exc:
            report.fail("columns", PARQUET_NAME, f"GeoParquet cannot be queried: {exc}")
        finally:
            if con is not None:
                con.close()
    if parquet_path is not None:
        _parquet_geo(report, parquet_path, PARQUET_NAME, bbox)

    return finish()


def _compare_group(report, entries, present, con, year, category, expected):
    """Scan every file of one year and category and compare each format with the GeoParquet rows."""
    groups = {"GeoPackage layer": [], "yearly CSV": [], "monthly CSV": [], "monthly GeoJSON": []}
    for e in entries:
        if e["year"] != year or e["path"] not in present or e["format"] == "geoparquet":
            continue
        path = present[e["path"]]
        if e["format"] == "gpkg":
            groups["GeoPackage layer"].append((e, _read_gpkg(path, category)))
        elif e["category"] == category:
            if e["format"] == "geojson":
                groups["monthly GeoJSON"].append((e, _read_geojson(path)))
            else:
                groups["yearly CSV" if e["month"] is None else "monthly CSV"].append((e, _read_csv(path)))
    label = f"{year:04d} {category}"
    for name, items in groups.items():
        if not items:
            if expected is not None and expected.count:
                report.fail("cross-format", label, f"{name} has no file although GeoParquet holds {expected.count} rows")
            continue
        acc = _Acc()
        for entry, reader in items:
            gpkg = entry["format"] == "gpkg"
            counted = _scan(
                report, entry["path"] + (f"[{category}]" if gpkg else ""), reader, acc,
                year=year, month=entry["month"], category=None if gpkg else category,
            )
            if counted is None:
                continue
            want = entry["layers"][category] if gpkg else entry["rows"]
            if counted != want:
                where = f" in layer {category}" if gpkg else ""
                report.fail("catalog", entry["path"], f"holds {counted} rows{where}, catalog says {want}")
        if acc.unreadable or expected is None:
            continue
        if acc.count != expected.count or acc.digests != expected.digests:
            reference = [lambda note: _only(_read_parquet(con, year), category)]
            report.fail(
                "cross-format", label,
                f"{name} has {acc.count} rows, GeoParquet {expected.count}: "
                + _explain(reference, [reader for _, reader in items]),
            )


def _only(rows, category):
    index = INDEX["category"]
    for v in rows:
        if v[index] == category:
            yield v
