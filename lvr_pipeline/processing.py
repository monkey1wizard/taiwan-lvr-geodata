"""Common binding, measurement and publication helpers for local P1 stages."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .sources import sha256_file
from .snapshots import SnapshotStore, _identifier

ROOT = Path(__file__).resolve().parents[1]
PRODUCER_CONFIGS = {}


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def peak_rss_bytes() -> int:
    if os.name == "nt":
        import ctypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
                (name, ctypes.c_size_t) for name in ["peak", "working", "paged_peak", "paged", "nonpaged_peak", "nonpaged", "pagefile", "pagefile_peak"]]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        ctypes.windll.kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if not ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(handle), ctypes.byref(counters), counters.cb):
            raise OSError("Cannot measure process memory")
        return counters.peak
    import resource
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(rss if sys.platform == "darwin" else rss * 1024)


def bindings(stage: str, parameters: dict, input_hashes: list[str], code_commit: str | None = None) -> dict:
    if code_commit is None:
        code_commit = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    files = [*sorted((ROOT / "lvr_pipeline").glob("*.py")), *sorted((ROOT / "lvr_pipeline" / "contracts").glob("*.py")), *sorted((ROOT / "lvr_pipeline" / "contracts" / "json").glob("*.json")), ROOT / "uv.lock"]
    # Text normalization makes source fingerprints portable across CRLF/LF checkouts.
    code = {file.relative_to(ROOT).as_posix(): hashlib.sha256(file.read_text(encoding="utf-8").encode("utf-8")).hexdigest() for file in files}
    dirty = bool(subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain", "--", "lvr_pipeline", "uv.lock"], text=True).strip())
    config = {"stage": stage, "parameters": parameters, "producer_files": code, "working_tree_source_dirty": dirty}
    fingerprint = digest(config)
    PRODUCER_CONFIGS[fingerprint] = config
    return {"code_commit": code_commit, "schema_version": "1.0", "config_sha256": fingerprint, "input_sha256": input_hashes}


class Stage:
    def __init__(self, root: Path, kind: str, binding: dict, run_id: str | None = None):
        self.start = time.perf_counter()
        self.store = SnapshotStore(Path(root))
        self.kind, self.binding = kind, binding
        self.id = _identifier(run_id or f"{kind}-{digest(binding)[:24]}")
        self.path = self.store.root / "snapshots" / self.id
        self.reused = self.path.exists()
        if self.reused:
            if not self.store.reusable(self.id, binding):
                raise ValueError("Existing snapshot has different bindings")
            return
        current = self.store.current()
        self.parent = current["snapshot_id"] if current else None
        self.store.begin(self.id, expected_parent=self.parent, bindings=binding)
        self.build = self.store.root / "build" / self.id
        self.build.mkdir(parents=True)

    def finish(self, artifacts: list[tuple[str, Path, str, int]], report: dict):
        if self.reused:
            return self.path
        report = {**report, "stage": self.kind, "internal_only": True,
                  "producer_config": PRODUCER_CONFIGS[self.binding["config_sha256"]],
                  "build_elapsed_seconds": round(time.perf_counter() - self.start, 6),
                  "build_process_peak_rss_bytes": peak_rss_bytes(),
                  "artifact_bytes": sum(path.stat().st_size for _, path, _, _ in artifacts),
                  "validation_duckdb_memory_limit": os.environ.get("LVR_DUCKDB_MEMORY_LIMIT", "256MB"),
                  "validation_duckdb_temp_limit": os.environ.get("LVR_DUCKDB_TEMP_LIMIT", "1GiB")}
        report_path = self.build / "quality.json"
        report_path.write_text(canonical_json(report) + "\n", encoding="utf-8")
        for name, path, dataset, count in artifacts:
            self.store.add(self.id, name, path, format_name="parquet", row_count=count, schema=dataset)
        self.store.add(self.id, "quality.json", report_path, format_name="binary", row_count=None)
        self.store.publish(self.id, expected_parent=self.parent)
        return self.path


def load_snapshot(path: Path, expected_stage: str) -> tuple[dict, dict]:
    path = Path(path).resolve()
    manifest = SnapshotStore(path.parent.parent).verify(path)
    report = json.loads((path / "quality.json").read_text(encoding="utf-8"))
    if report["stage"] != expected_stage or report["internal_only"] is not True:
        raise ValueError("Incorrect processing stage")
    if digest(report["producer_config"]) != manifest["bindings"]["config_sha256"]:
        raise ValueError("Producer configuration fingerprint mismatch")
    counts = {}
    for item in manifest["artifacts"]:
        if item["schema"]:
            counts[item["schema"]] = counts.get(item["schema"], 0) + item["row_count"]
    if expected_stage == "ingest":
        if counts.get("ingest-record") != report["input_rows"]:
            raise ValueError("Ingest report count differs from artifacts")
    elif expected_stage == "address-patch":
        for dataset, counter in [
            ("address-patch", "patch_rows"),
            ("address-patch-provenance", "provenance_rows"),
            ("address-patch-quarantine", "quarantine_rows"),
        ]:
            if counts.get(dataset) != report[counter]:
                raise ValueError("Address patch report count differs from artifacts")
    else:
        for dataset, counter in [("observation", "retained_rows"), ("exclusion", "excluded_rows"),
                                 ("address-component", "component_rows"), ("diagnostic", "diagnostic_rows"), ("disposition", "input_rows")]:
            if counts.get(dataset) != report[counter]:
                raise ValueError("Processing report count differs from artifacts")
        if report["retained_rows"] + report["excluded_rows"] + report["failed_rows"] != report["input_rows"]:
            raise ValueError("Processing disposition counts inconsistent")
        if expected_stage == "converted" and counts != report["dataset_counts"]:
            raise ValueError("Converted dataset counts inconsistent")
    return manifest, report
