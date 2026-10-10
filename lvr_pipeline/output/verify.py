"""Verify month parity, yearly points against monthly files, and output manifests."""

from __future__ import annotations

import codecs
import csv
import hashlib
import json
import math
import shutil
from itertools import zip_longest
from pathlib import Path
import tempfile
import zipfile

import pyarrow.parquet as pq

from ..results.reconcile import load_p2
from ..source_ref import resolve_source_ref
from ..storage.runs import digest, load_snapshot, sha256_file
from . import monthly
from .monthly import ATTRIBUTE_NAMES, FORMATS, MAX_ASSET_BYTES, feature, output_schema, safe_path
from .yearly import (
    EARTH_RADIUS_M,
    POINT_COLUMNS,
    POINT_COLUMNS_V10,
    SPREAD_LIMIT_M,
    iter_points,
    iter_points_with_id,
    iter_spread_points,
)


def is_legacy_month(path):
    """True for monthly GeoParquet written before the GIS attribute contract."""
    return "trade_date" not in pq.ParquetFile(path).schema_arrow.names


def haversine_m(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, a)))


def verify_month(paths):
    parquet = pq.ParquetFile(paths["geoparquet"])
    declared_types = json.loads(parquet.schema_arrow.metadata[b"geo"])["columns"][
        "geometry"
    ]["geometry_types"]
    legacy = "trade_date" not in parquet.schema_arrow.names
    if not parquet.schema_arrow.equals(
        output_schema(declared_types, legacy), check_metadata=True
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
                    if not legacy:
                        derived = monthly.attributes(row, shape)
                        if any(row[k] != derived[k] for k in ATTRIBUTE_NAMES):
                            raise ValueError("GIS attribute fields differ from source")
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


# 1.0 yearly points carry raw_record_id; 1.1 carry source_ref; 1.2 also spreads shared coordinates (<= 1 m)
GIS_CONTRACTS = {"1.0", "1.1", "1.2"}


def verify_output(root: Path):
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if (
        manifest["schema_version"] != "1.0"
        or manifest["key_version"] != "v2"
        or manifest["record_grain"] != "source_observation"
    ):
        raise ValueError("Unsupported public snapshot contract")
    if digest(manifest["producer_config"]) != manifest["bindings"]["config_sha256"]:
        raise ValueError("Output producer configuration mismatch")
    if not isinstance(manifest["tgos_started"], bool) or manifest["scope_limited"] is not True:
        raise ValueError("Unsupported output scope/state")
    names = set()
    paths = set()
    monthly = {}
    total = 0
    for item in manifest["assets"]:
        safe_path(item["path"])
        safe_path(item["asset_name"])
        if (
            item["asset_name"] != Path(item["path"]).name
            or item["asset_name"] in names
            or item["path"] in paths
        ):
            raise ValueError("Duplicate/ambiguous public asset")
        names.add(item["asset_name"])
        paths.add(item["path"])
        path = root / item["path"]
        if path.stat().st_size != item["bytes"] or sha256_file(path) != item["sha256"]:
            raise ValueError("Public asset hash/size mismatch")
        if item["bytes"] > MAX_ASSET_BYTES:
            raise ValueError("GitHub asset exceeds 2GiB")
        if item["kind"] == "monthly":
            monthly.setdefault((item["tx_yyyymm"], item["category"]), {})[
                item["format"]
            ] = path
    gis = manifest.get("gis_attribute_contract")
    if gis is not None and gis not in GIS_CONTRACTS:
        raise ValueError("Unsupported GIS attribute contract")
    expected_points = {}
    for (month, category), files in sorted(monthly.items()):
        if set(files) != set(FORMATS):
            raise ValueError("Missing monthly output format")
        if gis and is_legacy_month(files["geoparquet"]):
            raise ValueError("Monthly file lacks the declared GIS attribute contract")
        stats = verify_month(files)
        if gis:
            expected_points.setdefault((month // 100, category), []).append(
                files["geoparquet"]
            )
        declared = next(m for m in manifest["months"] if m["tx_yyyymm"] == month)[
            "categories"
        ][category]
        if any(declared[k] != v for k, v in stats.items()):
            raise ValueError("Monthly manifest statistics mismatch")
        total += stats["rows"]
    if (
        len(monthly) != len(manifest["months"]) * 3
        or total != manifest["retained_rows"]
    ):
        raise ValueError("Output month coverage/count mismatch")
    verify_points(root, manifest, expected_points, gis)
    annual_members = []
    for item in manifest["assets"]:
        if item["kind"] != "annual":
            continue
        with zipfile.ZipFile(root / item["path"]) as archive:
            if sorted(archive.namelist()) != sorted(a["path"] for a in item["members"]):
                raise ValueError("Annual member list mismatch")
            for member in item["members"]:
                h = hashlib.sha256()
                size = 0
                with archive.open(member["path"]) as stream:
                    while chunk := stream.read(2**20):
                        h.update(chunk)
                        size += len(chunk)
                if h.hexdigest() != member["sha256"] or size != member["bytes"]:
                    raise ValueError("Annual month bytes differ")
                if sha256_file(root / member["path"]) != member["sha256"]:
                    raise ValueError("Annual file differs from released month")
                annual_members.append(member["path"])
    expected = [a["path"] for a in manifest["assets"] if a["kind"] == "monthly"]
    if sorted(annual_members) != sorted(expected):
        raise ValueError("Annual coverage duplicates or omits monthly assets")
    maintenance = [a for a in manifest["assets"] if a["kind"] == "maintenance"]
    index = [a for a in maintenance if a.get("role") == "index"]
    if index:
        if len(index) != 1 or any(
            a.get("role") not in {"index", "part"} for a in maintenance
        ):
            raise ValueError("Missing maintenance handoff")
        declared = json.loads((root / index[0]["path"]).read_text(encoding="utf-8"))
        if sorted(p["name"] for p in declared["parts"]) != sorted(
            a["asset_name"] for a in maintenance if a["role"] == "part"
        ):
            raise ValueError("Maintenance index differs from released parts")
        entry = root / index[0]["path"]
    elif len(maintenance) == 1:
        entry = root / maintenance[0]["path"]
    else:
        raise ValueError("Missing maintenance handoff")
    with tempfile.TemporaryDirectory(prefix="handoff-verify-") as folder:
        extract_handoff(entry, Path(folder))
    return manifest


def _within_one_metre(row, original):
    try:
        lon, lat = float(row[POINT_COLUMNS.index("longitude")]), float(row[POINT_COLUMNS.index("latitude")])
    except (ValueError, IndexError, TypeError):
        return False
    return haversine_m(lon, lat, *original) <= SPREAD_LIMIT_M


def verify_points(root, manifest, expected_points, gis):
    """Yearly point CSVs equal the fully located single-Point rows of the monthly files.

    Output without gis_attribute_contract is the old contract: it has no such files.
    """
    declared = [a for a in manifest["assets"] if a["kind"] == "annual_points"]
    if not gis:
        if declared:
            raise ValueError("Old-contract output must not declare yearly points")
        return
    legacy = gis == "1.0"
    keys = [(a["year"], a["category"]) for a in declared]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate yearly points file")
    present = {
        key
        for key, files in expected_points.items()
        if any(True for f in files for _ in iter_points(f, legacy))
    }
    if set(keys) != present:
        raise ValueError("Yearly points files differ from located monthly rows")
    columns = POINT_COLUMNS_V10 if legacy else POINT_COLUMNS
    hashes = manifest.get("source_sha256", {})
    for item in declared:
        key = (item["year"], item["category"])
        if item["path"] != f"yearly/{key[0]}/{key[0]}_{key[1]}_points.csv":
            raise ValueError("Yearly points path differs from contract")
        path = root / item["path"]
        with path.open("rb") as stream:
            if stream.read(3) == codecs.BOM_UTF8:
                raise ValueError("Yearly points CSV must not have a BOM")
        if item["columns"] != columns:
            raise ValueError("Yearly points columns differ from contract")
        spread = gis == "1.2"
        if spread:
            expected = iter_spread_points(expected_points[key])
        else:
            expected = (v + (None,) for f in expected_points[key] for v in iter_points_with_id(f, legacy))
        count = 0
        seen = set()
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.reader(stream)
            if next(reader, None) != columns:
                raise ValueError("Yearly points columns differ from contract")
            for row, want in zip_longest(reader, expected):
                if want is None or row is None:
                    raise ValueError("Yearly points content differs from monthly files")
                if spread and not _within_one_metre(row, want[2]):
                    raise ValueError("Yearly points coordinate is more than 1 m from its monthly coordinate")
                if row != want[0]:
                    raise ValueError("Yearly points content differs from monthly files")
                if not legacy:
                    ref = row[-1]
                    if ref in seen:
                        raise ValueError("Yearly points source_ref is not unique")
                    seen.add(ref)
                    if resolve_source_ref(ref, hashes)["raw_record_id"] != want[1]:
                        raise ValueError("Yearly points source_ref does not map to its monthly observation")
                count += 1
        if count != item["rows"]:
            raise ValueError("Yearly points row count differs from monthly files")


def extract_zip(archive_path, target, budget=8 * 2**30):
    """Extract a handoff ZIP safely; returns the extracted entry names."""
    target = Path(target)
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names):
            raise ValueError("Duplicate handoff ZIP entry")
        if sum(i.file_size for i in archive.infolist()) > budget:
            raise ValueError("Handoff exceeds 8GiB extraction budget")
        for info in archive.infolist():
            safe_path(info.filename)
            if info.external_attr >> 16 & 0o170000 == 0o120000:
                raise ValueError("Handoff symlink forbidden")
            destination = target / info.filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise FileExistsError(destination)
            with archive.open(info) as source, destination.open("xb") as stream:
                shutil.copyfileobj(source, stream, 2**20)
    return names


def extract_indexed_handoff(index_path, target):
    index_path, target = Path(index_path), Path(target)
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if index.get("schema_version") != "1.0" or index.get("kind") != "maintenance-index":
        raise ValueError("Unsupported maintenance index")
    parts = index["parts"]
    members = index["members"]
    if (
        len({p["name"] for p in parts}) != len(parts)
        or len({m["path"] for m in members}) != len(members)
        or index["part_count"] != len(parts)
        or index["member_count"] != len(members)
        or index["member_bytes"] != sum(m["bytes"] for m in members)
    ):
        raise ValueError("Maintenance index totals mismatch")
    if index["member_bytes"] > 8 * 2**30:
        raise ValueError("Handoff exceeds 8GiB extraction budget")
    for part in parts:
        safe_path(part["name"])
        if "/" in part["name"]:
            raise ValueError("Unsafe artifact path")
        path = index_path.parent / part["name"]
        if not path.is_file():
            raise ValueError("Maintenance part missing")
        if path.stat().st_size != part["bytes"] or sha256_file(path) != part["sha256"]:
            raise ValueError("Maintenance part hash/size mismatch")
    for member in members:
        safe_path(member["path"])
    part_names = {p["name"] for p in parts}
    if any(m["part"] not in part_names for m in members):
        raise ValueError("Maintenance member refers to unknown part")
    for part in parts:
        names = extract_zip(index_path.parent / part["name"], target)
        if sorted(names) != sorted(
            m["path"] for m in members if m["part"] == part["name"]
        ):
            raise ValueError("Maintenance part member list differs from index")
    extracted = {
        p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()
    }
    if extracted != {m["path"] for m in members}:
        raise ValueError("Extracted maintenance files differ from index")
    for member in members:
        path = target / member["path"]
        if (
            path.stat().st_size != member["bytes"]
            or sha256_file(path) != member["sha256"]
        ):
            raise ValueError("Maintenance member hash/size mismatch")


def extract_handoff(archive_path: Path, target: Path):
    """Extract a legacy single maintenance ZIP, or an indexed set of parts (.json)."""
    target = Path(target)
    if Path(archive_path).suffix == ".json":
        extract_indexed_handoff(archive_path, target)
    else:
        extract_zip(archive_path, target)
    handoff = json.loads((target / "handoff.json").read_text(encoding="utf-8"))
    if (
        handoff["schema_version"] != "1.0"
        or handoff["key_version"] != "v2"
        or not isinstance(handoff["tgos_started"], bool)
    ):
        raise ValueError(
            "Incomplete or incompatible handoff; rebuild required when source fields are unavailable"
        )
    for key in ["converted", "state"]:
        safe_path(handoff[key])
        if (
            sha256_file(target / handoff[key] / "manifest.json")
            != handoff[key + "_manifest_sha256"]
        ):
            raise ValueError("Handoff manifest differs")
    cm, _ = load_snapshot(target / handoff["converted"], "converted")
    state_stage = handoff.get("state_stage", "offline-state")
    if state_stage not in {"offline-state", "tgos-state"}:
        raise ValueError("Unsupported handoff state stage")
    _, sr = load_p2(target / handoff["state"], state_stage)
    required = {
        "address-result",
        "address-pool",
        "address-occurrence",
        "offline-row",
        "unmatched-address",
        "verified-alias",
        "tgos-ledger",
    }
    if not required.issubset(sr["dataset_counts"]):
        raise ValueError(
            "Missing maintenance state; compatible snapshot or raw rebuild required"
        )
    if sr["converted_snapshot_sha256"] != handoff["converted_manifest_sha256"]:
        raise ValueError("Handoff source/state mismatch")
    return handoff
