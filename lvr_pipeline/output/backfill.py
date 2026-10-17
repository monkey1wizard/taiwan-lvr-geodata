"""Backfill proof (R10-1b, d11): compare two catalogs against the affected-months list of the final state.

Every output is rebuilt in full. The proof is that the files outside the affected list are the same
(``sha256``, ``rows`` and ``size_bytes``) and that the listed monthly CSV files grew by exactly the
added minus the removed rows the list records.
"""

from __future__ import annotations

from .catalog import CATALOG_NAME, PARQUET_NAME


def _entries(catalog: dict) -> dict[str, dict]:
    entries = {}
    for entry in catalog["files"]:
        if entry["path"] in entries:
            raise ValueError(f"Catalog lists {entry['path']} twice")
        entries[entry["path"]] = entry
    return entries


def compare_catalogs(old_catalog: dict, new_catalog: dict, affected: dict) -> dict:
    """Compare the file entries of two catalogs with the affected list; see the module docstring.

    ``affected`` is the content of ``affected_months.json``: ``files`` lists the paths that may change
    and ``months`` the added, updated and removed door counts per output file. Returns
    ``{"ok", "errors", "warnings", "info", "checked"}``; ``info`` lists the differences of the
    catalog-wide fields other than ``files`` (and ``total_rows``, which the file rows explain).
    """
    old, new = _entries(old_catalog), _entries(new_catalog)
    listed = set(affected["files"])
    net = {item["csv_path"]: item["added"] - item["removed"] for item in affected["months"]}
    errors, warnings, info = [], [], []
    for path in sorted(listed - old.keys() - new.keys() - {CATALOG_NAME}):
        errors.append(f"{path}: listed as affected but in neither catalog")
    checked = 0
    for path in sorted(old.keys() | new.keys()):
        checked += 1
        before, after = old.get(path), new.get(path)
        if path not in listed:
            if before is None or after is None:
                errors.append(f"{path}: {'added' if before is None else 'removed'} but not in the affected list")
                continue
            for field in ("sha256", "rows", "size_bytes"):
                if before[field] != after[field]:
                    errors.append(f"{path}: {field} changed ({before[field]} to {after[field]}) outside the affected list")
            continue
        if after is None:
            errors.append(f"{path}: listed as affected but missing from the new catalog")
            continue
        old_rows = before["rows"] if before is not None else 0
        if path in net and after["rows"] - old_rows != net[path]:
            errors.append(f"{path}: rows grew by {after['rows'] - old_rows}, the list records {net[path]}")
        if path == PARQUET_NAME and after["rows"] - old_rows != sum(net.values()):
            errors.append(f"{path}: rows grew by {after['rows'] - old_rows}, the list records {sum(net.values())}")
        if before is not None and before["sha256"] == after["sha256"]:
            warnings.append(f"{path}: listed as affected but its sha256 did not change")
    for name in sorted((old_catalog.keys() | new_catalog.keys()) - {"files", "total_rows"}):
        if old_catalog.get(name) != new_catalog.get(name):
            info.append(f"{name}: {old_catalog.get(name)!r} to {new_catalog.get(name)!r}")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "info": info, "checked": checked}
