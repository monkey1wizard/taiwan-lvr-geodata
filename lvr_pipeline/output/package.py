"""Assemble one output package: monthly files, yearly packages, points, maintenance and manifest."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time

import duckdb

from ..results.reconcile import load_p2
from ..storage.parquet import duckdb_config
from ..storage.runs import PRODUCER_CONFIGS, bindings, digest, load_snapshot, peak_rss_bytes, sha256_file
from . import yearly
from .monthly import CATEGORIES, FORMATS, MAX_ASSET_BYTES, artifact, export_month, safe_path, write_json, zip_files
from .publish import write_maintenance
from .verify import verify_month, verify_output


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
    points = yearly.PointsWriter(staging)
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
        "gis_attribute_contract": yearly.GIS_CONTRACT,
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
