"""Local immutable snapshots with guarded parent publication.

P0 only: one local filesystem, with an exclusive publication lock. No remote
CAS or GitHub publication. A stale lock fails closed and requires operator review.
"""
from __future__ import annotations

from contextlib import contextmanager
import csv
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import uuid

from .contracts import validate_relations, validate_rows
from .sources import sha256_file


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
        os.replace(temporary, draft_path)

    def verify(self, directory: Path, *, draft: bool = False) -> dict:
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
        for item in artifacts:
            path = _artifact_path(directory, item["path"])
            if path.stat().st_size != item["size_bytes"] or sha256_file(path) != item["sha256"]:
                raise ValueError("Snapshot artifact hash/size mismatch")
            count, rows = _inspect(path, item["format"], item["schema"])
            if count != item["row_count"] or isinstance(item["row_count"], bool):
                raise ValueError("Snapshot row count mismatch")
            if item["schema"]:
                rows_by_schema.setdefault(item["schema"], []).extend(rows)
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
            os.replace(temporary, self.root / "current.json")
            return pointer

    def reusable(self, snapshot_id: str, bindings: dict) -> bool:
        _check_bindings(bindings)
        manifest = self.verify(self.root / "snapshots" / _identifier(snapshot_id))
        return manifest["bindings"] == bindings
