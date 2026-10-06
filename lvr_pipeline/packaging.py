"""Verify month parity, preserve original month bytes in year packages and handoffs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import time
import zipfile

import duckdb

from .address_state import load_p2
from .parquet_io import duckdb_config
from .export import export_month, verify_month
from .processing import (
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
    maintenance = staging / f"{identifier}_maintenance.zip"
    zip_files(maintenance, bundle)
    descriptor.unlink()
    assets.append(artifact(staging, maintenance, kind="maintenance"))
    if maintenance.stat().st_size > max_asset_bytes:
        raise ValueError(
            "Maintenance asset exceeds configured limit; chunked state contract required"
        )
    size_report = {
        "snapshot_id": identifier,
        "git": "code, schemas, synthetic tests, source descriptors and small release pointers only",
        "release_asset_bytes": sum(a["bytes"] for a in assets),
        "monthly_bytes": sum(a["bytes"] for a in assets if a["kind"] == "monthly"),
        "annual_bytes": sum(a["bytes"] for a in assets if a["kind"] == "annual"),
        "maintenance_bytes": maintenance.stat().st_size,
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
    for (month, category), files in monthly.items():
        if set(files) != set(FORMATS):
            raise ValueError("Missing monthly output format")
        stats = verify_month(files)
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
    if len(maintenance) != 1:
        raise ValueError("Missing maintenance handoff")
    with tempfile.TemporaryDirectory(prefix="handoff-verify-") as folder:
        extract_handoff(root / maintenance[0]["path"], Path(folder))
    return manifest


def extract_handoff(archive_path: Path, target: Path):
    target = Path(target)
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names):
            raise ValueError("Duplicate handoff ZIP entry")
        if sum(i.file_size for i in archive.infolist()) > 8 * 2**30:
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
