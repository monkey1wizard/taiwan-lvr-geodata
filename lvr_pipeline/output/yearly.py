"""Yearly point CSVs and the display spread of shared coordinates (contract 1.2)."""

from __future__ import annotations

import csv
import math

import pyarrow.parquet as pq

from ..source_ref import make_source_ref
from .monthly import artifact


_POINT_LEADING = [
    "trade_date", "county", "district", "address", "building_type", "total_price",
    "unit_price_sqm", "building_area_sqm", "location_status", "longitude", "latitude",
]
POINT_COLUMNS_V10 = _POINT_LEADING + ["raw_record_id"]  # contract 1.0, still verified
POINT_COLUMNS = _POINT_LEADING + ["source_ref"]  # contracts 1.1 and 1.2
POINT_SOURCE_COLUMNS = _POINT_LEADING + ["src_batch", "member_path", "source_row_number"]


def point_values(row, legacy=False):
    """CSV cells of one fully located single-Point observation, else None."""
    if row["location_status"] != "complete" or row["longitude"] is None:
        return None
    cells = ["" if row[k] is None else str(row[k]) for k in _POINT_LEADING]
    if legacy:
        return cells + [row["raw_record_id"]]
    return cells + [make_source_ref(row["src_batch"], row["member_path"], row["source_row_number"])]


def iter_points(path, legacy=False):
    """Yield point_values for every fully located single-Point row of one monthly file."""
    for values, _ in iter_points_with_id(path, legacy):
        yield values


def iter_points_with_id(path, legacy=False):
    """Yield (point_values, raw_record_id) for every fully located single-Point row."""
    parquet = pq.ParquetFile(path)
    columns = _POINT_LEADING + ["raw_record_id", "src_batch", "member_path", "source_row_number"]
    for batch in parquet.iter_batches(batch_size=4096, columns=columns):
        for row in batch.to_pylist():
            values = point_values(row, legacy)
            if values is not None:
                yield values, row["raw_record_id"]


# Contract 1.2: display spread of yearly points that share one building coordinate.
EARTH_RADIUS_M = 6371008.8  # the radius haversine_m uses
SPREAD_RADIUS_M = 0.9  # outer radius of the spiral; the 1 m limit keeps 0.1 m of margin
SPREAD_LIMIT_M = 1.0
SPREAD_DECIMALS = 8  # 1e-8 degree is about 1.1 mm, far finer than the 1 m spread
GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))
_LONGITUDE, _LATITUDE = _POINT_LEADING.index("longitude"), _POINT_LEADING.index("latitude")


def spread_rank(rank, count):
    """Cell texts (longitude offset, latitude offset in metres) of the rank-th of count points.

    Fermat (sunflower) spiral: radius = SPREAD_RADIUS_M * sqrt((rank + 0.5) / count), angle = rank * golden
    angle. Each point owns an equal area, so spacing shrinks as count grows and the radius never exceeds
    SPREAD_RADIUS_M. Even the first point is moved off the building coordinate.
    """
    radius = SPREAD_RADIUS_M * math.sqrt((rank + 0.5) / count)
    angle = rank * GOLDEN_ANGLE
    return radius * math.cos(angle), radius * math.sin(angle)


def spread_coordinate(lon, lat, rank, count):
    """(longitude, latitude) cell texts of one spread point; count == 1 returns the original unchanged."""
    if count <= 1:
        return repr(lon), repr(lat)
    east, north = spread_rank(rank, count)
    new_lat = lat + math.degrees(north / EARTH_RADIUS_M)
    new_lon = lon + math.degrees(east / (EARTH_RADIUS_M * math.cos(math.radians(lat))))
    return f"{new_lon:.{SPREAD_DECIMALS}f}", f"{new_lat:.{SPREAD_DECIMALS}f}"


def spread_ranks(entries):
    """{source_ref: (rank, count)} for groups of more than one point.

    entries: iterable of (group_key, trade_date, source_ref). Within a group the order is (trade_date,
    source_ref) with a missing trade_date first; no randomness, so reruns give the same ranks.
    """
    groups = {}
    for key, trade_date, ref in entries:
        groups.setdefault(key, []).append((trade_date or "", ref))
    ranks = {}
    for items in groups.values():
        if len(items) > 1:
            items.sort()
            for rank, (_, ref) in enumerate(items):
                ranks[ref] = (rank, len(items))
    return ranks


def iter_spread_points(paths):
    """Yield (point_values, raw_record_id, (lon, lat)) of one yearly category with contract 1.2 coordinates.

    point_values carry the spread coordinates; (lon, lat) are the original monthly coordinates. Two passes
    over the monthly files: the first keeps only (coordinate, trade_date, source_ref) per row, the second
    streams the rows, so a year of one category (about 220,000 rows) never sits in memory as full rows.
    """
    paths = list(paths)
    ranks = spread_ranks(
        ((v[_LONGITUDE], v[_LATITUDE]), v[0], v[-1]) for path in paths for v, _ in iter_points_with_id(path)
    )
    for path in paths:
        for values, raw_id in iter_points_with_id(path):
            lon, lat = float(values[_LONGITUDE]), float(values[_LATITUDE])
            rank, count = ranks.get(values[-1], (0, 1))
            values[_LONGITUDE], values[_LATITUDE] = spread_coordinate(lon, lat, rank, count)
            yield values, raw_id, (lon, lat)


GIS_CONTRACT = "1.2"


class PointsWriter:
    """One UTF-8 (no BOM) point CSV per year and category, written month by month."""

    def __init__(self, staging):
        self.staging = staging
        self.files = {}
        self.sources = {}

    def add(self, year, category, monthly_parquet):
        self.sources.setdefault((year, category), []).append(monthly_parquet)

    def close(self):
        """Write each yearly file once all its monthly files are known (the spread needs the whole group)."""
        for (year, category), months in sorted(self.sources.items()):
            path = self.staging / "yearly" / str(year) / f"{year}_{category}_points.csv"
            rows = 0
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream, lineterminator="\n")
                writer.writerow(POINT_COLUMNS)
                for values, _, _ in iter_spread_points(months):
                    writer.writerow(values)
                    rows += 1
            if rows:
                self.files[(year, category)] = [path, None, None, rows]
            else:
                path.unlink()

    def assets(self, max_asset_bytes):
        out = []
        for (year, category), (path, _, _, rows) in sorted(self.files.items()):
            item = artifact(
                self.staging,
                path,
                kind="annual_points",
                year=year,
                category=category,
                rows=rows,
                columns=POINT_COLUMNS,
            )
            if item["bytes"] > max_asset_bytes:
                raise ValueError(
                    f"Annual points file exceeds configured asset limit: {item['path']}"
                )
            out.append(item)
        return out
