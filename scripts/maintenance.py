"""R08-2a: export the private maintenance package of a completed run and rebuild the output from it.

    python scripts/maintenance.py export --run-id <id> [--runs-root data/runs]
    python scripts/maintenance.py rebuild --package <dir> --out <dir>

Export copies the converted and tgos-state snapshots named by the run's checkpoints.json, plus three
run files, to ``<run>/maintenance/``. Rebuild uses only that package and the code: it verifies the
package, runs write_output, write_catalog and verify_output into ``<out>/output/<output_id>`` and compares
every produced file's SHA-256 with ``output.files_sha256``. The package keeps the store layout
``snapshots/<stage>/snapshots/<snapshot_id>`` so the pipeline's snapshot loaders work unmodified.

Rule 2 (same program): the current producer file hashes (pipeline sources and uv.lock) are compared with
the ``producer_files`` recorded in each packaged snapshot's quality.json ``producer_config``; that config
is bound by the snapshot manifest's ``config_sha256``. ``working_tree_source_dirty`` is a sibling key of
``producer_files`` in the config, so it is not compared; the Git commit is not compared either.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lvr_pipeline.output.build import write_output  # noqa: E402
from lvr_pipeline.output.catalog import write_catalog  # noqa: E402
from lvr_pipeline.output.verify import OutputVerificationError, verify_output  # noqa: E402
from lvr_pipeline.storage.runs import (  # noqa: E402
    DEFAULT_RUNS_ROOT,
    REBUILD_EPOCH,
    ROOT,
    SnapshotStore,
    _identifier,
    _json_write,
    _read_json,
    digest,
    load_snapshot,
    peak_rss_bytes,
    sha256_file,
)
from lvr_pipeline.storage.spill import measured_spill, peak_bytes, reset_peak  # noqa: E402

PACKAGE_SCHEMA_VERSION = "1.0"
PACKAGE_KEYS = {"schema_version", "created_at", "run_id", "rebuild_epoch", "config_sha256", "code_commit",
                "baseline", "output", "files"}
SNAPSHOT_STAGES = ("converted", "tgos-state")
BASELINE_STAGES = ("converted", "tgos-state", "output")


def producer_file_hashes() -> dict:
    """SHA-256 of the pipeline source files and uv.lock of THIS checkout, keyed relative to the repository root.

    Same file set and text normalisation as ``lvr_pipeline.storage.runs.bindings``, which has no reusable
    function for it; the tests prove the two agree.
    """
    package = ROOT / "lvr_pipeline"
    files = [*sorted(package.glob("*.py"))]
    for sub in ("addresses", "offline", "results", "tgos", "transactions", "output", "storage", "contracts"):
        files += sorted((package / sub).glob("*.py"))
    files += [*sorted((package / "contracts" / "json").glob("*.json")), ROOT / "uv.lock"]
    return {file.relative_to(ROOT).as_posix():
            hashlib.sha256(file.read_text(encoding="utf-8").encode("utf-8")).hexdigest() for file in files}


def output_id_for(converted_manifest_sha256: str, state_manifest_sha256: str) -> str:
    """The output id run_full derives from the two input manifest hashes."""
    return "output-" + digest({"converted": converted_manifest_sha256, "state": state_manifest_sha256,
                               "contract": "22-column"})[:24]


def write_output_tree(converted, state, output_root: Path, spill: Path, cutoff_yyyymm: str):
    """write_output, write_catalog and verify_output into output_root/{public,release}; spill is removed."""
    output_root = Path(output_root)
    try:
        summary = write_output(converted, state, output_root / "public", output_root / "release", spill_dir=spill)
    finally:
        shutil.rmtree(spill, ignore_errors=True)
    _, converted_report = load_snapshot(converted, "converted")
    write_catalog(output_root / "public", output_root / "release", summary,
                  converted_report=converted_report, cutoff_yyyymm=cutoff_yyyymm)
    try:
        verification = verify_output(output_root / "public", output_root / "release", spill)
    except OutputVerificationError as exc:
        _json_write(output_root / "verify_report.json", exc.report)
        raise
    finally:
        shutil.rmtree(spill, ignore_errors=True)
    return summary, verification


def _snapshot_code(directory: Path) -> tuple[dict, dict]:
    """(bindings, producer_files) of a snapshot; producer_config is checked against the bound config_sha256."""
    manifest = _read_json(directory / "manifest.json")
    config = _read_json(directory / "quality.json")["producer_config"]
    if digest(config) != manifest["bindings"]["config_sha256"]:
        raise ValueError(f"Producer configuration fingerprint mismatch in {directory.name}")
    return manifest["bindings"], config["producer_files"]


def _code_diff(recorded: dict, current: dict) -> list[str]:
    return sorted(path for path in set(recorded) | set(current) if recorded.get(path) != current.get(path))


def _safe_relative(path: str) -> PurePosixPath:
    pure = PurePosixPath(path)
    if not path or pure.is_absolute() or "\\" in path or ":" in path or any(p in {"", ".", ".."} for p in pure.parts):
        raise ValueError(f"Unsafe package path {path!r}")
    return pure


def _package_files(root: Path) -> list[dict]:
    return [{"path": item.relative_to(root).as_posix(), "size_bytes": item.stat().st_size, "sha256": sha256_file(item)}
            for item in sorted(root.rglob("*")) if item.is_file() and item.relative_to(root).as_posix() != "package.json"]


def _snapshot_dir(root: Path, stage: str, snapshot_id: str) -> Path:
    return root / "snapshots" / stage / "snapshots" / _identifier(snapshot_id)


def verify_package_files(package: Path) -> dict:
    """Load package.json and check its ``files`` against the package: no missing, extra, resized or changed file."""
    package = Path(package)
    document = _read_json(package / "package.json")
    if not isinstance(document, dict) or set(document) != PACKAGE_KEYS:
        raise ValueError("Maintenance package.json has missing or unknown fields")
    if document["schema_version"] != PACKAGE_SCHEMA_VERSION:
        raise ValueError("Unsupported maintenance package schema version")
    listed = {}
    for item in document["files"]:
        if set(item) != {"path", "size_bytes", "sha256"}:
            raise ValueError("Maintenance package file entry is malformed")
        _safe_relative(item["path"])
        if item["path"] in listed or item["path"] == "package.json":
            raise ValueError(f"Duplicate or reserved package file {item['path']}")
        listed[item["path"]] = item
    actual = {item.relative_to(package).as_posix() for item in package.rglob("*") if item.is_file()} - {"package.json"}
    if actual != set(listed):
        raise ValueError(f"Package files differ from package.json: missing {sorted(set(listed) - actual)[:5]}, "
                         f"extra {sorted(actual - set(listed))[:5]}")
    for path, item in sorted(listed.items()):
        file = package / path
        if file.stat().st_size != item["size_bytes"] or sha256_file(file) != item["sha256"]:
            raise ValueError(f"Package file {path} does not match package.json")
    return document


def _check_baseline(package: Path, document: dict) -> None:
    """The baseline entries equal the packaged checkpoints.json and the packaged snapshot manifests."""
    checkpoints = _read_json(package / "run" / "checkpoints.json")["stages"]
    baseline = document["baseline"]
    if set(baseline) != set(BASELINE_STAGES):
        raise ValueError("Package baseline must name converted, tgos-state and output")
    for stage in BASELINE_STAGES:
        entry = baseline[stage]
        if set(entry) != {"snapshot_id", "manifest_sha256"} or entry != checkpoints.get(stage):
            raise ValueError(f"Package baseline {stage} differs from the packaged checkpoints.json")
    for stage in SNAPSHOT_STAGES:
        store = package / "snapshots" / stage / "snapshots"
        found = sorted(item.name for item in store.iterdir()) if store.is_dir() else []
        if found != [baseline[stage]["snapshot_id"]]:
            raise ValueError(f"Package holds {found} for {stage}, expected {baseline[stage]['snapshot_id']}")
        manifest = _snapshot_dir(package, stage, baseline[stage]["snapshot_id"]) / "manifest.json"
        if sha256_file(manifest) != baseline[stage]["manifest_sha256"]:
            raise ValueError(f"Packaged {stage} manifest hash differs from the baseline")


def export_maintenance(run_id: str, runs_root: Path | None = None) -> dict:
    run_root = (DEFAULT_RUNS_ROOT if runs_root is None else Path(runs_root)) / _identifier(run_id)
    if not run_root.is_dir():
        raise ValueError(f"Run {run_id} does not exist under {run_root.parent}")
    target = run_root / "maintenance"
    if target.exists():
        raise FileExistsError(f"{target} already exists; a maintenance package is never overwritten")
    run_manifest = _read_json(run_root / "manifest.json")
    if run_manifest["bindings"].get("rebuild_epoch") != REBUILD_EPOCH:
        raise ValueError(f"Run predates the {REBUILD_EPOCH} rebuild")
    stages = _read_json(run_root / "checkpoints.json")["stages"]
    missing = [stage for stage in (*BASELINE_STAGES, "run-report") if stage not in stages]
    if missing:
        raise ValueError(f"Run {run_id} is not completed; checkpoints missing {missing}")
    directories = {}
    for stage in (*BASELINE_STAGES, "run-report"):
        directory = run_root / stage / "snapshots" / _identifier(stages[stage]["snapshot_id"])
        if sha256_file(directory / "manifest.json") != stages[stage]["manifest_sha256"]:
            raise ValueError(f"{stage} manifest differs from checkpoints.json")
        SnapshotStore(run_root / stage).verify_integrity(directory)
        directories[stage] = directory
    record = _read_json(directories["output"] / "output.json")
    output_id = output_id_for(stages["converted"]["manifest_sha256"], stages["tgos-state"]["manifest_sha256"])
    if record["output_id"] != output_id or stages["output"]["snapshot_id"] != output_id:
        raise ValueError("The run's output id does not follow from its converted and tgos-state manifests")
    (converted_bindings, converted_files), (state_bindings, state_files) = (
        _snapshot_code(directories[stage]) for stage in SNAPSHOT_STAGES)
    if converted_files != state_files or converted_bindings["code_commit"] != state_bindings["code_commit"]:
        raise ValueError("The converted and tgos-state snapshots were built by different code")

    staging = run_root / f".maintenance-staging-{uuid.uuid4().hex[:12]}"
    try:
        for stage in SNAPSHOT_STAGES:
            store = staging / "snapshots" / stage
            (store / "staging").mkdir(parents=True)
            shutil.copytree(directories[stage], store / "snapshots" / directories[stage].name)
        (staging / "run").mkdir()
        shutil.copyfile(run_root / "manifest.json", staging / "run" / "manifest.json")
        shutil.copyfile(run_root / "checkpoints.json", staging / "run" / "checkpoints.json")
        shutil.copyfile(directories["run-report"] / "run_report.json", staging / "run" / "run_report.json")
        document = {
            "schema_version": PACKAGE_SCHEMA_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id, "rebuild_epoch": run_manifest["bindings"]["rebuild_epoch"],
            "config_sha256": digest(converted_files), "code_commit": converted_bindings["code_commit"],
            "baseline": {stage: dict(stages[stage]) for stage in BASELINE_STAGES},
            "output": {"output_id": record["output_id"], "catalog_sha256": record["catalog_sha256"],
                       "files_sha256": record["files_sha256"]},
            "files": _package_files(staging)}
        _json_write(staging / "package.json", document)
        verify_package_files(staging)
        _check_baseline(staging, document)
        os.rename(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    files = [item for item in target.rglob("*") if item.is_file()]
    return {"path": str(target), "run_id": run_id, "output_id": output_id, "file_count": len(files),
            "total_bytes": sum(item.stat().st_size for item in files)}


def _compare(expected: dict, actual: dict) -> dict:
    same = sorted(path for path in expected if path in actual and expected[path] == actual[path])
    different = sorted(path for path in expected if path in actual and expected[path] != actual[path])
    return {"same": len(same), "different": different, "missing": sorted(set(expected) - set(actual)),
            "extra": sorted(set(actual) - set(expected))}


def rebuild_from_maintenance(package: Path, out: Path) -> dict:
    """Verify the package, rebuild the output into out/output/<output_id> and compare hashes.

    Reads only the package, ``out`` and this code. Raises before writing any output when the package or
    the code does not match; writes ``out/rebuild_report.json`` once output production starts.
    """
    package, out = Path(package).resolve(), Path(out).resolve()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise FileExistsError(f"{out} exists and is not empty")
    timings, started = {}, time.perf_counter()

    def phase(name, begin):
        timings[name] = round(time.perf_counter() - begin, 3)

    begin = time.perf_counter()
    document = verify_package_files(package)
    _check_baseline(package, document)
    if document["rebuild_epoch"] != REBUILD_EPOCH:
        raise ValueError(f"Package rebuild_epoch {document['rebuild_epoch']!r} is not {REBUILD_EPOCH!r}")
    run_manifest = _read_json(package / "run" / "manifest.json")
    if _identifier(document["run_id"]) != run_manifest["run_id"]:
        raise ValueError("Package run_id differs from the packaged run manifest")
    phase("verify_package_files", begin)
    # The package is read-only input: verify_full creates a spill directory beside the store, so every
    # snapshot is copied into a store under out/store and proven identical to package.json before use.
    out.mkdir(parents=True, exist_ok=True)
    listed = {item["path"]: item for item in document["files"]}
    begin = time.perf_counter()
    copies, copied_bytes = {}, 0
    for stage in SNAPSHOT_STAGES:
        snapshot_id = document["baseline"][stage]["snapshot_id"]
        source = _snapshot_dir(package, stage, snapshot_id)
        copies[stage] = _snapshot_dir(out / "store", stage, snapshot_id)
        (out / "store" / stage / "staging").mkdir(parents=True)
        for file in sorted(item for item in source.rglob("*") if item.is_file()):
            relative = file.relative_to(package).as_posix()
            target = copies[stage] / file.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)
            if sha256_file(target) != listed[relative]["sha256"] or target.stat().st_size != listed[relative]["size_bytes"]:
                raise ValueError(f"Copied file {relative} does not match package.json")
            copied_bytes += target.stat().st_size
    timings["copy_snapshots"] = round(time.perf_counter() - begin, 3)
    converted, state = copies["converted"], copies["tgos-state"]
    begin = time.perf_counter()
    for stage, directory in (("converted", converted), ("tgos-state", state)):
        SnapshotStore(out / "store" / stage).verify_full(directory)
    phase("verify_full", begin)
    # Rule 2: the running program's producer files equal those recorded in each packaged snapshot.
    current = producer_file_hashes()
    for stage, directory in (("converted", converted), ("tgos-state", state)):
        _, recorded = _snapshot_code(directory)
        if digest(recorded) != document["config_sha256"]:
            raise ValueError(f"Packaged {stage} snapshot producer files differ from package.json config_sha256")
        differing = _code_diff(recorded, current)
        if differing:
            raise ValueError(f"The running code differs from the package in {len(differing)} producer file(s): "
                             f"{differing}; rebuild refused")
    output_id = output_id_for(document["baseline"]["converted"]["manifest_sha256"],
                              document["baseline"]["tgos-state"]["manifest_sha256"])
    if output_id != document["output"]["output_id"]:
        raise ValueError("package.json output_id does not follow from the baseline manifests")

    import duckdb
    import pyarrow
    report = {"package": str(package), "run_id": document["run_id"], "output_id": output_id,
              "started_at": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
              "python": sys.version, "python_executable": sys.executable, "duckdb_version": duckdb.__version__,
              "pyarrow_version": pyarrow.__version__, "package_config_sha256": document["config_sha256"],
              "package_code_commit": document["code_commit"],
              "copied_snapshot_bytes": copied_bytes}
    output_root = out / "output" / output_id
    reset_peak("step")
    begin = time.perf_counter()
    error, verification = None, None
    try:
        with measured_spill(out / "spill", "rebuild") as spill:
            _, verification = write_output_tree(converted, state, output_root, spill.path / "output",
                                                run_manifest["bindings"]["cutoff_yyyymm"])
    except OutputVerificationError as exc:
        error = {"code": "OutputVerificationError", "detail": str(exc), "report": exc.report}
    except Exception as exc:
        error = {"code": type(exc).__name__, "detail": str(exc)}
    phase("write_output_tree", begin)
    begin = time.perf_counter()
    actual = {item.relative_to(out).as_posix(): sha256_file(item)
              for item in sorted((out / "output").rglob("*")) if item.is_file()} if (out / "output").exists() else {}
    comparison = _compare(document["output"]["files_sha256"], actual)
    phase("hash_output", begin)
    catalog_match = verification is not None and verification["catalog_sha256"] == document["output"]["catalog_sha256"]
    passed = (error is None and catalog_match and comparison["same"] == len(document["output"]["files_sha256"])
              and not comparison["different"] and not comparison["missing"] and not comparison["extra"])
    report.update({"pass": passed, "error": error, "catalog_sha256_match": catalog_match,
                   "comparison": comparison, "expected_file_count": len(document["output"]["files_sha256"]),
                   "timings_seconds": {**timings, "total": round(time.perf_counter() - started, 3)},
                   "process_peak_rss_bytes": peak_rss_bytes(), "spill_peak_bytes": peak_bytes("step"),
                   "finished_at": datetime.now(timezone.utc).isoformat()})
    _json_write(out / "rebuild_report.json", report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("export", help="Copy a completed run's baseline snapshots to <run>/maintenance")
    p.add_argument("--run-id", required=True)
    p.add_argument("--runs-root", type=Path, help="Run directories root (default data/runs)")
    p = sub.add_parser("rebuild", help="Rebuild the output from a maintenance package and compare hashes")
    p.add_argument("--package", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            print(json.dumps(export_maintenance(args.run_id, args.runs_root), ensure_ascii=False, indent=2))
            return 0
        report = rebuild_from_maintenance(args.package, args.out)
    except (ValueError, FileExistsError, OSError, KeyError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"pass": report["pass"], "output_id": report["output_id"], "comparison": {
        "same": report["comparison"]["same"], "different": len(report["comparison"]["different"]),
        "missing": len(report["comparison"]["missing"]), "extra": len(report["comparison"]["extra"])},
        "report": str(Path(args.out).resolve() / "rebuild_report.json")}, ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
