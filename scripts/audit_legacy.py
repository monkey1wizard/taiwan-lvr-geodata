"""Audit the R-02 legacy inventory without modifying project data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote


CURRENT_MODULES = {
    "__init__.py",
    "__main__.py",
    "0_parse_raw.py",
    "1_normalize.py",
    "address.py",
    "address_patch.py",
    "address_pool.py",
    "address_source.py",
    "address_state.py",
    "address_v2.py",
    "backfill.py",
    "cli.py",
    "contracts.py",
    "converted.py",
    "distribution.py",
    "export.py",
    "garbled.py",
    "garbled_resolve.py",
    "ingest.py",
    "normalize.py",
    "offline_lookup.py",
    "packaging.py",
    "sources.py",
    "tgos.py",
    "tx_date.py",
}

LEGACY_MODULES = {
    "0_parse_raw.py",
    "1_normalize.py",
    "2_geocode_offline.py",
    "3_prepare_tgos.py",
    "__init__.py",
    "address.py",
    "garbled.py",
    "garbled_resolve.py",
    "geocode_cache.py",
    "tmp_load_output_to_cache.py",
    "tmp_migrate_legacy_to_cache.py",
    "tmp_reconcile_overrides.py",
    "tmp_rehydrate_from_output.py",
    "tmp_rescue_output_old_to_output.py",
    "tx_date.py",
}

EXPECTED_REGISTRY = {
    "garbled_override.csv": (21, "bb9d7b97dadb228c56bf285a494b2930d8811689c7f7ecc8ca2dd5698a2e2d65"),
    "land.csv": (1_343_843, "70c1d02b15c33afd00966fdea34a19851e03198d8963fa42ce65adb86d36999a"),
    "no_doorplate.csv": (9_811, "2f7ba7907212578c19dd9e51e2091a59fcbbbf535fc8f41ea2499de8b26c89f8"),
    "parking.csv": (71_766, "61575d246332f06a09ba15b3f4a873eb7e16cab8da2fee86645cc6e0a6393cc7"),
    "unresolvable.csv": (1, "24dadc5b0188c07efc030d909674f5c1d71051a7c80d03a555e6cf98a5576820"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def csv_rows(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return max(sum(1 for _ in csv.reader(handle)) - 1, 0)


def git_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return [line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]


# New packages created by the rebuild are not part of the R-02 legacy inventory; only contracts replaced a legacy module.
LEGACY_PACKAGES = {"contracts"}
# New top-level modules created by the rebuild (not in the R-02 inventory document).
NEW_MODULES = {"pipeline.py"}


def module_names(path: Path) -> set[str]:
    # A module later converted to a package (e.g. contracts/) still counts as the same top-level module.
    names = {item.name for item in path.glob("*.py") if item.is_file() and item.name not in NEW_MODULES}
    names |= {f"{item.name}.py" for item in path.iterdir() if item.is_dir() and (item / "__init__.py").is_file() and item.name in LEGACY_PACKAGES}
    return names


def heading_anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if not match:
            continue
        heading = re.sub(r"<[^>]+>", "", match.group(1))
        heading = heading.replace("`", "").strip().casefold()
        slug = re.sub(r"[^\w\- ]", "", heading, flags=re.UNICODE)
        slug = re.sub(r"\s+", "-", slug)
        duplicate = seen.get(slug, 0)
        seen[slug] = duplicate + 1
        anchors.add(slug if duplicate == 0 else f"{slug}-{duplicate}")
    return anchors


def broken_markdown_links(root: Path) -> list[str]:
    broken: list[str] = []
    markdown_files = [root / name for name in git_files(root) if name.casefold().endswith(".md")]
    pattern = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
    for source in markdown_files:
        for raw_target in pattern.findall(source.read_text(encoding="utf-8")):
            target = raw_target.strip().strip("<>")
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            target = target.split(maxsplit=1)[0]
            path_text, _, fragment = target.partition("#")
            destination = source if not path_text else (source.parent / unquote(path_text)).resolve()
            if not destination.exists():
                broken.append(f"{source.relative_to(root)} -> {target} (missing path)")
                continue
            if fragment and destination.suffix.casefold() == ".md":
                anchor = unquote(fragment).casefold()
                if anchor not in heading_anchors(destination):
                    broken.append(f"{source.relative_to(root)} -> {target} (missing anchor)")
    return broken


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--legacy",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "taiwan-lvr-geojson",
        help="read-only legacy checkout",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    legacy = args.legacy.resolve()
    checks: list[dict[str, object]] = []

    def check(name: str, expected: object, actual: object) -> None:
        checks.append(
            {
                "name": name,
                "status": "pass" if actual == expected else "fail",
                "expected": expected,
                "actual": actual,
            }
        )

    readmes = [name for name in git_files(root) if Path(name).name.casefold() == "readme.md"]
    check("single_project_readme", ["README.md"], readmes)
    check("markdown_local_links", [], broken_markdown_links(root))
    check("current_module_inventory", sorted(CURRENT_MODULES), sorted(module_names(root / "lvr_pipeline")))
    check("legacy_module_inventory", sorted(LEGACY_MODULES), sorted(module_names(legacy / "lvr_pipeline")))

    current_rules = root / "data" / "registry" / "garbled_override.csv"
    legacy_rules = legacy / "data" / "registry" / "garbled_override.csv"
    check("current_character_rule_count", 21, csv_rows(current_rules))
    check("legacy_character_rule_count", 21, csv_rows(legacy_rules))
    check("character_rule_hash_match", sha256(legacy_rules), sha256(current_rules))

    for name, (expected_rows, expected_hash) in EXPECTED_REGISTRY.items():
        source = legacy / "data" / "registry" / name
        check(f"legacy_registry_rows:{name}", expected_rows, csv_rows(source))
        check(f"legacy_registry_sha256:{name}", expected_hash, sha256(source))

    raw_manifest = json.loads((root / "data" / "sources" / "raw_manifest.json").read_text(encoding="utf-8"))
    entries = (
        raw_manifest.get("inputs")
        or raw_manifest.get("sources")
        or raw_manifest.get("entries")
        or raw_manifest.get("files")
        or []
    )
    check("raw_manifest_source_count", 58, len(entries))

    inventory = (root / "docs" / "records" / "R02盤點.md").read_text(encoding="utf-8")
    for name in sorted(CURRENT_MODULES | LEGACY_MODULES):
        check(f"documented_module:{name}", True, f"`{name}`" in inventory)
    for name in EXPECTED_REGISTRY:
        check(f"documented_registry:{name}", True, name in inventory)

    output = {
        "root": str(root),
        "legacy": str(legacy),
        "status": "pass" if all(item["status"] == "pass" for item in checks) else "fail",
        "checks": checks,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
