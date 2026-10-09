"""Pinned address evidence with explicit administrative and coordinate contracts.

Also creates a reproducible descriptor for a clean taiwan-address-data checkout.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
import re
import subprocess

from ..addresses import identity
from ..addresses.identity import building_key_v2
from ..transactions.normalize import normalize_address
from ..storage.parquet import BatchWriter
from ..contracts.validate import TAIWAN_BOUNDS, TAIWAN_BOUNDS_RULE, within_taiwan_bounds
from ..storage.runs import Stage, bindings, digest
from ..storage.runs import sha256_file
from .match import COUNTY_NAMES, AdministrativeNames

ADMINISTRATIVE = [
    "area_1984.csv",
    "area_2010.csv",
    "area_2014.csv",
    "area_2015.csv",
    "area_custom.csv",
]
METADATA = ["README.md", "update_log.csv", "selection.txt", "scripts/update_addresses.py"]


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, encoding="utf-8"
    ).strip()


def pin_address_source(root: Path, output: Path) -> dict:
    root = Path(root).resolve()
    if _git(root, "status", "--porcelain"):
        raise ValueError("Address source checkout must be clean")
    commit = _git(root, "rev-parse", "HEAD")

    def describe(relative: str) -> dict:
        path = root / relative
        if not path.is_file():
            raise ValueError(f"Address source file missing: {relative}")
        return {
            "path": relative.replace("\\", "/"),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }

    roads = [
        describe(path.relative_to(root).as_posix())
        for path in sorted((root / "roads").glob("*.csv"), key=lambda x: x.name)
    ]
    if not roads:
        raise ValueError("Address source has no road files")
    descriptor = {
        "schema_version": "1.0",
        "repository": "https://github.com/monkey1wizard/taiwan-address-data",
        "commit": commit,
        "source_kind": "legacy_base",
        "roads": roads,
        "road_count": len(roads),
        "size_bytes": sum(row["size_bytes"] for row in roads),
        "license_status": "pending_upstream_evidence",
        "license_evidence": "docs/drafts/taiwan-lvr-geodata-完整企劃.md#已確認現況",
        "rights_note": "Each upstream dataset requires its own redistribution evidence before public coordinate release.",
        "administrative_files": [describe(name) for name in ADMINISTRATIVE],
        "road_index": describe("road.csv"),
        "source_metadata": [describe(name) for name in METADATA if (root / name).is_file()],
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(descriptor, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return descriptor


def load_pinned_source(root: Path, descriptor: dict):
    head = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    if head != descriptor["commit"]:
        raise ValueError("Address checkout differs from pinned commit")
    dirty = subprocess.check_output(
        ["git", "-C", str(root), "status", "--porcelain"], text=True
    ).strip()
    if dirty:
        raise ValueError("Address checkout must be clean")
    return AdministrativeNames(root, descriptor["administrative_files"])


def _selected_roads(root: Path, descriptor: dict, counties) -> list[dict]:
    selected = [
        x
        for x in descriptor["roads"]
        if not counties or x["path"].split("/")[1].split("-")[0] in counties
    ]
    for item in selected:
        path = root / item["path"]
        if sha256_file(path) != item["sha256"]:
            raise ValueError("Address road hash mismatch")
    return selected


def _legacy_rows(root: Path, selected: list[dict]):
    """Yield (item, CSV line number, row) from pinned legacy road files."""
    for item in selected:
        with (root / item["path"]).open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not {"FULL_ADDR", "COUNTY", "TOWN", "ROAD", "X", "Y"}.issubset(
                reader.fieldnames
            ):
                raise ValueError("Address CSV columns missing")
            for row in reader:
                yield item, reader.line_num, row
        if sha256_file(root / item["path"]) != item["sha256"]:
            raise ValueError("Road input changed")


def _assess(names: AdministrativeNames, address, county, town, x, y):
    """Classify one source row; X is longitude and Y latitude, as in the index.

    Returns the parsed coordinate even when the row is invalid for other
    reasons; callers decide whether to keep it.
    """
    canonical, c, t = names.canonicalize(address)
    key = building_key_v2(canonical)
    validity = "valid"
    if c != county or t != town:
        validity = "invalid_admin"
    if not c:
        key = None
    if validity == "valid" and key is None:
        validity = "invalid_address"
    try:
        lng, lat = float(x), float(y)
        if not within_taiwan_bounds(lng, lat):
            raise ValueError("coordinate bounds")
    except (TypeError, ValueError):
        lng = lat = None
        validity = "invalid_coordinate"
    return canonical, c, t, key, lng, lat, validity


def build_index(
    root: Path,
    descriptor: dict,
    work_dir: Path,
    *,
    counties: list[str] | None = None,
    official: dict | None = None,
    run_id=None,
):
    root = Path(root)
    if official:
        if counties and sorted(counties) != [official["county_code"]]:
            raise ValueError(
                "Official coordinate coverage differs from declared county scope"
            )
        counties = [official["county_code"]]
    names = load_pinned_source(root, descriptor)
    selected = _selected_roads(root, descriptor, counties)
    official_metadata = (
        {k: v for k, v in official.items() if k != "path"} if official else None
    )
    parameters = {
        "descriptor": descriptor,
        "counties": sorted(counties or COUNTY_NAMES),
        "official": official_metadata,
        "normalization_version": identity.NORMALIZATION_VERSION,
        # R05-10: coordinates outside these bounds are invalid_coordinate.
        "coordinate_bounds_rule": TAIWAN_BOUNDS_RULE,
        "coordinate_bounds": list(TAIWAN_BOUNDS),
    }
    input_hashes = [digest(descriptor)]
    if official:
        path = Path(official["path"])
        if sha256_file(path) != official["sha256"]:
            raise ValueError("Official address hash mismatch")
        if official["crs"] not in {"EPSG:3826", "EPSG:4326"}:
            raise ValueError("Declared source CRS required")
        input_hashes.append(official["sha256"])
    stage = Stage(
        Path(work_dir) / "offline-index",
        "offline-index",
        bindings("offline-index", parameters, input_hashes),
        run_id,
    )
    if stage.reused:
        return stage.path
    writer = BatchWriter(stage.build / "offline_rows.parquet", "offline-row", 1024)
    stats = {
        "rows": 0,
        "valid_rows": 0,
        "invalid_rows": 0,
        "address_source_commit": descriptor["commit"],
        "address_counties": sorted(counties or COUNTY_NAMES),
        "official_source": official_metadata,
        "legacy_base": official is None,
    }

    def add(address, county, town, x, y, ref, sha, line, kind):
        canonical, c, t, key, lng, lat, validity = _assess(
            names, address, county, town, x, y
        )
        if validity != "valid":
            lng = lat = None
        writer.add(
            {
                "evidence_id": digest([sha, ref, line]),
                "key_version": "v2",
                "building_key": key,
                "normalized_address": canonical,
                "county_code": c or county,
                "town_code": t or town,
                "lng": lng,
                "lat": lat,
                "validity": validity,
                "source_kind": kind,
                "source_ref": ref,
                "input_sha256": sha,
                "source_row_number": line,
            }
        )
        stats["rows"] += 1
        stats["valid_rows"] += validity == "valid"
        stats["invalid_rows"] += validity != "valid"

    try:
        if official:
            from pyproj import Transformer

            transform = Transformer.from_crs(
                official["crs"], "EPSG:4326", always_xy=True
            )
            with Path(official["path"]).open(
                encoding="utf-8-sig", newline=""
            ) as stream:
                reader = csv.DictReader(stream)
                for row in reader:
                    county = official["county_code"]
                    original = row["鄉鎮市區代碼"].strip()
                    if row["省市縣市代碼"].strip() != county + "000":
                        raise ValueError(
                            "Official county code differs from declared scope"
                        )
                    if not re.fullmatch(county + "[0-9]{6}", original):
                        raise ValueError("Unexpected declared MOI municipality code")
                    town = county + "0" + str(int(original[-3:]) * 10).zfill(4)
                    if town not in names.current:
                        raise ValueError("Unknown official town code")

                    def part(value, unit):
                        value = normalize_address(value)
                        return (
                            value
                            if not value
                            or (unit == "號" and "號" in value)
                            or value.endswith(unit)
                            else value + unit
                        )

                    street = normalize_address(row["街路段"])
                    locality = street or normalize_address(row["村里"] + row["地區"])
                    address = (
                        names.current[town]
                        + locality
                        + part(row["巷"], "巷")
                        + part(row["弄"], "弄")
                        + part(row["號"], "號")
                    )
                    try:
                        lng, lat = transform.transform(
                            float(row["橫座標"]), float(row["縱座標"])
                        )
                    except ValueError:
                        lng = lat = None
                    add(
                        address,
                        county,
                        town,
                        lng,
                        lat,
                        "gov/" + str(official["dataset_id"]),
                        official["sha256"],
                        reader.line_num,
                        "official_snapshot",
                    )
            if sha256_file(Path(official["path"])) != official["sha256"]:
                raise ValueError("Official input changed")
        else:
            for item, line, row in _legacy_rows(root, selected):
                add(
                    row["FULL_ADDR"],
                    row["COUNTY"],
                    row["TOWN"],
                    row["X"],
                    row["Y"],
                    item["path"],
                    item["sha256"],
                    line,
                    "legacy_base",
                )
    finally:
        writer.close()
    stats["dataset_counts"] = {"offline-row": writer.row_count}
    load_pinned_source(root, descriptor)
    return stage.finish(
        [("offline_rows.parquet", writer.path, "offline-row", writer.row_count)], stats
    )


EARTH_RADIUS_M = 6_371_008.8
AUDIT_FILES = {
    "exact_duplicates": "exact_duplicates.parquet",
    "key_text_variants": "key_text_variants.parquet",
    "key_coordinate_variants": "key_coordinate_variants.parquet",
    "unparsed_addresses": "unparsed_addresses.parquet",
    "invalid_admin": "invalid_admin.parquet",
}


def _haversine_sql(a: str, b: str) -> str:
    """Great-circle distance in metres on a sphere of radius EARTH_RADIUS_M."""
    return (
        f"2 * {EARTH_RADIUS_M} * asin(sqrt(least(1.0, "
        f"pow(sin(radians({b}.lat - {a}.lat) / 2), 2) + "
        f"cos(radians({a}.lat)) * cos(radians({b}.lat)) * "
        f"pow(sin(radians({b}.lng - {a}.lng) / 2), 2))))"
    )


def audit_address_source(
    root: Path,
    descriptor: dict,
    output_dir: Path,
    *,
    counties: list[str] | None = None,
) -> dict:
    """Report duplicates and conflicts in the pinned legacy address base.

    Reads rows exactly as ``build_index`` does and writes one detail Parquet per
    category plus ``summary.json``. It never chooses a row, never changes the
    offline index and never alters coordinates. Categories may overlap:

    1. exact_duplicates: identical source rows (all CSV columns, any file).
    2. key_text_variants: index-valid rows sharing building_key_v2 with
       different FULL_ADDR text.
    3. key_coordinate_variants: index-valid rows sharing building_key_v2 with
       different coordinates; carries the largest pairwise distance.
    4. unparsed_addresses: building_key_v2 is None.
    5. invalid_admin: the parsed county or town differs from COUNTY/TOWN.

    Distances use the haversine formula on a sphere of radius 6,371,008.8 m
    with X as longitude and Y as latitude, the axis order ``build_index`` uses.
    """
    import tempfile

    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq

    from ..storage.parquet import duckdb_config

    root, output_dir = Path(root), Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"Audit output directory is not empty: {output_dir}")
    names = load_pinned_source(root, descriptor)
    selected = _selected_roads(root, descriptor, counties)
    schema = pa.schema(
        [
            ("source_ref", pa.string()),
            ("source_row_number", pa.int64()),
            ("raw_row", pa.string()),
            ("full_addr", pa.string()),
            ("source_county", pa.string()),
            ("source_town", pa.string()),
            ("raw_x", pa.string()),
            ("raw_y", pa.string()),
            ("normalized_address", pa.string()),
            ("county_code", pa.string()),
            ("town_code", pa.string()),
            ("building_key", pa.string()),
            ("lng", pa.float64()),
            ("lat", pa.float64()),
            ("admin_valid", pa.bool_()),
            ("validity", pa.string()),
        ]
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lvr-address-audit-") as temp:
        rows_path = Path(temp) / "rows.parquet"
        writer = pq.ParquetWriter(rows_path, schema, compression="zstd")
        buffer = []
        total = 0
        try:
            for item, line, row in _legacy_rows(root, selected):
                canonical, c, t, key, lng, lat, validity = _assess(
                    names, row["FULL_ADDR"], row["COUNTY"], row["TOWN"], row["X"], row["Y"]
                )
                buffer.append(
                    {
                        "source_ref": item["path"],
                        "source_row_number": line,
                        "raw_row": json.dumps(
                            [[str(k), v] for k, v in row.items()], ensure_ascii=False
                        ),
                        "full_addr": row["FULL_ADDR"],
                        "source_county": row["COUNTY"],
                        "source_town": row["TOWN"],
                        "raw_x": row["X"],
                        "raw_y": row["Y"],
                        "normalized_address": canonical,
                        "county_code": c or row["COUNTY"],
                        "town_code": t or row["TOWN"],
                        "building_key": key,
                        "lng": lng,
                        "lat": lat,
                        "admin_valid": c == row["COUNTY"] and t == row["TOWN"],
                        "validity": validity,
                    }
                )
                if len(buffer) >= 50_000:
                    writer.write_table(pa.Table.from_pylist(buffer, schema=schema))
                    total += len(buffer)
                    buffer.clear()
            if buffer:
                writer.write_table(pa.Table.from_pylist(buffer, schema=schema))
                total += len(buffer)
        finally:
            writer.close()
        load_pinned_source(root, descriptor)
        summary = _audit_queries(
            duckdb, duckdb_config(Path(temp) / "spill"), rows_path, output_dir
        )
    if summary["rows"] != total:
        raise ValueError("Audit row count mismatch")
    summary = {
        "schema_version": "1.0",
        "address_source_commit": descriptor["commit"],
        "address_counties": sorted(counties or COUNTY_NAMES),
        "road_files": len(selected),
        "normalization_version": identity.NORMALIZATION_VERSION,
        "distance_method": "haversine, sphere radius 6371008.8 m, X=longitude Y=latitude",
        "categories_overlap": True,
        "files": AUDIT_FILES,
        **summary,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def _audit_queries(duckdb, config: dict, rows_path: Path, output_dir: Path) -> dict:
    def out(name: str) -> str:
        return str(output_dir / AUDIT_FILES[name]).replace("'", "''")

    def parquet(name: str) -> str:
        return f"read_parquet('{out(name)}')"

    distance = _haversine_sql("a", "b")
    with duckdb.connect(config=config) as db:
        db.read_parquet(str(rows_path)).create_view("rows")
        db.sql("CREATE TEMP VIEW valid AS SELECT * FROM rows WHERE validity = 'valid'")
        db.sql(
            f"""COPY (
                SELECT md5(raw_row) AS duplicate_group, any_value(full_addr) AS full_addr,
                       any_value(source_county) AS source_county, count(*) AS row_count,
                       list(source_ref || ':' || source_row_number
                            ORDER BY source_ref, source_row_number) AS sources,
                       raw_row
                FROM rows GROUP BY raw_row HAVING count(*) > 1
                ORDER BY source_county, duplicate_group
            ) TO '{out("exact_duplicates")}' (FORMAT parquet, COMPRESSION zstd)"""
        )
        db.sql(
            f"""COPY (
                WITH k AS (SELECT building_key FROM valid GROUP BY building_key
                           HAVING count(DISTINCT full_addr) > 1)
                SELECT v.building_key, any_value(v.county_code) AS county_code,
                       v.full_addr, any_value(v.normalized_address) AS normalized_address,
                       count(*) AS row_count,
                       list(v.source_ref || ':' || v.source_row_number
                            ORDER BY v.source_ref, v.source_row_number) AS sources
                FROM valid v JOIN k USING (building_key)
                GROUP BY v.building_key, v.full_addr
                ORDER BY county_code, v.building_key, v.full_addr
            ) TO '{out("key_text_variants")}' (FORMAT parquet, COMPRESSION zstd)"""
        )
        db.sql(
            """CREATE TEMP TABLE coords AS
               WITH k AS (SELECT building_key, count(*) AS row_count FROM valid
                          GROUP BY building_key HAVING count(DISTINCT (lng, lat)) > 1)
               SELECT v.building_key, k.row_count, v.county_code, v.lng, v.lat,
                      count(*) AS coordinate_rows
               FROM valid v JOIN k USING (building_key)
               GROUP BY v.building_key, k.row_count, v.county_code, v.lng, v.lat"""
        )
        db.sql(
            f"""COPY (
                WITH pairs AS (
                    SELECT a.building_key, a.lng AS lng_a, a.lat AS lat_a,
                           b.lng AS lng_b, b.lat AS lat_b, {distance} AS distance_m
                    FROM coords a JOIN coords b ON a.building_key = b.building_key
                         AND (a.lng, a.lat) < (b.lng, b.lat)
                ),
                best AS (
                    SELECT building_key,
                           arg_max(struct_pack(lng_a, lat_a, lng_b, lat_b), distance_m) AS pair,
                           max(distance_m) AS max_distance_m
                    FROM pairs GROUP BY building_key
                ),
                keys AS (
                    SELECT building_key, any_value(row_count) AS row_count,
                           min(county_code) AS county_code,
                           count(*) AS distinct_coordinates
                    FROM coords GROUP BY building_key
                )
                SELECT keys.building_key, keys.county_code, keys.row_count,
                       keys.distinct_coordinates, best.max_distance_m,
                       best.pair.lng_a AS lng_a, best.pair.lat_a AS lat_a,
                       best.pair.lng_b AS lng_b, best.pair.lat_b AS lat_b
                FROM keys JOIN best USING (building_key)
                ORDER BY keys.county_code, keys.building_key
            ) TO '{out("key_coordinate_variants")}' (FORMAT parquet, COMPRESSION zstd)"""
        )
        detail = """source_ref, source_row_number, full_addr, source_county, source_town,
                    raw_x, raw_y, normalized_address, county_code, town_code,
                    building_key, admin_valid, validity"""
        db.sql(
            f"""COPY (SELECT {detail} FROM rows WHERE building_key IS NULL
                      ORDER BY source_ref, source_row_number)
                TO '{out("unparsed_addresses")}' (FORMAT parquet, COMPRESSION zstd)"""
        )
        db.sql(
            f"""COPY (SELECT {detail} FROM rows WHERE NOT admin_valid
                      ORDER BY source_ref, source_row_number)
                TO '{out("invalid_admin")}' (FORMAT parquet, COMPRESSION zstd)"""
        )

        def one(sql):
            return db.sql(sql).fetchone()

        rows = one("SELECT count(*) FROM rows")[0]
        dup = one(
            f"SELECT count(*), coalesce(sum(row_count), 0) FROM {parquet('exact_duplicates')}"
        )
        text = one(
            "SELECT count(DISTINCT building_key), coalesce(sum(row_count), 0) "
            f"FROM {parquet('key_text_variants')}"
        )
        coord = one(
            "SELECT count(*), coalesce(sum(row_count), 0) "
            f"FROM {parquet('key_coordinate_variants')}"
        )
        quantiles = one(
            "SELECT quantile_cont(max_distance_m, [0.5, 0.9, 0.99, 0.999]), "
            f"max(max_distance_m) FROM {parquet('key_coordinate_variants')}"
        )
        multi = one(
            "SELECT count(*), coalesce(sum(n), 0) FROM (SELECT count(*) AS n FROM valid "
            "GROUP BY building_key HAVING count(*) > 1)"
        )
        unparsed = one(f"SELECT count(*) FROM {parquet('unparsed_addresses')}")[0]
        admin = one(f"SELECT count(*) FROM {parquet('invalid_admin')}")[0]
        validity = dict(
            db.sql("SELECT validity, count(*) FROM rows GROUP BY 1 ORDER BY 1").fetchall()
        )
        by_county = {}
        for county, status, count in db.sql(
            "SELECT source_county, validity, count(*) FROM rows GROUP BY 1, 2 ORDER BY 1, 2"
        ).fetchall():
            by_county.setdefault(county, {})[status] = count
        coord_by_county = dict(
            db.sql(
                f"SELECT county_code, count(*) FROM {parquet('key_coordinate_variants')} "
                "GROUP BY 1 ORDER BY 1"
            ).fetchall()
        )
    q = quantiles[0] or [None] * 4
    return {
        "rows": rows,
        "validity_counts": validity,
        "validity_by_source_county": by_county,
        "multi_row_valid_keys": {"keys": multi[0], "rows": int(multi[1])},
        "categories": {
            "exact_duplicates": {"groups": dup[0], "rows": int(dup[1])},
            "key_text_variants": {"keys": text[0], "rows": int(text[1])},
            "key_coordinate_variants": {
                "keys": coord[0],
                "rows": int(coord[1]),
                "max_distance_m_quantiles": {
                    "p50": q[0],
                    "p90": q[1],
                    "p99": q[2],
                    "p999": q[3],
                    "max": quantiles[1],
                },
                "keys_by_county": coord_by_county,
            },
            "unparsed_addresses": {"rows": unparsed},
            "invalid_admin": {"rows": admin},
        },
    }
