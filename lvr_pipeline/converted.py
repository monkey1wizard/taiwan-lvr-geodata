"""Publish verified P1 typed checkpoints; no geocoding or consumer GIS output."""
from __future__ import annotations

from pathlib import Path

from .storage.parquet import verify_relations
from .storage.runs import Stage, bindings, load_snapshot
from .sources import sha256_file


def export_converted(normalized_snapshot: Path, work_dir: Path, *, run_id=None, code_commit=None) -> Path:
    normalized_snapshot = Path(normalized_snapshot)
    manifest, previous = load_snapshot(normalized_snapshot, "normalize")
    binding = bindings("converted", {}, [sha256_file(normalized_snapshot / "manifest.json")], code_commit)
    stage = Stage(Path(work_dir) / "converted", "converted", binding, run_id)
    if stage.reused:
        return stage.path
    artifacts, groups = [], {}
    for item in manifest["artifacts"]:
        if item["format"] == "parquet":
            path = normalized_snapshot / item["path"]
            groups.setdefault(item["schema"], []).append(path)
            artifacts.append((item["path"], path, item["schema"], item["row_count"]))
    counts = verify_relations(groups)
    if counts.get("disposition") != previous["input_rows"] or counts.get("observation") != previous["retained_rows"]:
        raise ValueError("Converted counts do not match declared scope")
    report = {**previous, "input_snapshot_sha256": binding["input_sha256"][0], "dataset_counts": counts,
              "record_grain": "source_observation", "tgos_started": False, "geocoded": False,
              "consumer_output": False}
    return stage.finish(artifacts, report)
