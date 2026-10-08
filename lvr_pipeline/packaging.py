"""Verify month parity, preserve original month bytes in year packages and handoffs."""

from __future__ import annotations

import codecs
import csv
import hashlib
from itertools import zip_longest
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import time
import zipfile

import duckdb

from .address_state import load_p2
from .storage.parquet import duckdb_config
from .export import (
    POINT_COLUMNS,
    export_month,
    is_legacy_month,
    iter_points,
    verify_month,
)
from .storage.runs import (
    bindings,
    canonical_json,
    digest,
    load_snapshot,
    peak_rss_bytes,
    PRODUCER_CONFIGS,
)
from .sources import sha256_file

FORMATS = ("geoparquet", "geojson", "ndjson")
CATEGORIES = ("sales", "presale", "rent")
MAX_ASSET_BYTES = 2**31 - 1
GIS_CONTRACT = "1.0"


class PointsWriter:
    """One UTF-8 (no BOM) point CSV per year and category, written month by month."""

    def __init__(self, staging):
        self.staging = staging
        self.files = {}

    def add(self, year, category, monthly_parquet):
        for values in iter_points(monthly_parquet):
            key = (year, category)
            if key not in self.files:
                path = (
                    self.staging / "yearly" / str(year) / f"{year}_{category}_points.csv"
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                stream = path.open("w", encoding="utf-8", newline="")
                writer = csv.writer(stream, lineterminator="\n")
                writer.writerow(POINT_COLUMNS)
                self.files[key] = [path, stream, writer, 0]
            entry = self.files[key]
            entry[2].writerow(values)
            entry[3] += 1

    def close(self):
        for _, stream, _, _ in self.files.values():
            stream.close()

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


def safe_path(value):
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or ":" in value
    ):
        raise ValueError("Unsafe artifact path")
    return path


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")


def artifact(root, path, **extra):
    return {
        "path": path.relative_to(root).as_posix(),
        "asset_name": path.name,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        **extra,
    }


def zip_files(path, files):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        for name, source in files:
            safe_path(name)
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            with (
                archive.open(info, "w", force_zip64=True) as target,
                Path(source).open("rb") as stream,
            ):
                shutil.copyfileobj(stream, target, length=2**20)


MEMBER_BUDGET_OVERHEAD = 512
ZIP_BASE_OVERHEAD = 4096


def write_maintenance(staging, identifier, bundle, max_asset_bytes):
    """Write the maintenance handoff as one legacy ZIP when it fits, else indexed parts.

    Files are never split. A member is budgeted at its raw size plus ZIP overhead, so a
    part cannot exceed the limit even if its content does not compress.
    """
    sized = [
        (name, Path(source), Path(source).stat().st_size) for name, source in bundle
    ]
    for name, _, size in sized:
        if size + MEMBER_BUDGET_OVERHEAD + ZIP_BASE_OVERHEAD > max_asset_bytes:
            raise ValueError(
                f"Maintenance member exceeds configured asset limit: {name}"
            )
    total = (
        sum(size + MEMBER_BUDGET_OVERHEAD for _, _, size in sized) + ZIP_BASE_OVERHEAD
    )
    if total <= max_asset_bytes:
        path = staging / f"{identifier}_maintenance.zip"
        zip_files(path, [(name, source) for name, source, _ in sized])
        if path.stat().st_size > max_asset_bytes:
            raise ValueError("Maintenance asset exceeds configured limit")
        return [artifact(staging, path, kind="maintenance")]
    groups, current, used = [], [], ZIP_BASE_OVERHEAD
    for item in sorted(sized, key=lambda x: x[0]):
        budget = item[2] + MEMBER_BUDGET_OVERHEAD
        if current and used + budget > max_asset_bytes:
            groups.append(current)
            current, used = [], ZIP_BASE_OVERHEAD
        current.append(item)
        used += budget
    if current:
        groups.append(current)
    parts, members, assets = [], [], []
    for i, group in enumerate(groups, 1):
        path = staging / f"{identifier}_maintenance.part{i:03d}.zip"
        zip_files(path, [(name, source) for name, source, _ in group])
        item = artifact(staging, path, kind="maintenance", role="part")
        if item["bytes"] > max_asset_bytes:
            raise ValueError("Maintenance part exceeds configured limit")
        assets.append(item)
        parts.append(
            {"name": item["asset_name"], "bytes": item["bytes"], "sha256": item["sha256"]}
        )
        members.extend(
            {
                "path": name,
                "part": path.name,
                "bytes": size,
                "sha256": sha256_file(source),
            }
            for name, source, size in group
        )
    index = staging / f"{identifier}_maintenance_index.json"
    write_json(
        index,
        {
            "schema_version": "1.0",
            "kind": "maintenance-index",
            "snapshot_id": identifier,
            "parts": parts,
            "members": members,
            "part_count": len(parts),
            "member_count": len(members),
            "member_bytes": sum(m["bytes"] for m in members),
        },
    )
    return [artifact(staging, index, kind="maintenance", role="index")] + assets


def package_output(
    converted: Path,
    state: Path,
    output_dir: Path,
    *,
    notices: dict,
    run_id=None,
    max_asset_bytes=MAX_ASSET_BYTES,
):
    start = time.perf_counter()
    converted, state = Path(converted), Path(state)
    cm, cr = load_snapshot(converted, "converted")
    state_stage = json.loads((state / "quality.json").read_text(encoding="utf-8"))["stage"]
    if state_stage not in {"offline-state", "tgos-state"}:
        raise ValueError("Unsupported address state stage")
    sm, sr = load_p2(state, state_stage)
    if sr["converted_snapshot_sha256"] != sha256_file(converted / "manifest.json"):
        raise ValueError("Offline state refers to different converted observations")
    if not notices.get("publication_authorized") or not notices.get("sources"):
        raise ValueError("Public source attribution and authorization required")
    binding = bindings(
        "package-output",
        {"notices": notices, "max_asset_bytes": max_asset_bytes},
        [
            sha256_file(converted / "manifest.json"),
            sha256_file(state / "manifest.json"),
        ],
    )
    identifier = run_id or "offline-" + digest(binding)[:24]
    safe_path(identifier)
    if "/" in identifier:
        raise ValueError("Snapshot ID must be a single path segment")
    root = Path(output_dir) / identifier
    if root.exists():
        manifest = verify_output(root)
        if manifest["bindings"] != binding:
            raise ValueError("Existing output has different bindings")
        return root
    staging = Path(output_dir) / ".staging" / identifier
    staging.mkdir(parents=True, exist_ok=False)
    assets = []
    months = []
    years = []
    write_json(staging / "NOTICE.json", notices)
    assets.append(artifact(staging, staging / "NOTICE.json", kind="notice"))
    points = PointsWriter(staging)
    with tempfile.TemporaryDirectory(prefix="export-", dir=staging) as spill:
        with duckdb.connect(
            config=duckdb_config(spill)
        ) as db:
            observations = [
                str(converted / x["path"])
                for x in cm["artifacts"]
                if x["schema"] == "observation"
            ]
            db.read_parquet(observations).create_view("observations")
            db.read_parquet(str(state / "address_occurrences.parquet")).create_view(
                "occurrences"
            )
            db.read_parquet(str(state / "address_index.parquet")).create_view("results")
            db.read_parquet(
                [
                    str(converted / x["path"])
                    for x in cm["artifacts"]
                    if x["schema"] == "address-component"
                ]
            ).create_view("components")
            if db.execute(
                "SELECT count(*) FROM ((SELECT component_id,raw_record_id FROM components EXCEPT SELECT component_id,raw_record_id FROM occurrences) UNION ALL (SELECT component_id,raw_record_id FROM occurrences EXCEPT SELECT component_id,raw_record_id FROM components))"
            ).fetchone()[0]:
                raise ValueError("Handoff occurrence/source component mismatch")
            month_values = [
                x[0]
                for x in db.execute(
                    "SELECT DISTINCT tx_yyyymm FROM observations ORDER BY 1"
                ).fetchall()
            ]
            if any(x is None for x in month_values):
                raise ValueError("Invalid transaction month cannot be packaged")
            for month in month_values:
                month_record = {
                    "tx_yyyymm": month,
                    "snapshot_id": identifier,
                    "month_coverage_status": "scope_limited",
                    "source_batches": cr["batches"],
                    "categories": {},
                }
                directory = staging / "monthly" / str(month // 100) / str(month)
                for category in CATEGORIES:
                    paths, stats = export_month(db, month, category, directory)
                    if verify_month(paths) != stats:
                        raise ValueError("Month quality readback differs")
                    points.add(month // 100, category, paths["geoparquet"])
                    entries = [
                        artifact(
                            staging,
                            path,
                            kind="monthly",
                            tx_yyyymm=month,
                            category=category,
                            format=fmt,
                        )
                        for fmt, path in paths.items()
                    ]
                    assets.extend(entries)
                    month_record["categories"][category] = {
                        "status": "scope_limited"
                        if stats["rows"]
                        else "empty_in_scope",
                        **stats,
                        "files": [x["path"] for x in entries],
                    }
                path = directory / f"{month}_manifest.json"
                write_json(path, month_record)
                assets.append(artifact(staging, path, kind="month-manifest"))
                months.append(month_record)
    points.close()
    assets.extend(points.assets(max_asset_bytes))
    for year in sorted({m["tx_yyyymm"] // 100 for m in months}):
        included = [m["tx_yyyymm"] for m in months if m["tx_yyyymm"] // 100 == year]
        coverage = {
            "year": year,
            "snapshot_id": identifier,
            "year_coverage_status": "scope_limited",
            "included_months": included,
            "absent_months": [
                year * 100 + i for i in range(1, 13) if year * 100 + i not in included
            ],
            "source_batches": cr["batches"],
            "formats": {},
        }
        for fmt in FORMATS:
            files = [
                a
                for a in assets
                if a.get("kind") == "monthly"
                and a["tx_yyyymm"] // 100 == year
                and a["format"] == fmt
            ]
            parts = []
            current = []
            size = 0
            for item in files:
                budget = item["bytes"] + 512
                if budget + 4096 > max_asset_bytes:
                    raise ValueError(
                        "Single monthly asset exceeds configured asset limit; chunked month contract required"
                    )
                if current and size + budget + 4096 > max_asset_bytes:
                    parts.append(current)
                    current = []
                    size = 0
                current.append(item)
                size += budget
            if current:
                parts.append(current)
            coverage["formats"][fmt] = []
            for i, part in enumerate(parts, 1):
                name = (
                    f"lvr_{year}_{fmt}"
                    + (f"_part{i:03d}" if len(parts) > 1 else "")
                    + ".zip"
                )
                path = staging / "yearly" / str(year) / name
                zip_files(path, [(a["path"], staging / a["path"]) for a in part])
                item = artifact(
                    staging,
                    path,
                    kind="annual",
                    year=year,
                    format=fmt,
                    part=i,
                    members=[
                        {k: a[k] for k in ["path", "sha256", "bytes"]} for a in part
                    ],
                )
                if item["bytes"] > max_asset_bytes:
                    raise ValueError("Annual ZIP exceeds declared asset budget")
                assets.append(item)
                coverage["formats"][fmt].append(item["path"])
        coverage["points"] = [
            a["path"]
            for a in assets
            if a["kind"] == "annual_points" and a["year"] == year
        ]
        path = staging / "yearly" / str(year) / f"{year}_manifest.json"
        write_json(path, coverage)
        assets.append(artifact(staging, path, kind="year-manifest"))
        years.append(coverage)
    handoff = {
        "schema_version": "1.0",
        "key_version": "v2",
        "snapshot_id": identifier,
        "tgos_started": sr["tgos_started"],
        "state_stage": state_stage,
        "converted": f"converted/snapshots/{converted.name}",
        "state": f"state/snapshots/{state.name}",
        "converted_manifest_sha256": sha256_file(converted / "manifest.json"),
        "state_manifest_sha256": sha256_file(state / "manifest.json"),
    }
    descriptor = staging / "handoff.json"
    write_json(descriptor, handoff)
    bundle = []
    for prefix, snapshot in [("converted", converted), ("state", state)]:
        for source in sorted(snapshot.rglob("*")):
            if source.is_file():
                bundle.append(
                    (
                        f"{prefix}/snapshots/{snapshot.name}/{source.relative_to(snapshot).as_posix()}",
                        source,
                    )
                )
    bundle.extend(
        [("handoff.json", descriptor), ("NOTICE.json", staging / "NOTICE.json")]
    )
    maintenance_assets = write_maintenance(
        staging, identifier, bundle, max_asset_bytes
    )
    descriptor.unlink()
    assets.extend(maintenance_assets)
    size_report = {
        "snapshot_id": identifier,
        "git": "code, schemas, synthetic tests, source descriptors and small release pointers only",
        "release_asset_bytes": sum(a["bytes"] for a in assets),
        "monthly_bytes": sum(a["bytes"] for a in assets if a["kind"] == "monthly"),
        "annual_bytes": sum(a["bytes"] for a in assets if a["kind"] == "annual"),
        "annual_points_bytes": sum(
            a["bytes"] for a in assets if a["kind"] == "annual_points"
        ),
        "maintenance_bytes": sum(a["bytes"] for a in maintenance_assets),
        "maintenance_parts": sum(
            1 for a in maintenance_assets if a.get("role") == "part"
        ),
        "largest_asset_bytes": max(a["bytes"] for a in assets),
        "max_asset_bytes": max_asset_bytes,
        "retained_build_staging_bytes": sum(
            p.stat().st_size for p in staging.rglob("*") if p.is_file()
        ),
        "conservative_working_disk_budget_bytes": sum(
            p.stat().st_size for p in converted.rglob("*") if p.is_file()
        )
        + sum(p.stat().st_size for p in state.rglob("*") if p.is_file())
        + 2 * sum(a["bytes"] for a in assets)
        + 2**30,
        "elapsed_seconds": time.perf_counter() - start,
        "process_peak_rss_bytes": peak_rss_bytes(),
        "git_generated_history_growth_bytes": 0,
    }
    write_json(staging / "size_report.json", size_report)
    assets.append(artifact(staging, staging / "size_report.json", kind="size-report"))
    manifest = {
        "schema_version": "1.0",
        "key_version": "v2",
        "snapshot_id": identifier,
        "bindings": binding,
        "producer_config": PRODUCER_CONFIGS[binding["config_sha256"]],
        "record_grain": "source_observation",
        "gis_attribute_contract": GIS_CONTRACT,
        "source_batches": cr["batches"],
        "source_sha256": cr["source_sha256"],
        "scope_limited": True,
        "tgos_started": sr["tgos_started"],
        "retained_rows": cr["retained_rows"],
        "status_counts": sr["status_counts"],
        "months": months,
        "years": years,
        "assets": assets,
    }
    write_json(staging / "manifest.json", manifest)
    verify_output(staging)
    root.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, root)
    return root


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
    if gis not in {None, GIS_CONTRACT}:
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


def verify_points(root, manifest, expected_points, gis):
    """Yearly point CSVs equal the fully located single-Point rows of the monthly files.

    Output without gis_attribute_contract is the old contract: it has no such files.
    """
    declared = [a for a in manifest["assets"] if a["kind"] == "annual_points"]
    if not gis:
        if declared:
            raise ValueError("Old-contract output must not declare yearly points")
        return
    keys = [(a["year"], a["category"]) for a in declared]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate yearly points file")
    present = {
        key
        for key, files in expected_points.items()
        if any(True for f in files for _ in iter_points(f))
    }
    if set(keys) != present:
        raise ValueError("Yearly points files differ from located monthly rows")
    for item in declared:
        key = (item["year"], item["category"])
        if item["path"] != f"yearly/{key[0]}/{key[0]}_{key[1]}_points.csv":
            raise ValueError("Yearly points path differs from contract")
        path = root / item["path"]
        with path.open("rb") as stream:
            if stream.read(3) == codecs.BOM_UTF8:
                raise ValueError("Yearly points CSV must not have a BOM")
        if item["columns"] != POINT_COLUMNS:
            raise ValueError("Yearly points columns differ from contract")
        expected = (v for f in expected_points[key] for v in iter_points(f))
        count = 0
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.reader(stream)
            if next(reader, None) != POINT_COLUMNS:
                raise ValueError("Yearly points columns differ from contract")
            for row, want in zip_longest(reader, expected):
                if row != want:
                    raise ValueError("Yearly points content differs from monthly files")
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
