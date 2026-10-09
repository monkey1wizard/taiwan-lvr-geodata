"""Command dispatch and the ingest, normalize and convert chain."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import subprocess
import time
from pathlib import Path

from .address_pool import build_pool
from .offline.index import audit_address_source, pin_address_source, build_index
from .addresses.identity import NORMALIZATION_VERSION
from .contracts import SCHEMA_VERSION
from .results.reconcile import DEFAULT_CONFIG, load_coordinate_tolerance, load_suspended_counties, resolve_offline, load_p2
from .results.review import build_review
from .backfill import backfill_output
from .address_patch import export_address_patch, verify_address_patch
from .distribution import fetch_output, publish_release, commit_pointer
from .packaging import package_output, verify_output, write_json
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
    ROOT,
    RunStore,
    SnapshotStore,
    _json_write,
    bindings as stage_bindings,
    digest,
    load_snapshot,
    peak_rss_bytes,
    sha256_file,
)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


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
                         batch_rows=args.batch_rows, run_id=args.run_id)
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
            normalized = normalize(raw, args.work_dir, cutoff=args.cutoff, rules_path=args.garbled_rules, batch_rows=args.batch_rows,
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
    elif command == "package-output":
        path = package_output(
            args.input,
            args.state,
            args.output_dir,
            notices=read(args.notices),
            run_id=args.run_id,
        )
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
    elif command == "verify-output":
        manifest = verify_output(args.input)
        print(
            json.dumps(
                {
                    "verified": True,
                    "snapshot_id": manifest["snapshot_id"],
                    "retained_rows": manifest["retained_rows"],
                }
            )
        )
        return 0
    elif command == "backfill-output":
        path, report = backfill_output(
            args.input,
            args.state,
            args.previous,
            args.output_dir,
            notices=read(args.notices),
            run_id=args.run_id,
        )
        write_json(args.report, report)
        print(json.dumps({"path": str(path), "report": str(args.report), "completed": True}, ensure_ascii=False))
        return 0
    elif command == "export-address-patch":
        path = export_address_patch(args.state, args.area_file, args.work_dir, run_id=args.run_id)
    elif command == "verify-address-patch":
        report = verify_address_patch(args.input)
        print(json.dumps({"verified": True, "snapshot_id": report["snapshot_id"], "patch_rows": report["patch_rows"], "quarantine_rows": report["quarantine_rows"]}, ensure_ascii=False))
        return 0
    elif command == "fetch-output":
        path = fetch_output(
            args.manifest_url,
            args.manifest_sha256,
            args.target,
            month=args.month,
            category=args.category,
            format=args.format,
            maintenance=args.maintenance,
        )
    elif command == "publish-output":
        receipt = publish_release(args.input, args.expected_parent, args.checkout)
        write_json(args.receipt, receipt)
        print(
            json.dumps(
                {
                    "receipt": str(args.receipt),
                    "release_tag": receipt["release_tag"],
                    "pointer_committed": False,
                }
            )
        )
        return 0
    elif command == "commit-release-pointer":
        commit = commit_pointer(read(args.receipt), args.checkout)
        print(json.dumps({"commit": commit, "pushed": True}))
        return 0
    else:
        raise ValueError("Unknown P2 command")
    print(json.dumps({"path": str(path), "completed": True}, ensure_ascii=False))
    return 0


# R06-4: one full offline run over every batch of the raw manifest.
RUN_FULL_STAGES = ["offline-index", "ingested", "normalized", "converted", "address-pool",
                   "offline-state", "review", "run-report"]
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


def run_bindings(manifest_path: Path, address_source: Path, rules_path: Path, config: Path, cutoff: int) -> dict:
    """The eight RunStore bindings of a full run; any change needs a new run id."""
    commit = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    return {"code_commit": commit, "raw_manifest_sha256": sha256_file(Path(manifest_path)),
            "address_source_sha256": sha256_file(Path(address_source)),
            # The pipeline config holds the 30 m tolerance and the suspended counties.
            "rules_sha256": digest({"character_fixes": sha256_file(Path(rules_path)),
                                    "pipeline_config": sha256_file(Path(config))}),
            "contract_version": SCHEMA_VERSION, "key_version": f"building_key_v2/{NORMALIZATION_VERSION}",
            "cutoff_yyyymm": f"{int(cutoff):06d}", "scope": RUN_FULL_SCOPE}


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
    bindings_ = run_bindings(manifest_path, address_source, rules_path, config, cutoff)
    run = RunStore.open(runs_root, run_id, bindings_)
    result = {"run_id": run_id, "run_dir": str(run.root), "completed": False,
              "started_at": datetime.now(timezone.utc).isoformat(), "bindings": bindings_,
              "quarantined": [], "steps": [], "failures": []}
    current = {"step": None}

    def stage(name, build):
        current["step"] = name
        start = time.perf_counter()
        reused = run.reusable(name, bindings_)
        if reused:
            path = run.stage(name).root / "snapshots" / run.checkpoints()[name]["snapshot_id"]
        else:
            path = Path(build())
            run.commit(name, path.name)
        result["steps"].append({"step": name, "snapshot_id": path.name, "reused_checkpoint": reused,
                                "elapsed_seconds": round(time.perf_counter() - start, 3)})
        return path

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
                                                               batch_rows=batch_rows))
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
            current["step"] = "run-report"
            _, state_report = load_p2(state, "offline-state")
            _, review_report = load_p2(review, "review")
            stages = {name: run.checkpoints()[name] for name in RUN_FULL_STAGES[:-1]}
            report = {
                "run_id": run_id, "completed": True, "bindings": bindings_, "stages": stages,
                "batches": converted_report["batches"], "known_empty_batches": converted_report["known_empty_batches"],
                "source_verification": {k: sources[k] for k in ["batch_count", "passed_count", "known_empty_count"]},
                "rows": {k: converted_report[k] for k in ["input_rows", "retained_rows", "excluded_rows", "failed_rows",
                                                          "diagnostic_rows", "component_rows"]},
                "line_mode_members": converted_report.get("line_mode_members", []),
                "member_count": len(converted_report["member_dispositions"]),
                "address_status_counts": state_report["status_counts"],
                "review_reason_counts": review_report["reason_counts"],
                "quarantined": result["quarantined"],
            }
            hashes = [sha256_file(run.stage(name).root / "snapshots" / entry["snapshot_id"] / "manifest.json")
                      for name, entry in stages.items()]
            path = stage("run-report", lambda: write_report(report, hashes))
            result["report"] = str(path / "run_report.json")
            result["completed"] = True
        except Exception as exc:  # any failure leaves the run incomplete; no run-report is written
            result["failures"].append({"step": current["step"], "code": type(exc).__name__, "detail": str(exc)})
        return result
    finally:
        result["elapsed_seconds"] = round(time.perf_counter() - clock, 3)
        result["process_peak_rss_bytes"] = peak_rss_bytes()
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
