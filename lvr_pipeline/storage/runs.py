"""Immutable snapshot storage, run stages and producer bindings (formerly snapshots.py and processing.py)."""
from __future__ import annotations

from contextlib import contextmanager
import csv
import subprocess
import sys
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import time
import uuid

from ..contracts import keys_not_recomputed, validate_relations, validate_rows


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _replace(source: Path, target: Path, attempts: int = 8) -> None:
    """os.replace that waits out short Windows locks, such as a virus scan."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if os.name != "nt" or attempt == attempts - 1:
                raise
            time.sleep(0.25 * 2 ** attempt)


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise ValueError("Unsafe snapshot identifier")
    return value


def _artifact_path(base: Path, name: str) -> Path:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or any(part in {".", ".."} for part in path.parts) or "\\" in name or ":" in name:
        raise ValueError("Unsafe artifact path")
    target = base.joinpath(*path.parts)
    if not target.resolve().is_relative_to(base.resolve()):
        raise ValueError("Artifact escapes snapshot")
    if any(part.is_symlink() for part in [target, *target.parents] if part != base.parent):
        raise ValueError("Symlink artifacts are not supported")
    return target


def _json_write(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _inspect(path: Path, format_name: str, schema: str | None) -> tuple[int | None, list[dict] | None]:
    if format_name == "jsonl":
        rows = []
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    raise ValueError("Blank JSONL row")
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("JSONL rows must be objects")
                rows.append(row)
        if schema:
            validate_rows(rows, schema)
        return len(rows), rows
    if schema and format_name == "parquet":
        from .parquet import inspect_parquet
        return inspect_parquet(path, schema), None
    if schema:
        raise ValueError("P0 row schemas require JSONL")
    if format_name == "csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            if not next(reader, None):
                raise ValueError("CSV header required")
            return sum(1 for _ in reader), None
    if format_name == "parquet":
        import pyarrow.parquet as parquet
        return parquet.ParquetFile(path).metadata.num_rows, None
    if format_name == "binary":
        return None, None
    raise ValueError("Unsupported artifact format")


OLD_RULE_NOTE = "舊規則版本，未重算鍵"


def _check_bindings(bindings: dict) -> None:
    if set(bindings) != {"code_commit", "schema_version", "config_sha256", "input_sha256"}:
        raise ValueError("Incomplete snapshot bindings")
    if not re.fullmatch(r"[0-9a-f]{40}", bindings["code_commit"]):
        raise ValueError("Invalid code commit")
    if bindings["schema_version"] != "1.0":
        raise ValueError("Unsupported schema version")
    if not isinstance(bindings["input_sha256"], list) or not bindings["input_sha256"]:
        raise ValueError("Input hashes required")
    hashes = [bindings["config_sha256"], *bindings["input_sha256"]]
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes):
        raise ValueError("Invalid binding hash")


class SnapshotStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in ["staging", "snapshots"]:
            (self.root / directory).mkdir(exist_ok=True)

    @contextmanager
    def _lock(self):
        lock = self.root / ".publish-lock"
        try:
            lock.mkdir()
        except FileExistsError as exc:
            raise RuntimeError("Snapshot writer busy or stale lock; inspect before retry") from exc
        try:
            yield
        finally:
            lock.rmdir()

    def current(self) -> dict | None:
        pointer = self.root / "current.json"
        if not pointer.exists():
            return None
        value = _read_json(pointer)
        identifier = _identifier(value["snapshot_id"])
        directory = self.root / "snapshots" / identifier
        if sha256_file(directory / "manifest.json") != value["manifest_sha256"]:
            raise ValueError("Committed manifest hash mismatch")
        self.verify(directory)
        return value

    def begin(self, snapshot_id: str, *, expected_parent: str | None, bindings: dict) -> Path:
        identifier = _identifier(snapshot_id)
        if expected_parent is not None:
            _identifier(expected_parent)
        _check_bindings(bindings)
        if (self.root / "snapshots" / identifier).exists():
            raise FileExistsError("Immutable snapshot already exists")
        directory = self.root / "staging" / identifier
        directory.mkdir()
        _json_write(directory / "draft.json", {"schema_version": "1.0", "snapshot_id": identifier,
                    "parent_snapshot": expected_parent, "bindings": bindings, "artifacts": [], "complete": False})
        return directory

    def add(self, snapshot_id: str, name: str, source: Path, *, format_name: str,
            row_count: int | None, schema: str | None = None) -> None:
        directory = self.root / "staging" / _identifier(snapshot_id)
        draft_path = directory / "draft.json"
        draft = _read_json(draft_path)
        if any(item["path"] == name for item in draft["artifacts"]) or name in {"draft.json", "manifest.json"}:
            raise ValueError("Duplicate/reserved artifact name")
        if source.is_symlink():
            raise ValueError("Symlink sources are not supported")
        target = _artifact_path(directory, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as stream, target.open("xb") as output:
            shutil.copyfileobj(stream, output)
            output.flush()
            os.fsync(output.fileno())
        count, _ = _inspect(target, format_name, schema)
        if count != row_count or isinstance(row_count, bool):
            raise ValueError("Artifact row count mismatch")
        draft["artifacts"].append({"path": name, "format": format_name, "row_count": row_count,
                                   "schema": schema, "sha256": sha256_file(target), "size_bytes": target.stat().st_size})
        temporary = directory / f"draft-{uuid.uuid4().hex}.tmp"
        _json_write(temporary, draft)
        _replace(temporary, draft_path)

    @staticmethod
    def snapshot_normalization_version(directory: Path, manifest: dict) -> str | None:
        """The NORMALIZATION_VERSION the snapshot was made under, or None when it records none.

        The value is read from the producer configuration in quality.json, and only when that
        configuration hashes to the manifest's config_sha256 (so it is part of the bindings).
        """
        report_path = directory / "quality.json"
        if not report_path.is_file():
            return None
        try:
            config = _read_json(report_path).get("producer_config")
            if not isinstance(config, dict) or digest(config) != manifest["bindings"]["config_sha256"]:
                return None
            version = config.get("parameters", {}).get("normalization_version")
        except (ValueError, OSError, AttributeError):
            return None
        return version if isinstance(version, str) and version else None

    def key_rule_status(self, directory: Path) -> str:
        """'recomputed' when verify() rechecks address keys; otherwise the note for an older rule version."""
        version = self.snapshot_normalization_version(directory, _read_json(directory / "manifest.json"))
        return OLD_RULE_NOTE if self._is_old_rule(version) else "recomputed"

    @staticmethod
    def _is_old_rule(version: str | None) -> bool:
        from ..addresses import identity
        return version is not None and version != identity.NORMALIZATION_VERSION

    def verify(self, directory: Path, *, draft: bool = False) -> dict:
        manifest = _read_json(directory / ("draft.json" if draft else "manifest.json"))
        version = self.snapshot_normalization_version(directory, manifest) if manifest.get("bindings") else None
        if self._is_old_rule(version):
            # Older rule version: hashes and structure are still checked, keys are not recomputed.
            with keys_not_recomputed():
                return self._verify(directory, draft=draft)
        return self._verify(directory, draft=draft)

    def _verify(self, directory: Path, *, draft: bool = False) -> dict:
        manifest = _read_json(directory / ("draft.json" if draft else "manifest.json"))
        if manifest["complete"] is not (not draft) or manifest["schema_version"] != "1.0":
            raise ValueError("Invalid snapshot completion/schema")
        _identifier(manifest["snapshot_id"])
        if manifest["snapshot_id"] != directory.name:
            raise ValueError("Snapshot directory identity mismatch")
        _check_bindings(manifest["bindings"])
        artifacts = manifest["artifacts"]
        if not artifacts or len({item["path"] for item in artifacts}) != len(artifacts):
            raise ValueError("Missing or duplicate snapshot artifacts")
        expected_files = {item["path"] for item in artifacts} | {"draft.json", "manifest.json"}
        actual_files = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
        if actual_files - expected_files:
            raise ValueError("Unindexed or interrupted snapshot artifact")
        rows_by_schema = {}
        parquet_groups = {}
        for item in artifacts:
            path = _artifact_path(directory, item["path"])
            if path.stat().st_size != item["size_bytes"] or sha256_file(path) != item["sha256"]:
                raise ValueError("Snapshot artifact hash/size mismatch")
            count, rows = _inspect(path, item["format"], item["schema"])
            if count != item["row_count"] or isinstance(item["row_count"], bool):
                raise ValueError("Snapshot row count mismatch")
            if item["schema"] and item["format"] == "parquet":
                parquet_groups.setdefault(item["schema"], []).append(path)
            elif item["schema"]:
                rows_by_schema.setdefault(item["schema"], []).extend(rows)
        if parquet_groups:
            if rows_by_schema:
                raise ValueError("Mixed JSONL/Parquet row contracts are unsupported")
            from .parquet import verify_relations
            report_path = directory / "quality.json"
            report = _read_json(report_path) if report_path.exists() else {}
            verify_relations(parquet_groups, source_scope=report.get("source_sha256"), cutoff=report.get("cutoff"),
                             uncarried_queries=report.get("tgos_uncarried_queries"))
        for name, rows in rows_by_schema.items():
            validate_rows(rows, name)
        if "address-component" in rows_by_schema:
            if "observation" not in rows_by_schema:
                raise ValueError("Components require observations")
        if "observation" in rows_by_schema:
            validate_relations(rows_by_schema["observation"], rows_by_schema.get("address-component", []), rows_by_schema.get("exclusion", []))
        return manifest

    def publish(self, snapshot_id: str, *, expected_parent: str | None) -> dict:
        identifier = _identifier(snapshot_id)
        with self._lock():
            current = self.current()
            observed_parent = current["snapshot_id"] if current else None
            if observed_parent != expected_parent:
                raise ValueError("Stale snapshot parent")
            staged = self.root / "staging" / identifier
            final = self.root / "snapshots" / identifier
            if staged.exists():
                manifest = self.verify(staged, draft=True)
                if manifest["parent_snapshot"] != expected_parent:
                    raise ValueError("Draft parent mismatch")
                manifest["complete"] = True
                # An interrupted attempt may have written this immutable manifest.
                manifest_path = staged / "manifest.json"
                if manifest_path.exists():
                    if _read_json(manifest_path) != manifest:
                        raise ValueError("Cannot rewrite an existing manifest")
                else:
                    _json_write(manifest_path, manifest)
                if final.exists():
                    raise FileExistsError("Immutable snapshot collision")
                os.rename(staged, final)
            manifest = self.verify(final)
            if manifest["parent_snapshot"] != expected_parent:
                raise ValueError("Final parent mismatch")
            pointer = {"snapshot_id": identifier, "manifest_sha256": sha256_file(final / "manifest.json")}
            temporary = self.root / f"pointer-{uuid.uuid4().hex}.tmp"
            _json_write(temporary, pointer)
            _replace(temporary, self.root / "current.json")
            return pointer

    def reusable(self, snapshot_id: str, bindings: dict) -> bool:
        _check_bindings(bindings)
        directory = self.root / "snapshots" / _identifier(snapshot_id)
        manifest = self.verify(directory)
        if self._is_old_rule(self.snapshot_normalization_version(directory, manifest)):
            return False
        return manifest["bindings"] == bindings


RUN_BINDING_KEYS = {"code_commit", "raw_manifest_sha256", "address_source_sha256", "rules_sha256",
                    "contract_version", "key_version", "cutoff_yyyymm", "scope"}
DEFAULT_RUNS_ROOT = Path(__file__).resolve().parents[2] / "data" / "runs"


def _check_run_bindings(bindings: dict) -> dict:
    if not isinstance(bindings, dict) or set(bindings) != RUN_BINDING_KEYS:
        raise ValueError("Incomplete run bindings")
    if not isinstance(bindings["code_commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", bindings["code_commit"]):
        raise ValueError("Invalid code commit")
    for key in ["raw_manifest_sha256", "address_source_sha256", "rules_sha256"]:
        if not isinstance(bindings[key], str) or not re.fullmatch(r"[0-9a-f]{64}", bindings[key]):
            raise ValueError("Invalid binding hash")
    for key in ["contract_version", "key_version", "scope"]:
        if not isinstance(bindings[key], str) or not bindings[key]:
            raise ValueError("Invalid run binding text")
    if not isinstance(bindings["cutoff_yyyymm"], str) or not re.fullmatch(r"[0-9]{6}", bindings["cutoff_yyyymm"]):
        raise ValueError("Invalid cutoff")
    return bindings


class RunStore:
    """One run directory: an immutable manifest of bindings, one SnapshotStore per stage, and checkpoints.json."""

    def __init__(self, root: Path, run_id: str, bindings: dict):
        self.root = Path(root) / _identifier(run_id)
        self.run_id = run_id
        self.bindings = _check_run_bindings(bindings)

    @classmethod
    def open(cls, root: Path | None, run_id: str, bindings: dict) -> "RunStore":
        run = cls(DEFAULT_RUNS_ROOT if root is None else root, run_id, bindings)
        run.root.mkdir(parents=True, exist_ok=True)
        with run._lock():
            manifest = run.root / "manifest.json"
            if not manifest.exists():
                temporary = run.root / f"manifest-{uuid.uuid4().hex}.tmp"
                _json_write(temporary, {"run_id": run_id, "bindings": run.bindings})
                _replace(temporary, manifest)
            elif _read_json(manifest)["bindings"] != run.bindings:
                raise ValueError(f"Run {run_id} exists with different bindings; use a new run_id")
        return run

    @contextmanager
    def _lock(self):
        lock = self.root / ".run-lock"
        try:
            lock.mkdir()
        except FileExistsError as exc:
            raise RuntimeError("Run writer busy or stale lock; inspect before retry") from exc
        try:
            yield
        finally:
            lock.rmdir()

    def stage(self, name: str) -> SnapshotStore:
        return SnapshotStore(self.root / _identifier(name))

    def checkpoints(self) -> dict:
        path = self.root / "checkpoints.json"
        return _read_json(path)["stages"] if path.exists() else {}

    def commit(self, stage: str, snapshot_id: str) -> dict:
        store = self.stage(stage)
        directory = store.root / "snapshots" / _identifier(snapshot_id)
        with self._lock():
            manifest = store.verify(directory)
            if manifest["snapshot_id"] != snapshot_id:
                raise ValueError("Snapshot identity mismatch")
            entry = {"snapshot_id": snapshot_id, "manifest_sha256": sha256_file(directory / "manifest.json")}
            stages = {**self.checkpoints(), stage: entry}
            temporary = self.root / f"checkpoints-{uuid.uuid4().hex}.tmp"
            _json_write(temporary, {"run_id": self.run_id, "stages": stages})
            _replace(temporary, self.root / "checkpoints.json")
            return entry

    def reusable(self, stage: str, bindings: dict) -> bool:
        """True only if the stage is checkpointed, the bindings equal the run's, and the files re-hash cleanly."""
        try:
            if _check_run_bindings(bindings) != self.bindings:
                return False
            entry = self.checkpoints().get(_identifier(stage))
            if entry is None:
                return False
            store = self.stage(stage)
            directory = store.root / "snapshots" / _identifier(entry["snapshot_id"])
            if sha256_file(directory / "manifest.json") != entry["manifest_sha256"]:
                return False
            store.verify(directory)
            return True
        except (ValueError, OSError, KeyError):
            return False


ROOT = Path(__file__).resolve().parents[2]
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
    files = [*sorted((ROOT / "lvr_pipeline").glob("*.py")), *sorted((ROOT / "lvr_pipeline" / "addresses").glob("*.py")), *sorted((ROOT / "lvr_pipeline" / "offline").glob("*.py")), *sorted((ROOT / "lvr_pipeline" / "results").glob("*.py")), *sorted((ROOT / "lvr_pipeline" / "transactions").glob("*.py")), *sorted((ROOT / "lvr_pipeline" / "output").glob("*.py")), *sorted((ROOT / "lvr_pipeline" / "storage").glob("*.py")), *sorted((ROOT / "lvr_pipeline" / "contracts").glob("*.py")), *sorted((ROOT / "lvr_pipeline" / "contracts" / "json").glob("*.json")), ROOT / "uv.lock"]
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
