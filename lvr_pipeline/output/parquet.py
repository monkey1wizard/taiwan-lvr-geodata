"""Single GeoParquet file `taiwanlvr.parquet` (GeoParquet 1.1.0, WKB Point, CRS84)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
import struct

import pyarrow as pa
import pyarrow.parquet as pq

from .rows import ARROW_SCHEMA, COLUMN_NAMES

FLUSH_ROWS = 4096


def point_wkb(lng, lat):
    return struct.pack("<BIdd", 1, 1, lng, lat)


def geo_metadata(bbox=None):
    from pyproj import CRS

    column = {
        "encoding": "WKB",
        "geometry_types": ["Point"],
        "crs": CRS.from_user_input("OGC:CRS84").to_json_dict(),
        "edges": "planar",
    }
    if bbox is not None:
        column["bbox"] = list(bbox)
    return {"version": "1.1.0", "primary_column": "geometry", "columns": {"geometry": column}}


def _geo_bytes(bbox=None):
    return json.dumps(geo_metadata(bbox), ensure_ascii=False, separators=(",", ":")).encode()


class ParquetOutput:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raise FileExistsError(self.path)
        self.schema = ARROW_SCHEMA.with_metadata({b"geo": _geo_bytes()})
        self.writer = pq.ParquetWriter(self.path, self.schema, compression="zstd")
        self.buffer = []
        self.rows = 0
        self.bbox = None

    def add(self, row):
        lng, lat = row["longitude"], row["latitude"]
        value = {name: row.get(name) for name in COLUMN_NAMES}
        value["geometry"] = point_wkb(lng, lat)
        self.buffer.append(value)
        b = self.bbox
        self.bbox = (lng, lat, lng, lat) if b is None else (min(b[0], lng), min(b[1], lat), max(b[2], lng), max(b[3], lat))
        self.rows += 1
        if len(self.buffer) >= FLUSH_ROWS:
            self.flush()

    def flush(self):
        if self.buffer:
            self.writer.write_table(pa.Table.from_pylist(self.buffer, schema=self.schema))
            self.buffer = []

    def close(self):
        self.flush()
        final = self.schema.with_metadata({b"geo": _geo_bytes(self.bbox)})
        self.writer.add_key_value_metadata(
            {b"geo": _geo_bytes(self.bbox), b"ARROW:schema": base64.b64encode(final.serialize().to_pybytes())}
        )
        self.writer.close()
