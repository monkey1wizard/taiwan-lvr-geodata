"""Rebuild a TGOS child output and prove unchanged month bytes remain stable."""

from __future__ import annotations

import json
from pathlib import Path

from .packaging import package_output, verify_output


def backfill_output(
    converted: Path,
    state: Path,
    previous: Path,
    output_dir: Path,
    *,
    notices: dict,
    run_id: str,
):
    prior = verify_output(previous)
    current_path = package_output(
        converted, state, output_dir, notices=notices, run_id=run_id
    )
    current = verify_output(current_path)

    def month_map(manifest):
        return {
            (item["tx_yyyymm"], item["category"], item["format"]): item["sha256"]
            for item in manifest["assets"]
            if item["kind"] == "monthly"
        }

    before = month_map(prior)
    after = month_map(current)
    if set(before) != set(after):
        raise ValueError("Backfill changed declared transaction-month scope")
    changed = sorted(key for key in before if before[key] != after[key])
    changed_months = sorted({key[0] for key in changed})
    changed_years = sorted({month // 100 for month in changed_months})
    untouched = len(before) - len(changed)
    quality = json.loads((Path(state) / "quality.json").read_text(encoding="utf-8"))
    return current_path, {
        "schema_version": "1.0",
        "previous_snapshot_id": prior["snapshot_id"],
        "snapshot_id": current["snapshot_id"],
        "status_counts_before": quality.get("status_counts_before_import"),
        "status_counts_after": quality["status_counts"],
        "changed_months": changed_months,
        "changed_years": changed_years,
        "changed_month_artifacts": len(changed),
        "unchanged_month_artifacts": untouched,
        "previous_readable": True,
    }
