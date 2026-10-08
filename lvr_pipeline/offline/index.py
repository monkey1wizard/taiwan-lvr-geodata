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
from ..normalize import normalize_address
from ..storage.parquet import BatchWriter
from ..contracts.validate import valid_coordinate
from ..storage.runs import Stage, bindings, digest
from ..sources import sha256_file
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
    selected = [
        x
        for x in descriptor["roads"]
        if not counties or x["path"].split("/")[1].split("-")[0] in counties
    ]
    for item in selected:
        path = root / item["path"]
        if sha256_file(path) != item["sha256"]:
            raise ValueError("Address road hash mismatch")
    official_metadata = (
        {k: v for k, v in official.items() if k != "path"} if official else None
    )
    parameters = {
        "descriptor": descriptor,
        "counties": sorted(counties or COUNTY_NAMES),
        "official": official_metadata,
        "normalization_version": identity.NORMALIZATION_VERSION,
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
            if not valid_coordinate(lng, lat):
                raise ValueError("coordinate bounds")
        except (TypeError, ValueError):
            lng = lat = None
            validity = "invalid_coordinate"
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
            for item in selected:
                with (root / item["path"]).open(
                    encoding="utf-8-sig", newline=""
                ) as stream:
                    reader = csv.DictReader(stream)
                    if not {"FULL_ADDR", "COUNTY", "TOWN", "ROAD", "X", "Y"}.issubset(
                        reader.fieldnames
                    ):
                        raise ValueError("Address CSV columns missing")
                    for row in reader:
                        add(
                            row["FULL_ADDR"],
                            row["COUNTY"],
                            row["TOWN"],
                            row["X"],
                            row["Y"],
                            item["path"],
                            item["sha256"],
                            reader.line_num,
                            "legacy_base",
                        )
                if sha256_file(root / item["path"]) != item["sha256"]:
                    raise ValueError("Road input changed")
    finally:
        writer.close()
    stats["dataset_counts"] = {"offline-row": writer.row_count}
    load_pinned_source(root, descriptor)
    return stage.finish(
        [("offline_rows.parquet", writer.path, "offline-row", writer.row_count)], stats
    )
