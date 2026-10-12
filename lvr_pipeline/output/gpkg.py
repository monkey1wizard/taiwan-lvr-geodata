"""One GeoPackage (OGC 1.4) per year with the layers sales, presale and rent, written with sqlite3."""

from __future__ import annotations

import datetime
from pathlib import Path
import sqlite3
import struct
import xml.etree.ElementTree as ET

import pyarrow as pa

from .rows import ATTRIBUTES, CATEGORIES

SRS_ID = 4326
WGS84_WKT = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],'
    'AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
    'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4326"]]'
)
GEOMETRY_COLUMN = "geom"
FLUSH_ROWS = 2000


def _sqlite_type(arrow_type):
    if pa.types.is_integer(arrow_type):
        return "INTEGER"
    if pa.types.is_floating(arrow_type):
        return "REAL"
    return "TEXT"


def geometry_blob(lng, lat):
    """GeoPackage binary: 'GP', version 0, flags 0x01 (little endian, no envelope), srs_id, WKB Point."""
    return b"GP" + struct.pack("<BBi", 0, 0x01, SRS_ID) + struct.pack("<BIdd", 1, 1, lng, lat)


def _symbol(name, shape, color):
    symbol = ET.Element("symbol", {"type": "marker", "name": name, "alpha": "1", "clip_to_extent": "1", "force_rhr": "0"})
    layer = ET.SubElement(symbol, "layer", {"class": "SimpleMarker", "enabled": "1", "locked": "0", "pass": "0"})
    options = ET.SubElement(layer, "Option", {"type": "Map"})
    for key, value in [
        ("name", shape),
        ("color", color),
        ("outline_color", "35,35,35,255"),
        ("outline_width", "0.2"),
        ("outline_style", "solid"),
        ("size", "3"),
        ("size_unit", "MM"),
    ]:
        ET.SubElement(options, "Option", {"name": key, "value": value, "type": "QString"})
    return symbol


def layer_qml():
    """Rule-based marker style: door_count > 1 as a red square, otherwise a blue circle, label only for multi-door."""
    multi = '"door_count" > 1'
    root = ET.Element("qgis", {"version": "3.34", "styleCategories": "Symbology|Labeling"})
    renderer = ET.SubElement(
        root, "renderer-v2", {"type": "RuleRenderer", "symbollevels": "0", "enableorderby": "0", "forceraster": "0"}
    )
    rules = ET.SubElement(renderer, "rules", {"key": "{6b8f3e1a-0000-4000-8000-000000000001}"})
    ET.SubElement(
        rules,
        "rule",
        {"key": "{6b8f3e1a-0000-4000-8000-000000000002}", "filter": multi, "symbol": "0", "label": "door_count > 1"},
    )
    ET.SubElement(
        rules,
        "rule",
        {"key": "{6b8f3e1a-0000-4000-8000-000000000003}", "filter": "ELSE", "symbol": "1", "label": "door_count = 1"},
    )
    symbols = ET.SubElement(renderer, "symbols")
    symbols.append(_symbol("0", "square", "227,26,28,255"))
    symbols.append(_symbol("1", "circle", "31,120,180,255"))
    labeling = ET.SubElement(root, "labeling", {"type": "rule-based"})
    label_rules = ET.SubElement(labeling, "rules", {"key": "{6b8f3e1a-0000-4000-8000-000000000004}"})
    rule = ET.SubElement(
        label_rules, "rule", {"key": "{6b8f3e1a-0000-4000-8000-000000000005}", "filter": multi, "description": "door_index/door_count"}
    )
    settings = ET.SubElement(rule, "settings", {"calloutType": "simple"})
    ET.SubElement(
        settings,
        "text-style",
        {"fieldName": "\"door_index\" || '/' || \"door_count\"", "isExpression": "1", "fontSize": "9", "fontSizeUnit": "Point"},
    )
    ET.SubElement(settings, "placement", {"placement": "1", "dist": "1", "distUnits": "MM"})
    ET.SubElement(settings, "rendering", {"scaleVisibility": "0", "obstacle": "0"})
    body = ET.tostring(root, encoding="unicode")
    return "<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n" + body


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class GpkgOutput:
    """Write one yearly GeoPackage; rows are added with their category and the file is finished by close()."""

    def __init__(self, path: Path, year: int, *, timestamp: str | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raise FileExistsError(self.path)
        self.year = year
        self.timestamp = timestamp or _now()
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA application_id = 0x47504B47")
        self.db.execute("PRAGMA user_version = 10400")
        self.rows = dict.fromkeys(CATEGORIES, 0)
        self.bbox = dict.fromkeys(CATEGORIES)
        self.buffers = {c: [] for c in CATEGORIES}
        names = [n for n, _, _ in ATTRIBUTES]
        self.names = names
        self._create()

    def _create(self):
        db = self.db
        db.execute(
            """CREATE TABLE gpkg_spatial_ref_sys (
            srs_name TEXT NOT NULL, srs_id INTEGER PRIMARY KEY, organization TEXT NOT NULL,
            organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, description TEXT)"""
        )
        db.executemany(
            "INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)",
            [
                ("Undefined Cartesian SRS", -1, "NONE", -1, "undefined", "undefined Cartesian coordinate reference system"),
                ("Undefined geographic SRS", 0, "NONE", 0, "undefined", "undefined geographic coordinate reference system"),
                ("WGS 84 geodetic", SRS_ID, "EPSG", SRS_ID, WGS84_WKT, "longitude/latitude coordinates in decimal degrees on the WGS 84 spheroid"),
            ],
        )
        db.execute(
            """CREATE TABLE gpkg_contents (
            table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL, identifier TEXT UNIQUE, description TEXT DEFAULT '',
            last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER,
            CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id))"""
        )
        db.execute(
            """CREATE TABLE gpkg_geometry_columns (
            table_name TEXT NOT NULL, column_name TEXT NOT NULL, geometry_type_name TEXT NOT NULL,
            srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL,
            CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name),
            CONSTRAINT fk_gc_tn FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name),
            CONSTRAINT fk_gc_srs FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id))"""
        )
        db.execute(
            """CREATE TABLE layer_styles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, f_table_catalog TEXT(256), f_table_schema TEXT(256),
            f_table_name TEXT(256), f_geometry_column TEXT(256), styleName TEXT(30), styleQML TEXT, styleSLD TEXT,
            useAsDefault BOOLEAN, description TEXT, owner TEXT(30), ui TEXT(30),
            update_time DATETIME DEFAULT CURRENT_TIMESTAMP)"""
        )
        qml = layer_qml()
        columns = ", ".join(f'"{n}" {_sqlite_type(t)}' for n, t, _ in ATTRIBUTES)
        for category in CATEGORIES:
            db.execute(
                f'CREATE TABLE "{category}" (fid INTEGER PRIMARY KEY AUTOINCREMENT, {GEOMETRY_COLUMN} POINT, {columns})'
            )
            db.execute(
                "INSERT INTO gpkg_contents (table_name, data_type, identifier, description, last_change, srs_id) VALUES (?,?,?,?,?,?)",
                (category, "features", category, f"taiwanlvr {self.year} {category}", self.timestamp, SRS_ID),
            )
            db.execute(
                "INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,?,?)", (category, GEOMETRY_COLUMN, "POINT", SRS_ID, 0, 0)
            )
            db.execute(
                "INSERT INTO layer_styles (f_table_catalog, f_table_schema, f_table_name, f_geometry_column, styleName, "
                "styleQML, styleSLD, useAsDefault, description, owner, ui, update_time) VALUES ('','',?,?,?,?,NULL,1,?,'','',?)",
                (category, GEOMETRY_COLUMN, category, qml, "door_count > 1 marked, label door_index/door_count", self.timestamp),
            )
        db.commit()

    def add(self, row):
        category = row["category"]
        self.buffers[category].append(
            (geometry_blob(row["longitude"], row["latitude"]),) + tuple(row.get(n) for n in self.names)
        )
        lng, lat = row["longitude"], row["latitude"]
        b = self.bbox[category]
        self.bbox[category] = (lng, lat, lng, lat) if b is None else (min(b[0], lng), min(b[1], lat), max(b[2], lng), max(b[3], lat))
        self.rows[category] += 1
        if len(self.buffers[category]) >= FLUSH_ROWS:
            self._flush(category)

    def _flush(self, category):
        if self.buffers[category]:
            columns = ", ".join([GEOMETRY_COLUMN] + [f'"{n}"' for n in self.names])
            marks = ",".join("?" * (len(self.names) + 1))
            self.db.executemany(f'INSERT INTO "{category}" ({columns}) VALUES ({marks})', self.buffers[category])
            self.buffers[category] = []

    def close(self):
        for category in CATEGORIES:
            self._flush(category)
            b = self.bbox[category]
            if b is not None:
                self.db.execute(
                    "UPDATE gpkg_contents SET min_x=?, min_y=?, max_x=?, max_y=? WHERE table_name=?", (*b, category)
                )
        self.db.commit()
        self.db.close()
