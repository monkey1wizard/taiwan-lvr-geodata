"""One source observation and one geometry decision in all consumer formats."""

from __future__ import annotations

import json
import base64
from pathlib import Path
import struct

import pyarrow as pa
import pyarrow.parquet as pq

from .storage.parquet import SCHEMAS
from .storage.runs import canonical_json


def geometry(points, category):
    points = sorted(set(points))
    if not points:
        return None, False
    if len(points) == 1:
        return {"type": "Point", "coordinates": list(points[0])}, False
    xs, ys = zip(*points)
    if category != "rent" and min(xs) < max(xs) and min(ys) < max(ys):
        a, b, c, d = min(xs), min(ys), max(xs), max(ys)
        return {
            "type": "Polygon",
            "coordinates": [[[a, b], [c, b], [c, d], [a, d], [a, b]]],
        }, True
    return {"type": "MultiPoint", "coordinates": [list(p) for p in points]}, False


def wkb(value):
    if value is None:
        return None
    if value["type"] == "Point":
        return struct.pack("<BIdd", 1, 1, *value["coordinates"])
    if value["type"] == "MultiPoint":
        return struct.pack("<BII", 1, 4, len(value["coordinates"])) + b"".join(
            wkb({"type": "Point", "coordinates": p}) for p in value["coordinates"]
        )
    ring = value["coordinates"][0]
    return struct.pack("<BIII", 1, 3, 1, len(ring)) + b"".join(
        struct.pack("<dd", *p) for p in ring
    )


def read_wkb(data):
    if data is None:
        return None
    if len(data) < 5 or data[0] != 1:
        raise ValueError("Unsupported output WKB")
    kind = struct.unpack_from("<I", data, 1)[0]
    if kind == 1 and len(data) == 21:
        return {
            "type": "Point",
            "coordinates": list(struct.unpack_from("<dd", data, 5)),
        }
    if kind == 4:
        n = struct.unpack_from("<I", data, 5)[0]
        if len(data) != 9 + n * 21:
            raise ValueError("Invalid MultiPoint WKB")
        return {
            "type": "MultiPoint",
            "coordinates": [
                read_wkb(data[9 + i * 21 : 30 + i * 21])["coordinates"]
                for i in range(n)
            ],
        }
    if kind == 3:
        rings, n = struct.unpack_from("<II", data, 5)
        if rings != 1 or len(data) != 13 + n * 16:
            raise ValueError("Invalid Polygon WKB")
        return {
            "type": "Polygon",
            "coordinates": [
                [list(struct.unpack_from("<dd", data, 13 + i * 16)) for i in range(n)]
            ],
        }
    raise ValueError("Invalid output WKB geometry")


def output_schema(geometry_types=None):
    from pyproj import CRS

    fields = list(SCHEMAS["observation"]) + [
        pa.field("location_status", pa.string(), nullable=False),
        pa.field("is_approximation", pa.bool_(), nullable=False),
        pa.field("address_component_count", pa.int64(), nullable=False),
        pa.field("located_component_count", pa.int64(), nullable=False),
        pa.field("unique_point_count", pa.int64(), nullable=False),
        pa.field("geometry", pa.binary(), nullable=True),
    ]
    geo = {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {
            "geometry": {
                "encoding": "WKB",
                "geometry_types": geometry_types or [],
                "crs": CRS.from_user_input("OGC:CRS84").to_json_dict(),
                "edges": "planar",
            }
        },
    }
    return pa.schema(
        fields,
        metadata={
            b"geo": canonical_json(geo).encode(),
            b"lvr.record_grain": b"source_observation",
            b"lvr.schema_version": b"1.0",
        },
    )


def feature(row):
    properties = {k: v for k, v in row.items() if k != "geometry"}
    return {
        "type": "Feature",
        "id": row["raw_record_id"],
        "geometry": read_wkb(row["geometry"]),
        "properties": properties,
    }


class MonthWriter:
    def __init__(self, directory: Path, prefix: str):
        directory.mkdir(parents=True, exist_ok=True)
        self.paths = {
            fmt: directory / (prefix + "." + extension)
            for fmt, extension in [
                ("geoparquet", "parquet"),
                ("geojson", "geojson"),
                ("ndjson", "ndjson"),
            ]
        }
        self.schema = output_schema()
        self.parquet = pq.ParquetWriter(
            self.paths["geoparquet"], self.schema, compression="zstd"
        )
        self.geojson = self.paths["geojson"].open("w", encoding="utf-8", newline="\n")
        self.ndjson = self.paths["ndjson"].open("w", encoding="utf-8", newline="\n")
        self.geojson.write('{"type":"FeatureCollection","features":[\n')
        self.buffer = []
        self.geometry_types = set()
        self.buffer_bytes = 0
        self.count = 0
        self.stats = {
            "rows": 0,
            "null_geometry": 0,
            "partial_geometry": 0,
            "approximation": 0,
            "amount_minor_sum": 0,
        }

    def add(self, row, points, component_count, located_count):
        shape, approximation = geometry(points, row["category"])
        if shape:
            self.geometry_types.add(shape["type"])
        value = {
            **row,
            "location_status": "none"
            if not located_count
            else "complete"
            if located_count == component_count
            else "partial",
            "is_approximation": approximation,
            "address_component_count": component_count,
            "located_component_count": located_count,
            "unique_point_count": len(set(points)),
            "geometry": wkb(shape),
        }
        line = canonical_json(feature(value))
        if len(line.encode()) > 8 * 2**20:
            raise ValueError("Output row exceeds 8MiB byte budget")
        if self.count:
            self.geojson.write(",\n")
        self.geojson.write(line)
        self.ndjson.write(line + "\n")
        if self.buffer_bytes + len(line.encode()) > 8 * 2**20:
            self.flush()
        self.buffer.append(value)
        self.buffer_bytes += len(line.encode())
        if len(self.buffer) >= 1024:
            self.flush()
        self.count += 1
        self.stats["rows"] += 1
        self.stats["null_geometry"] += shape is None
        self.stats["partial_geometry"] += value["location_status"] == "partial"
        self.stats["approximation"] += approximation
        self.stats["amount_minor_sum"] += row["amount_minor"] or 0

    def flush(self):
        if self.buffer:
            self.parquet.write_table(
                pa.Table.from_pylist(self.buffer, schema=self.schema)
            )
            self.buffer = []
            self.buffer_bytes = 0

    def close(self):
        self.flush()
        final_schema = output_schema(sorted(self.geometry_types))
        self.parquet.add_key_value_metadata(
            {
                **final_schema.metadata,
                b"ARROW:schema": base64.b64encode(
                    final_schema.serialize().to_pybytes()
                ),
            }
        )
        self.parquet.close()
        self.geojson.write("\n]}\n")
        self.geojson.close()
        if not self.count:
            self.ndjson.write("\n")
        self.ndjson.close()


def export_month(db, month, category, directory):
    writer = MonthWriter(directory, f"{month}_{category}")
    query = """SELECT o.*,a.component_id,r.lng,r.lat FROM observations o
        JOIN occurrences a USING(raw_record_id) LEFT JOIN results r USING(key_version,building_key)
        WHERE o.tx_yyyymm=? AND o.category=? ORDER BY o.raw_record_id,a.component_id"""
    cursor = db.execute(query, [month, category])
    names = [x[0] for x in cursor.description]
    current = None
    record = None
    points = []
    total = located = 0
    try:
        while batch := cursor.fetchmany(1024):
            for values in batch:
                value = dict(zip(names, values))
                identifier = value["raw_record_id"]
                if identifier != current:
                    if record is not None:
                        writer.add(record, points, total, located)
                    current = identifier
                    points = []
                    total = located = 0
                    record = {k: value[k] for k in SCHEMAS["observation"].names}
                total += 1
                if total > 10000:
                    raise ValueError(
                        "Observation exceeds 10000 component resource budget"
                    )
                if value["lng"] is not None:
                    points.append((value["lng"], value["lat"]))
                    located += 1
        if record is not None:
            writer.add(record, points, total, located)
    finally:
        writer.close()
    return writer.paths, writer.stats


def verify_month(paths):
    parquet = pq.ParquetFile(paths["geoparquet"])
    declared_types = json.loads(parquet.schema_arrow.metadata[b"geo"])["columns"][
        "geometry"
    ]["geometry_types"]
    if not parquet.schema_arrow.equals(
        output_schema(declared_types), check_metadata=True
    ):
        raise ValueError("GeoParquet schema/CRS metadata mismatch")
    observed_types = set()
    stats = {
        "rows": 0,
        "null_geometry": 0,
        "partial_geometry": 0,
        "approximation": 0,
        "amount_minor_sum": 0,
    }
    with (
        Path(paths["ndjson"]).open(encoding="utf-8") as nd,
        Path(paths["geojson"]).open(encoding="utf-8") as geo,
    ):
        if geo.readline() != '{"type":"FeatureCollection","features":[\n':
            raise ValueError("GeoJSON header mismatch")
        for group in range(parquet.num_row_groups):
            for batch in parquet.iter_batches(batch_size=1024, row_groups=[group]):
                for row in batch.to_pylist():
                    expected = feature(row)
                    if (
                        json.loads(nd.readline()) != expected
                        or json.loads(geo.readline().rstrip("\n,")) != expected
                    ):
                        raise ValueError("Output format parity mismatch")
                    shape = expected["geometry"]
                    if shape:
                        observed_types.add(shape["type"])
                    n = row["address_component_count"]
                    located = row["located_component_count"]
                    if not 0 <= located <= n or n < 1:
                        raise ValueError("Output component counts invalid")
                    status = (
                        "none"
                        if not located
                        else "complete"
                        if located == n
                        else "partial"
                    )
                    if row["location_status"] != status or (shape is None) != (
                        located == 0
                    ):
                        raise ValueError("Output location status invalid")
                    if shape and shape["type"] == "Polygon":
                        ring = shape["coordinates"][0]
                        if (
                            row["category"] == "rent"
                            or not row["is_approximation"]
                            or ring[0] != ring[-1]
                            or ring[0][0] >= ring[1][0]
                            or ring[1][1] >= ring[2][1]
                        ):
                            raise ValueError("Invalid approximate bbox")
                    elif row["is_approximation"]:
                        raise ValueError("Only Polygon may be approximate")
                    stats["rows"] += 1
                    stats["null_geometry"] += shape is None
                    stats["partial_geometry"] += status == "partial"
                    stats["approximation"] += row["is_approximation"]
                    stats["amount_minor_sum"] += row["amount_minor"] or 0
        if nd.read().strip():
            raise ValueError("NDJSON has extra records")
        if geo.read() != ("\n]}\n" if not stats["rows"] else "]}\n"):
            raise ValueError("GeoJSON footer/row count mismatch")
    if sorted(observed_types) != declared_types:
        raise ValueError("GeoParquet geometry type metadata differs from actual rows")
    return stats
