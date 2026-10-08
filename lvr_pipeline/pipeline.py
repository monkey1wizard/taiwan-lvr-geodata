"""Command dispatch and the ingest, normalize and convert chain."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .address_pool import build_pool
from .offline.index import audit_address_source, pin_address_source, build_index
from .results.reconcile import resolve_offline, load_p2
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
from .transactions.read import ingest
from .transactions.normalize import normalize
from .storage.runs import peak_rss_bytes, load_snapshot


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
    return _run_offline_and_output(args)


def _run_offline_and_output(args):
    read = _read
    command = args.command
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
