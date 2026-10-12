"""Write all four public formats from the door rows in one ordered pass."""

from __future__ import annotations

from pathlib import Path

from .csv import CsvOutput
from .geojson import GeoJsonOutput
from .gpkg import GpkgOutput
from .parquet import ParquetOutput
from .rows import iter_door_rows


def _require_empty(directory: Path):
    directory = Path(directory)
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        raise FileExistsError(f"Refusing to write into a non-empty target: {directory}")
    directory.mkdir(parents=True, exist_ok=True)


class _Open:
    """Writers kept open while their period lasts. Rows arrive ordered by trade_date, so a period never returns."""

    def __init__(self):
        self.writers = {}
        self.closed = set()

    def get(self, key, factory):
        if key in self.closed:
            raise ValueError(f"Rows are not ordered by trade_date: {key} was already closed")
        if key not in self.writers:
            self.writers[key] = factory()
        return self.writers[key]

    def close_except(self, keep):
        for key in [k for k in self.writers if not keep(k)]:
            yield key, self.writers.pop(key)
            self.closed.add(key)


def write_output(converted, state, public_root, release_root, *, bom: bool = False, order_by=None) -> dict:
    """Create GeoParquet under release_root and GPKG, CSV and GeoJSON under public_root.

    Returns {"files": {relative path: {"format", "year", "month", "category", "rows"}}, "total_rows": n}.
    The parquet path is relative to release_root, every other path to public_root.
    """
    public_root, release_root = Path(public_root), Path(release_root)
    _require_empty(public_root)
    _require_empty(release_root)
    kwargs = {} if order_by is None else {"order_by": order_by}
    files = {}

    def record(path, root, fmt, year, month, category, rows):
        files[path.relative_to(root).as_posix()] = {
            "format": fmt, "year": year, "month": month, "category": category, "rows": rows,
        }

    parquet = ParquetOutput(release_root / "taiwanlvr.parquet")
    gpkg = _Open()       # key (year,)
    yearly = _Open()     # key (year, category)
    monthly = _Open()    # key (year, month, category), shared by csv and geojson
    total = 0

    def finish_gpkg(items):
        for (year,), writer in items:
            writer.close()
            record(writer.path, public_root, "gpkg", year, None, None, sum(writer.rows.values()))
            files[writer.path.relative_to(public_root).as_posix()]["layers"] = dict(writer.rows)

    def finish_yearly(items):
        for (year, category), writer in items:
            writer.close()
            record(writer.path, public_root, "csv", year, None, category, writer.rows)

    def finish_monthly(items):
        for (year, month, category), (csv_writer, json_writer) in items:
            for writer, fmt in ((csv_writer, "csv"), (json_writer, "geojson")):
                writer.close()
                record(writer.path, public_root, fmt, year, month, category, writer.rows)

    try:
        for row in iter_door_rows(converted, state, **kwargs):
            date = row["trade_date"]
            if date is None:
                raise ValueError(f"Row {row['transaction_id']} has no valid trade_date and cannot be placed in a period")
            year, month, category = int(date[:4]), int(date[5:7]), row["category"]
            yy, ym = f"{year:04d}", f"{year:04d}{month:02d}"
            finish_gpkg(gpkg.close_except(lambda k: k == (year,)))
            finish_yearly(yearly.close_except(lambda k: k[0] == year))
            finish_monthly(monthly.close_except(lambda k: k[:2] == (year, month)))
            gpkg.get((year,), lambda: GpkgOutput(public_root / "gpkg" / f"taiwanlvr_{yy}.gpkg", year)).add(row)
            yearly.get(
                (year, category), lambda: CsvOutput(public_root / "csv" / yy / f"taiwanlvr_{yy}_{category}.csv", bom=bom)
            ).add(row)
            pair = monthly.get(
                (year, month, category),
                lambda: (
                    CsvOutput(public_root / "csv" / yy / f"taiwanlvr_{ym}_{category}.csv", bom=bom),
                    GeoJsonOutput(public_root / "geojson" / yy / f"taiwanlvr_{ym}_{category}.geojson"),
                ),
            )
            pair[0].add(row)
            pair[1].add(row)
            parquet.add(row)
            total += 1
    finally:
        parquet.close()
        finish_gpkg(gpkg.close_except(lambda k: False))
        finish_yearly(yearly.close_except(lambda k: False))
        finish_monthly(monthly.close_except(lambda k: False))
    record(parquet.path, release_root, "geoparquet", None, None, None, parquet.rows)
    return {"files": files, "total_rows": total}
