"""Inventory explicitly supplied local inputs without copying or uploading rows."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lvr_pipeline.storage.runs import sha256_file
from lvr_pipeline.transactions.inventory import describe_zip


def inventory_json(value, depth=0):
    """Keep one road/member per line, rather than expanding repeated headers."""
    indent = "  " * depth
    child = "  " * (depth + 1)
    if isinstance(value, dict):
        return "{\n" + ",\n".join(child + json.dumps(key) + ": " + inventory_json(item, depth + 1)
                                   for key, item in value.items()) + "\n" + indent + "}"
    if isinstance(value, list) and value and isinstance(value[0], dict):
        rows = [inventory_json(item, depth + 1) if "members" in item else json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in value]
        return "[\n" + ",\n".join(child + row for row in rows) + "\n" + indent + "]"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--address-repo", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    raw = [describe_zip(path) for path in sorted(args.raw_dir.glob("*_lvr_landcsv.zip"))]
    if not raw:
        raise ValueError("No raw inputs found")
    commit = subprocess.check_output(["git", "-C", str(args.address_repo), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(args.address_repo), "status", "--porcelain"], text=True)
    if dirty.strip():
        raise ValueError("Address source checkout must be clean")
    roads = [{"path": path.relative_to(args.address_repo).as_posix(),
              "size_bytes": path.stat().st_size, "sha256": sha256_file(path)}
             for path in sorted((args.address_repo / "roads").glob("*.csv"))]
    if not roads:
        raise ValueError("No address roads found")
    source = {"schema_version": "1.0", "repository": "https://github.com/monkey1wizard/taiwan-address-data",
              "commit": commit, "source_kind": "legacy_base", "roads": roads,
              "road_count": len(roads), "size_bytes": sum(row["size_bytes"] for row in roads),
              "license_status": "pending_upstream_evidence",
              "license_evidence": "docs/drafts/taiwan-lvr-geodata-完整企劃.md#已確認現況",
              "rights_note": "Repository README is not proof of upstream redistribution rights."}
    source["administrative_files"] = [{"path": path.name, "size_bytes": path.stat().st_size, "sha256": sha256_file(path)}
                                     for path in sorted(args.address_repo.glob("area_*.csv"))]
    source["road_index"] = {"path": "road.csv", "sha256": sha256_file(args.address_repo / "road.csv")}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    raw_source = {"schema_version": "1.0", "raw_count": len(raw), "size_bytes": sum(row["size_bytes"] for row in raw),
                  "license_status": "legacy_documentation_only_pending_current_source_check",
                  "license_evidence": "docs/DATA_SOURCES.md", "inputs": raw}
    for filename, document in [("raw_manifest.json", raw_source), ("address_source.json", source)]:
        (args.output_dir / filename).write_text(inventory_json(document)+"\n", encoding="utf-8")
    print(f"Inventoried {len(raw)} raw ZIPs and {len(roads)} road files; no rows copied")


if __name__ == "__main__":
    main()
