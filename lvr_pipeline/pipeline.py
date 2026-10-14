"""Command dispatch and the ingest, normalize and convert chain."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .addresses.pool import build_pool
from .offline.index import audit_address_source, check_pinned_source, pin_address_source, build_index
from .addresses.admin import admin_towns_failures, admin_towns_sha256, write_admin_towns
from .addresses.address_fixes import load_address_char_fixes
from .addresses.identity import NORMALIZATION_VERSION
from .contracts import SCHEMA_VERSION
from .results.reconcile import DEFAULT_CONFIG, load_coordinate_tolerance, load_suspended_counties, resolve_offline, load_p2
from .results.review import build_review
from .output.build import write_output
from .output.catalog import write_catalog
from .output.verify import OutputVerificationError, verify_output
from .results.address_patch import export_address_patch, verify_address_patch
from .tgos import (
    import_tgos,
    load_state,
    prepare_tgos,
    repair_prepared_exchange,
    revoke_alias,
    tgos_log_date,
    transition_batch,
)
from .converted import export_converted
from .transactions.inventory import verify_inventory, write_raw_manifest
from .transactions.read import ingest
from .transactions.normalize import normalize
from .storage.runs import (
    REBUILD_EPOCH,
    ROOT,
    RunStore,
    DEFAULT_RUNS_ROOT,
    SnapshotStore,
    _json_write,
    bindings as stage_bindings,
    digest,
    load_snapshot,
    peak_rss_bytes,
    rebuild_source_failures,
    sha256_file,
)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _char_fixes(args):
    """The address character fixes of the pinned address source named by --address-dir and --address-source."""
    if args.address_dir is None:
        raise ValueError("--address-dir is required to normalize addresses")
    return load_address_char_fixes(args.address_dir, _read(args.address_source))


def _run_local_stage(args):
    start = time.perf_counter()
    if args.command == "verify-converted":
        _, report = load_snapshot(args.input, "converted")
        print(json.dumps({"snapshot": str(args.input), "verified": True, "dataset_counts": report["dataset_counts"]}, ensure_ascii=False))
        return 0
    if args.command == "ingest":
        manifest = _read(args.manifest)
        path = ingest(args.raw_dir, manifest, args.batch, args.work_dir, batch_rows=args.batch_rows, run_id=args.run_id)
    elif args.command == "normalize":
        path = normalize(args.input, args.work_dir, cutoff=args.cutoff, rules_path=args.garbled_rules,
                         char_fixes=_char_fixes(args), batch_rows=args.batch_rows, run_id=args.run_id)
    else:
        normalized = args.input
        if normalized is not None:
            _, existing = load_snapshot(normalized, "normalize")
            if existing["cutoff"] != args.cutoff:
                raise ValueError("Cutoff differs from normalized snapshot; rerun normalize to change it")
            if args.batch:
                raise ValueError("Cannot reselect batches from a normalized snapshot")
        if normalized is None:
            manifest = _read(args.manifest)
            raw = ingest(args.raw_dir, manifest, args.batch, args.work_dir, batch_rows=args.batch_rows,
                         run_id=args.run_id + "-ingest" if args.run_id else None)
            normalized = normalize(raw, args.work_dir, cutoff=args.cutoff, rules_path=args.garbled_rules,
                                   char_fixes=_char_fixes(args), batch_rows=args.batch_rows,
                                   run_id=args.run_id + "-normalize" if args.run_id else None)
        path = export_converted(normalized, args.work_dir, run_id=args.run_id)
    print(json.dumps({"snapshot": str(path), "internal_only": True,
                      "elapsed_seconds": round(time.perf_counter() - start, 6), "process_peak_rss_bytes": peak_rss_bytes()}, ensure_ascii=False))
    return 0


def run(args):
    if args.command in {"ingest", "normalize", "export-converted", "verify-converted"}:
        return _run_local_stage(args)
    if args.command == "run-full":
        return _run_full_command(args)
    return _run_offline_and_output(args)


def _run_offline_and_output(args):
    read = _read
    command = args.command
    if command == "verify-sources":
        start = time.perf_counter()
        report = verify_inventory(args.raw_dir, read(args.manifest))
        report["elapsed_seconds"] = round(time.perf_counter() - start, 3)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["verified"] else 1
    if command == "inventory-sources":
        document = write_raw_manifest(args.raw_dir, args.output)
        print(json.dumps({"output": str(args.output), "raw_count": document["raw_count"]}))
        return 0
    if command == "build-admin-towns":
        document = write_admin_towns(args.address_dir, read(args.address_source), args.output)
        print(json.dumps({"output": str(args.output), "source_commit": document["source_commit"],
                          "counties": len(document["counties"]),
                          "towns": sum(len(t) for t in document["counties"].values())}))
        return 0
    if command == "pin-address-source":
        descriptor = pin_address_source(args.address_dir, args.output)
        print(json.dumps({"output": str(args.output), "commit": descriptor["commit"], "road_count": descriptor["road_count"]}))
        return 0
    elif command == "build-offline-index":
        official = read(args.official_source) if args.official_source else None
        if bool(official) != bool(args.official_file):
            raise ValueError(
                "Official source descriptor and CSV must be provided together"
            )
        if official:
            official = {**official, "path": str(args.official_file)}
        path = build_index(
            args.address_dir,
            read(args.address_source),
            args.work_dir,
            counties=args.county,
            official=official,
            run_id=args.run_id,
        )
    elif command == "audit-address-source":
        start = time.perf_counter()
        summary = audit_address_source(
            args.address_dir,
            read(args.address_source),
            args.output_dir,
            counties=args.county,
        )
        print(
            json.dumps(
                {
                    "output_dir": str(args.output_dir),
                    "rows": summary["rows"],
                    "categories": summary["categories"],
                    "elapsed_seconds": round(time.perf_counter() - start, 3),
                    "process_peak_rss_bytes": peak_rss_bytes(),
                },
                ensure_ascii=False,
            )
        )
        return 0
    elif command == "build-address-pool":
        path = build_pool(
            args.input,
            args.address_dir,
            read(args.address_source),
            args.work_dir,
            index_path=args.index,
            run_id=args.run_id,
        )
    elif command == "resolve-offline":
        path = resolve_offline(
            args.pool,
            args.index,
            args.work_dir,
            prior_state=args.prior_state,
            coordinate_tolerance_m=load_coordinate_tolerance(args.config),
            suspended_counties=load_suspended_counties(args.config),
            run_id=args.run_id,
        )
    elif command == "verify-offline-state":
        _, report = load_p2(args.input, "offline-state")
        print(json.dumps({"verified": True, "status_counts": report["status_counts"]}))
        return 0
    elif command == "prepare-tgos":
        log_date = tgos_log_date(args.date_file)
        path, exchange = prepare_tgos(
            args.state,
            args.work_dir,
            args.exchange_dir,
            limit=args.limit,
            retry_fingerprints=args.retry_query_fingerprint,
            retry_reason=args.retry_reason,
            ledger=args.ledger,
            exchange_date=log_date,
            run_id=args.run_id,
        )
        print(json.dumps({"path": str(path), "exchange": str(exchange), "completed": True}, ensure_ascii=False))
        return 0
    elif command == "set-tgos-status":
        path = transition_batch(
            args.state,
            args.work_dir,
            args.batch,
            args.status,
            reason=args.reason,
            run_id=args.run_id,
        )
    elif command == "repair-tgos-exchange":
        log_date = tgos_log_date(args.date_file)
        path, exchange = repair_prepared_exchange(
            args.state,
            args.work_dir,
            args.exchange_dir,
            args.batch,
            exchange_date=log_date,
            run_id=args.run_id,
        )
        print(
            json.dumps(
                {"path": str(path), "exchange": str(exchange), "completed": True},
                ensure_ascii=False,
            )
        )
        return 0
    elif command == "import-tgos":
        path = import_tgos(
            args.state,
            args.work_dir,
            args.batch,
            args.response,
            run_id=args.run_id,
        )
    elif command == "revoke-alias":
        path = revoke_alias(
            args.state,
            args.work_dir,
            args.alias_key,
            reason=args.reason,
            run_id=args.run_id,
        )
    elif command == "verify-tgos-state":
        _, report, stage = load_state(args.input)
        if stage != "tgos-state":
            raise ValueError("Expected TGOS state")
        print(json.dumps({"verified": True, "status_counts": report["status_counts"], "tgos_batch_count": report["tgos_batch_count"]}))
        return 0
    elif command == "build-review":
        start = time.perf_counter()
        path = build_review(
            args.input,
            args.state,
            args.work_dir,
            descriptor=read(args.address_source),
            tgos_state=args.tgos_state,
            prior_review=args.prior_review,
            run_id=args.run_id,
        )
        report = read(Path(path) / "quality.json")
        print(
            json.dumps(
                {
                    "path": str(path),
                    "completed": True,
                    "reason_counts": report["reason_counts"],
                    "reason_distinct_addresses": report["reason_distinct_addresses"],
                    "csv_bytes": report["csv_bytes"],
                    "elapsed_seconds": round(time.perf_counter() - start, 3),
                    "process_peak_rss_bytes": peak_rss_bytes(),
                },
                ensure_ascii=False,
            )
        )
        return 0
    elif command == "export-address-patch":
        path = export_address_patch(args.state, args.area_file, args.work_dir, run_id=args.run_id)
    elif command == "verify-address-patch":
        report = verify_address_patch(args.input)
        print(json.dumps({"verified": True, "snapshot_id": report["snapshot_id"], "patch_rows": report["patch_rows"], "quarantine_rows": report["quarantine_rows"]}, ensure_ascii=False))
        return 0
    else:
        raise ValueError("Unknown P2 command")
    print(json.dumps({"path": str(path), "completed": True}, ensure_ascii=False))
    return 0


# R06-4: one full offline run over every batch of the raw manifest.
RUN_FULL_STAGES = ["offline-index", "ingested", "normalized", "converted", "address-pool",
                   "offline-state", "review", "output", "run-report"]
RUN_FULL_SCOPE = "all-manifest-batches"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def quarantine_residue(run_root: Path) -> list[dict]:
    """Move interrupted stage drafts aside (owner decision 2026-10-09, option A).

    ``<stage>/staging/<id>`` is always residue, because publishing renames a draft to
    ``snapshots/<id>``. ``<stage>/build/<id>`` is residue only when ``snapshots/<id>`` is
    absent; a finished Stage keeps its build directory. Residue is renamed into
    ``<stage>/quarantine/<id>-<UTC time>/``. Nothing is deleted, read or reused.
    """
    moved = []
    for stage_dir in sorted(p for p in Path(run_root).iterdir() if p.is_dir() and not p.name.startswith(".")):
        residue: dict[str, list[tuple[str, Path]]] = {}
        if (stage_dir / "staging").is_dir():
            for item in sorted((stage_dir / "staging").iterdir()):
                residue.setdefault(item.name, []).append(("staging", item))
        if (stage_dir / "build").is_dir():
            for item in sorted((stage_dir / "build").iterdir()):
                if not (stage_dir / "snapshots" / item.name).exists():
                    residue.setdefault(item.name, []).append(("build", item))
        for identifier, items in sorted(residue.items()):
            target = stage_dir / "quarantine" / f"{identifier}-{_utc_stamp()}"
            target.mkdir(parents=True)
            for kind, source in items:
                os.rename(source, target / kind)
                moved.append({"stage": stage_dir.name, "snapshot_id": identifier, "kind": kind,
                              "from": source.relative_to(run_root).as_posix(),
                              "to": (target / kind).relative_to(run_root).as_posix()})
    return moved


def run_bindings(manifest_path: Path, address_source: Path, rules_path: Path, config: Path, cutoff: int,
                 char_fixes_sha256: str) -> dict:
    """The RunStore bindings of a full run; any change needs a new run id."""
    commit = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    return {"code_commit": commit, "raw_manifest_sha256": sha256_file(Path(manifest_path)),
            "address_source_sha256": sha256_file(Path(address_source)),
            "admin_towns_sha256": admin_towns_sha256(),
            "char_fixes_sha256": char_fixes_sha256,
            # The pipeline config holds the 30 m tolerance and the suspended counties.
            "rules_sha256": digest({"character_fixes": sha256_file(Path(rules_path)),
                                    "pipeline_config": sha256_file(Path(config))}),
            "contract_version": SCHEMA_VERSION, "key_version": f"building_key/{NORMALIZATION_VERSION}",
            "cutoff_yyyymm": f"{int(cutoff):06d}", "scope": RUN_FULL_SCOPE, "rebuild_epoch": REBUILD_EPOCH}


def coverage_failures(manifest: dict, report: dict) -> list[dict]:
    """Every manifest batch is present, known-empty batches match, every member balances."""
    failures = []
    expected = sorted(entry["batch"] for entry in manifest["inputs"])
    found = sorted(report["batches"])
    if found != expected:
        failures.append({"code": "batch_coverage", "detail": f"expected {expected}, found {found}"})
    known = sorted(entry["batch"] for entry in manifest["inputs"] if entry.get("known_empty") is True)
    if sorted(report["known_empty_batches"]) != known:
        failures.append({"code": "known_empty_mismatch",
                         "detail": f"expected {known}, found {sorted(report['known_empty_batches'])}"})
    members = report.get("member_dispositions", [])
    for batch in sorted(set(expected) - set(known) - {item["batch"] for item in members}):
        failures.append({"code": "batch_without_members", "detail": batch})
    for item in members:
        if item.get("equation") != "holds":
            failures.append({"code": "member_accounting", "detail": f"{item['batch']}/{item['path']}: {item.get('equation')}"})
    return failures


# Required free share of the disk. Kept here, not in config/pipeline.example.toml: that file is hashed into run bindings.
DISK_HEADROOM_RATIO = 0.20


class AddressSourceChanged(RuntimeError):
    """R07-12a: the address checkout is dirty or a pinned data file changed."""


class DiskHeadroomError(RuntimeError):
    """Free disk space is below the required share; the stage was not started."""


def disk_headroom(path) -> dict:
    """Free space on the disk holding ``path`` (nearest existing ancestor) against DISK_HEADROOM_RATIO."""
    probe = Path(path).absolute()
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    ratio = usage.free / usage.total if usage.total else 0.0
    return {"path": str(probe), "free_bytes": usage.free, "total_bytes": usage.total, "free_ratio": ratio,
            "required_ratio": DISK_HEADROOM_RATIO, "ok": ratio >= DISK_HEADROOM_RATIO}


def _tree_bytes(path) -> int:
    root = Path(path)
    if root.is_file():
        return root.stat().st_size
    return sum(item.stat().st_size for item in root.rglob("*") if item.is_file())


def run_full(*, run_id: str, cutoff: int, address_dir: Path, runs_root: Path | None = None,
             raw_dir: Path = Path("data/raw"), manifest_path: Path = Path("config/sources/raw_manifest.json"),
             address_source: Path = Path("config/sources/address_source.json"),
             rules_path: Path = Path("config/rules/character-fixes.csv"), config: Path | None = None,
             batch_rows: int = 1024) -> dict:
    """verify-sources, offline index, ingest, normalize, converted, coverage, address pool,
    offline state and review, each stage committed through RunStore.

    The result is complete only when every step passed; only then is the ``run-report``
    stage written, and that checkpoint is the run's completion state.
    """
    config = Path(config) if config is not None else DEFAULT_CONFIG
    clock = time.perf_counter()
    # R07-6a: both source inventories must be built for this rebuild and verified by the owner.
    # Checked before anything is written: no run directory, lock or attempts file.
    source_failures = [{"step": "rebuild-sources", **failure}
                       for path, kind in [(manifest_path, "raw manifest"), (address_source, "address source")]
                       for failure in rebuild_source_failures(_read(path), kind)]
    # R07-6b: the town table must come from the pinned address source of this run.
    # (Judged only for an address source of this rebuild; an older one is already refused above.)
    if not source_failures:
        source_failures += [{"step": "admin-towns", **failure} for failure in admin_towns_failures(_read(address_source))]
        # R07-6d: the pinned source must name char_fixes.csv, or addresses would be normalized without it.
        if not isinstance(_read(address_source).get("char_fixes"), dict):
            source_failures.append({"step": "address-source", "code": "address_source_missing_char_fixes",
                                    "detail": "the address source has no char_fixes entry; run pin-address-source"})
    if source_failures:
        return {"run_id": run_id, "completed": False, "started_at": datetime.now(timezone.utc).isoformat(),
                "quarantined": [], "steps": [], "failures": source_failures}
    char_fixes = load_address_char_fixes(address_dir, _read(address_source))
    bindings_ = run_bindings(manifest_path, address_source, rules_path, config, cutoff, char_fixes.sha256)
    headroom_start = disk_headroom(DEFAULT_RUNS_ROOT if runs_root is None else runs_root)
    if not headroom_start["ok"]:  # stop before anything is written: no run directory, lock or attempts file
        return {"run_id": run_id, "completed": False, "started_at": datetime.now(timezone.utc).isoformat(),
                "bindings": bindings_, "quarantined": [], "steps": [], "disk_headroom": headroom_start,
                "failures": [{"step": "disk-headroom", "code": "disk_headroom",
                              "detail": f"free {headroom_start['free_ratio']:.4f} of disk, "
                                        f"required {DISK_HEADROOM_RATIO:.2f}"}]}
    run = RunStore.open(runs_root, run_id, bindings_)
    result = {"run_id": run_id, "run_dir": str(run.root), "completed": False,
              "started_at": datetime.now(timezone.utc).isoformat(), "bindings": bindings_,
              "quarantined": [], "steps": [], "failures": [], "address_source_head": []}
    current = {"step": None}
    full_check_head = {"head": None}

    def check_source(name):
        """R07-12a: before each stage the address checkout must be clean and keep its pinned data files.

        The first entry (and any entry whose HEAD differs from the last full check) hashes every
        pinned file; other entries only check HEAD and clean status. A HEAD other than the pinned
        commit passes and is recorded.
        """
        try:
            full = full_check_head["head"] is None
            if not full:
                full = check_pinned_source(address_dir, descriptor, hashes=False)["actual_head"] != full_check_head["head"]
            checked = check_pinned_source(address_dir, descriptor, hashes=full)
        except (ValueError, subprocess.CalledProcessError, OSError) as exc:
            raise AddressSourceChanged(str(exc)) from exc
        if full:
            full_check_head["head"] = checked["actual_head"]
        result["address_source_head"].append({
            "step": name, "actual_head": checked["actual_head"], "head_matches": checked["head_matches"],
            "full_check": full, "checked_at": checked["checked_at"]})

    def stage(name, build):
        current["step"] = name
        check_source(name)
        start = time.perf_counter()
        reused = run.reusable(name, bindings_)
        if reused:
            path = run.stage(name).root / "snapshots" / run.checkpoints()[name]["snapshot_id"]
        else:
            headroom = disk_headroom(run.root)
            if not headroom["ok"]:
                raise DiskHeadroomError(f"free {headroom['free_ratio']:.4f} of disk before {name}, "
                                        f"required {DISK_HEADROOM_RATIO:.2f}")
            path = Path(build())
            run.commit(name, path.name)
        result["steps"].append({"step": name, "snapshot_id": path.name, "reused_checkpoint": reused,
                                "elapsed_seconds": round(time.perf_counter() - start, 3),
                                # Process peak so far (ru_maxrss is monotonic), not a per-stage peak.
                                "process_peak_rss_bytes": peak_rss_bytes(),
                                "snapshot_bytes": _tree_bytes(path),
                                "disk_free_bytes": shutil.disk_usage(run.root).free})
        return path

    def resources():
        return {"disk_headroom_start": headroom_start, "disk_headroom_end": disk_headroom(run.root),
                "run_dir_bytes": _tree_bytes(run.root),
                "duckdb_memory_limit": os.environ.get("LVR_DUCKDB_MEMORY_LIMIT"),
                "steps": [{k: step[k] for k in ["step", "elapsed_seconds", "process_peak_rss_bytes", "snapshot_bytes"]}
                          for step in result["steps"] if "snapshot_bytes" in step]}

    def output_stage(converted, state):
        """R07-8b: write_output into <run>/output/<output_id>/{public,release}, committed as one snapshot.

        Reused only when the checkpoint verifies, the snapshot id equals the id derived from the
        inputs, and every recorded file still has its recorded sha256. An existing output
        directory is never deleted, adopted or overwritten.
        """
        current["step"] = "output"
        check_source("output")
        start = time.perf_counter()
        input_hashes = [sha256_file(Path(converted) / "manifest.json"), sha256_file(Path(state) / "manifest.json")]
        output_id = "output-" + digest({"converted": input_hashes[0], "state": input_hashes[1],
                                        "contract": "22-column"})[:24]
        store = run.stage("output")
        final = store.root / "snapshots" / output_id
        output_root = store.root / output_id
        entry = run.checkpoints().get("output")
        reused = False
        if entry is not None and entry["snapshot_id"] == output_id and run.reusable("output", bindings_):
            record = _read(final / "output.json")
            for relative, expected in sorted(record["files_sha256"].items()):
                file = run.root / relative
                if not file.is_file() or sha256_file(file) != expected:
                    raise RuntimeError(f"output file {file} no longer matches the recorded sha256; "
                                       "the output directory is kept untouched, inspect it before retry")
            reused = True
        else:
            headroom = disk_headroom(run.root)
            if not headroom["ok"]:
                raise DiskHeadroomError(f"free {headroom['free_ratio']:.4f} of disk before output, "
                                        f"required {DISK_HEADROOM_RATIO:.2f}")
            if output_root.exists():
                raise RuntimeError(f"output directory {output_root} already exists and cannot be adopted; "
                                   "it was not changed")
            summary = write_output(converted, state, output_root / "public", output_root / "release")
            _, converted_report = load_snapshot(converted, "converted")
            write_catalog(output_root / "public", output_root / "release", summary,
                          converted_report=converted_report, cutoff_yyyymm=bindings_["cutoff_yyyymm"])
            try:
                verification = verify_output(output_root / "public", output_root / "release")
            except OutputVerificationError as exc:
                _json_write(output_root / "verify_report.json", exc.report)
                raise
            files_sha256 = {item.relative_to(run.root).as_posix(): sha256_file(item)
                            for item in sorted(output_root.rglob("*")) if item.is_file()}
            record = {"output_id": output_id, "public_dir": (output_root / "public").relative_to(run.root).as_posix(),
                      "release_dir": (output_root / "release").relative_to(run.root).as_posix(),
                      "summary": summary, "files_sha256": files_sha256,
                      "catalog_sha256": verification["catalog_sha256"], "verification": verification}
            binding = stage_bindings("output", {"run_bindings": bindings_, "output_id": output_id}, input_hashes)
            current_pointer = store.current()
            parent = current_pointer["snapshot_id"] if current_pointer else None
            store.begin(output_id, expected_parent=parent, bindings=binding)
            build = store.root / "build" / output_id
            build.mkdir(parents=True)
            _json_write(build / "output.json", record)
            store.add(output_id, "output.json", build / "output.json", format_name="binary", row_count=None)
            store.publish(output_id, expected_parent=parent)
            run.commit("output", output_id)
        result["steps"].append({"step": "output", "snapshot_id": output_id, "reused_checkpoint": reused,
                                "elapsed_seconds": round(time.perf_counter() - start, 3),
                                "process_peak_rss_bytes": peak_rss_bytes(),
                                "snapshot_bytes": _tree_bytes(final),
                                "disk_free_bytes": shutil.disk_usage(run.root).free})
        return final

    def write_report(report, stage_hashes):
        store = SnapshotStore(run.root / "run-report")
        binding = stage_bindings("run-report", {"run_bindings": bindings_}, stage_hashes)
        identifier = f"run-report-{digest(binding)[:24]}"
        final = store.root / "snapshots" / identifier
        if final.exists():
            return final
        current_pointer = store.current()
        parent = current_pointer["snapshot_id"] if current_pointer else None
        store.begin(identifier, expected_parent=parent, bindings=binding)
        build = store.root / "build" / identifier
        build.mkdir(parents=True)
        _json_write(build / "run_report.json", report)
        store.add(identifier, "run_report.json", build / "run_report.json", format_name="binary", row_count=None)
        store.publish(identifier, expected_parent=parent)
        return final

    lock = run.root / ".run-full-lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise RuntimeError("run-full already running on this run or stale lock; inspect before retry") from exc
    try:
        try:
            current["step"] = "quarantine"
            result["quarantined"] = quarantine_residue(run.root)
            current["step"] = "verify-sources"
            manifest = _read(manifest_path)
            descriptor = _read(address_source)
            sources = verify_inventory(raw_dir, manifest)
            result["steps"].append({"step": "verify-sources", "verified": sources["verified"],
                                    **{k: sources[k] for k in ["batch_count", "passed_count", "failed_count",
                                                               "known_empty_count"]}})
            if not sources["verified"]:
                result["failures"] = [
                    *({"step": "verify-sources", "batch": row["batch"], **failure}
                      for row in sources["batches"] for failure in row["failures"]),
                    *({"step": "verify-sources", **failure} for failure in sources["manifest_failures"])]
                return result
            batches = sorted(entry["batch"] for entry in manifest["inputs"])
            work = run.root
            index = stage("offline-index", lambda: build_index(address_dir, descriptor, work))
            ingested = stage("ingested", lambda: ingest(raw_dir, manifest, batches, work, batch_rows=batch_rows))
            normalized = stage("normalized", lambda: normalize(ingested, work, cutoff=cutoff, rules_path=rules_path,
                                                               char_fixes=char_fixes, batch_rows=batch_rows))
            converted = stage("converted", lambda: export_converted(normalized, work))
            current["step"] = "coverage"
            _, converted_report = load_snapshot(converted, "converted")
            coverage = coverage_failures(manifest, converted_report)
            result["steps"].append({"step": "coverage", "passed": not coverage})
            if coverage:
                result["failures"] = [{"step": "coverage", **failure} for failure in coverage]
                return result
            pool = stage("address-pool", lambda: build_pool(converted, address_dir, descriptor, work, index_path=index))
            state = stage("offline-state", lambda: resolve_offline(
                pool, index, work, coordinate_tolerance_m=load_coordinate_tolerance(config),
                suspended_counties=load_suspended_counties(config)))
            review = stage("review", lambda: build_review(converted, state, work, descriptor=descriptor))
            output = output_stage(converted, state)
            released = _read(output / "output.json")
            current["step"] = "run-report"
            _, state_report = load_p2(state, "offline-state")
            _, review_report = load_p2(review, "review")
            stages = {name: run.checkpoints()[name] for name in RUN_FULL_STAGES[:-1]}
            report = {
                "run_id": run_id, "completed": True, "bindings": bindings_, "stages": stages,
                "batches": converted_report["batches"], "known_empty_batches": converted_report["known_empty_batches"],
                "source_verification": {k: sources[k] for k in ["batch_count", "passed_count", "known_empty_count"]},
                "rows": {k: converted_report[k] for k in ["input_rows", "retained_rows", "excluded_rows", "failed_rows",
                                                          "diagnostic_rows", "component_rows", "detail_rows"]},
                "line_mode_members": converted_report.get("line_mode_members", []),
                "detail_link": converted_report["detail_link"],
                "member_count": len(converted_report["member_dispositions"]),
                "address_status_counts": state_report["status_counts"],
                "review_reason_counts": review_report["reason_counts"],
                "output": {"output_id": released["output_id"], "public_dir": released["public_dir"],
                           "release_dir": released["release_dir"],
                           "total_rows": released["summary"]["total_rows"],
                           "file_count": len(released["summary"]["files"]),
                           "parquet_rows": sum(item["rows"] for item in released["summary"]["files"].values()
                                               if item["format"] == "geoparquet"),
                           "catalog_sha256": released["catalog_sha256"],
                           "warnings": released["verification"]["warnings"],
                           "quality": released["verification"]["quality"]},
                "quarantined": result["quarantined"],
                "address_source_head": result["address_source_head"],
                "resources": resources(),
            }
            hashes = [sha256_file(run.stage(name).root / "snapshots" / entry["snapshot_id"] / "manifest.json")
                      for name, entry in stages.items()]
            path = stage("run-report", lambda: write_report(report, hashes))
            result["report"] = str(path / "run_report.json")
            result["completed"] = True
        except AddressSourceChanged as exc:
            result["failures"].append({"step": current["step"], "code": "address_source_changed", "detail": str(exc)})
        except Exception as exc:  # any failure leaves the run incomplete; no run-report is written
            result["failures"].append({"step": current["step"], "code": type(exc).__name__, "detail": str(exc)})
        return result
    finally:
        result["elapsed_seconds"] = round(time.perf_counter() - clock, 3)
        result["process_peak_rss_bytes"] = peak_rss_bytes()
        result["resources"] = resources()
        attempts = run.root / "attempts"
        attempts.mkdir(exist_ok=True)
        _json_write(attempts / f"{_utc_stamp()}.json", result)
        lock.rmdir()


def _run_full_command(args) -> int:
    result = run_full(run_id=args.run_id, cutoff=args.cutoff, address_dir=args.address_dir, runs_root=args.runs_root,
                      raw_dir=args.raw_dir, manifest_path=args.manifest, address_source=args.address_source,
                      rules_path=args.garbled_rules, config=args.config, batch_rows=args.batch_rows)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["completed"] else 1
