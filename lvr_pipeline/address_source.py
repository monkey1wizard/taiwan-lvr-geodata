"""Create a reproducible descriptor for a clean taiwan-address-data checkout."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from .sources import sha256_file

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
