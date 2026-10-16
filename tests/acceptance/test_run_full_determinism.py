"""R07-12e: two run_full runs with the same code, inputs and run_id must produce the same outputs.

Synthetic inputs only. The inputs are built once (raw dir, manifest, address repo), so the address
repo commit is shared and cannot differ between the two runs. Only the runs_root differs.
"""
import hashlib
import json
import re
import sys
from pathlib import Path

import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lvr_pipeline.pipeline import run_full  # noqa: E402
from test_run_full import setup_inputs  # noqa: E402

RUN_ID = "det"
# Path components that are snapshot or output ids (for example "<stage>-<24 hex>") are replaced by "<id>".
# Snapshot and output ids embedded anywhere in a path, key or string (for example output/output-<hex>/...).
ID_SUB_RE = re.compile(r"[a-z0-9-]+-[0-9a-f]{24}")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# Timing, memory and disk figures in JSON. They depend on machine load and are not produced by the logic.
JSON_VOLATILE_KEYS = {
    "build_elapsed_seconds", "build_process_peak_rss_bytes", "elapsed_seconds", "process_peak_rss_bytes",
    "disk_free_bytes", "disk_headroom_start", "disk_headroom_end", "run_dir_bytes", "snapshot_bytes",
    # R07-12i: the sampled DuckDB spill peak varies with timing and thread scheduling.
    "build_spill_peak_bytes", "spill_peak_bytes", "spill_sample_interval_seconds",
}
# Resource preflight records sample the disk and memory of the machine when the run starts.
JSON_VOLATILE_KEY_PREFIX = "resource_preflight"
# Wall-clock timestamps such as started_at and finished_at.
JSON_VOLATILE_KEY_SUFFIX = "_at"
# Artifact entries (path + size_bytes) of JSON files: those JSON files embed timing floats, so their size varies.
JSON_ARTIFACT_SIZE_EXEMPT_SUFFIX = ".json"

# first_run_id is the review snapshot id. The id depends on timing fields of the upstream stages.
FIRST_RUN_ID_FILES = {"cross_check_addresses.parquet", "exception_addresses.parquet"}
FIRST_RUN_ID_COLUMN = "first_run_id"

# Workers for the first and second run. R07-12d will pass N for the second run.
# run_full has no worker option yet, so both runs are serial.
WORKER_PAIRS = [(1, 1)]


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _is_volatile_key(key):
    return (key in JSON_VOLATILE_KEYS or key.startswith(JSON_VOLATILE_KEY_PREFIX)
            or key.endswith(JSON_VOLATILE_KEY_SUFFIX))


def _normalize_json(value, roots):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _is_volatile_key(key):
                continue
            if key == "size_bytes" and str(value.get("path", "")).endswith(JSON_ARTIFACT_SIZE_EXEMPT_SUFFIX):
                continue
            new_key = _normalize_string(key, roots)
            if new_key in out:
                raise AssertionError(f"normalized keys collide: {key!r} -> {new_key!r}")
            out[new_key] = _normalize_json(item, roots)
        return out
    if isinstance(value, list):
        return [_normalize_json(v, roots) for v in value]
    if isinstance(value, str):
        return _normalize_string(value, roots)
    return value


def _normalize_string(value, roots):
    if SHA_RE.match(value):
        return "<sha256>"
    value = ID_SUB_RE.sub("<id>", value)
    for root in roots:
        value = value.replace(root, "<root>")
    return value


def _first_json_diff(a, b, path="$"):
    if type(a) is not type(b):
        return f"{path}: {a!r} != {b!r}"
    if isinstance(a, dict):
        if set(a) != set(b):
            return f"{path}: keys {sorted(set(a) ^ set(b))} differ"
        for key in a:
            found = _first_json_diff(a[key], b[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            found = _first_json_diff(x, y, f"{path}[{i}]")
            if found:
                return found
        return None
    return None if a == b else f"{path}: {a!r} != {b!r}"


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parquet_diff(a, b):
    ta, tb = pq.read_table(a), pq.read_table(b)
    names_a = [n for n in ta.column_names if n != FIRST_RUN_ID_COLUMN]
    names_b = [n for n in tb.column_names if n != FIRST_RUN_ID_COLUMN]
    if names_a != names_b:
        return f"columns {names_a} != {names_b}"
    rows_a = [{k: r[k] for k in names_a} for r in ta.to_pylist()]
    rows_b = [{k: r[k] for k in names_b} for r in tb.to_pylist()]
    if len(rows_a) != len(rows_b):
        return f"row count {len(rows_a)} != {len(rows_b)}"
    for i, (x, y) in enumerate(zip(rows_a, rows_b)):
        if x != y:
            column = next(k for k in names_a if x[k] != y[k])
            return f"row {i} column {column}: {x[column]!r} != {y[column]!r}"
    return None


def _collect(run_dir):
    """Map each normalized key to the files that have it. attempts/ is excluded: it holds per-attempt timing."""
    groups = {}
    for path in sorted(Path(run_dir).rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(run_dir).as_posix()
        if rel.split("/")[0] == "attempts":
            continue
        key = ID_SUB_RE.sub("<id>", rel)
        groups.setdefault(key, []).append(path)
    return groups


def _compare(key, a, b, roots):
    """Return (extension, difference or None) for one pair of files."""
    ext = a.suffix
    if ext == ".parquet":
        if a.name in FIRST_RUN_ID_FILES:
            return ext, _parquet_diff(a, b)
        return ext, None if _sha256(a) == _sha256(b) else "raw SHA-256 differs"
    if ext in {".csv", ".jsonl"}:
        return ext, None if _sha256(a) == _sha256(b) else "raw SHA-256 differs"
    if ext == ".json":
        found = _first_json_diff(_normalize_json(_read_json(a), roots), _normalize_json(_read_json(b), roots))
        return ext, found
    return ext, None if _sha256(a) == _sha256(b) else "raw SHA-256 differs"


@pytest.mark.parametrize("workers", WORKER_PAIRS, ids=["1-1"])
def test_run_full_is_deterministic(tmp_path, workers):
    inputs = setup_inputs(tmp_path)
    roots = [tmp_path / "run_a", tmp_path / "run_b"]
    for root in roots:
        result = run_full(run_id=RUN_ID, **{**inputs, "runs_root": root})
        assert result["completed"] is True and result["failures"] == [], result["failures"]

    root_strings = []
    for root in roots:
        root_strings += [str(root), root.as_posix()]
    root_strings.sort(key=len, reverse=True)
    groups_a = _collect(roots[0] / RUN_ID)
    groups_b = _collect(roots[1] / RUN_ID)
    assert groups_a and groups_b
    assert set(groups_a) == set(groups_b), (
        f"keys only in run a: {sorted(set(groups_a) - set(groups_b))}; "
        f"keys only in run b: {sorted(set(groups_b) - set(groups_a))}")

    counts = {}
    diffs = []
    for key in sorted(groups_a):
        files_a, files_b = groups_a[key], groups_b[key]
        if len(files_a) != 1 or len(files_b) != 1:
            diffs.append(f"{key}: {len(files_a)} files in run a, {len(files_b)} in run b (collision)")
            continue
        ext, found = _compare(key, files_a[0], files_b[0], root_strings)
        counts[ext] = counts.get(ext, 0) + 1
        if found:
            diffs.append(f"{key}: {found}")
    print("compared files per extension:", dict(sorted(counts.items())))
    assert not diffs, f"{len(diffs)} differing keys:\n" + "\n".join(diffs)
