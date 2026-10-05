"""Offline indexing, output packaging and public handoff commands."""

import json
from pathlib import Path

from .address_pool import build_pool
from .address_source import pin_address_source
from .address_state import resolve_offline, load_p2
from .backfill import backfill_output
from .distribution import fetch_output, publish_release, commit_pointer
from .offline_lookup import build_index
from .packaging import package_output, verify_output, write_json
from .tgos import import_tgos, load_state, prepare_tgos, revoke_alias, transition_batch


def configure(sub):
    p = sub.add_parser("pin-address-source")
    p.add_argument("--address-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, default=Path("data/sources/address_source.json"))
    p = sub.add_parser("build-offline-index")
    p.add_argument("--address-dir", type=Path, required=True)
    p.add_argument(
        "--address-source", type=Path, default=Path("data/sources/address_source.json")
    )
    p.add_argument("--county", action="append")
    p.add_argument("--official-source", type=Path)
    p.add_argument("--official-file", type=Path)
    p.add_argument("--work-dir", type=Path, default=Path("data/work"))
    p.add_argument("--run-id")
    p = sub.add_parser("build-address-pool")
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--address-dir", type=Path, required=True)
    p.add_argument(
        "--address-source", type=Path, default=Path("data/sources/address_source.json")
    )
    p.add_argument("--index", type=Path, required=True)
    p.add_argument("--work-dir", type=Path, default=Path("data/work"))
    p.add_argument("--run-id")
    p = sub.add_parser("resolve-offline")
    p.add_argument("--pool", type=Path, required=True)
    p.add_argument("--index", type=Path, required=True)
    p.add_argument("--prior-state", type=Path)
    p.add_argument("--work-dir", type=Path, default=Path("data/work"))
    p.add_argument("--run-id")
    p = sub.add_parser("verify-offline-state")
    p.add_argument("--input", type=Path, required=True)
    p = sub.add_parser("prepare-tgos")
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--service-date", required=True)
    p.add_argument("--external-used", type=int, required=True)
    p.add_argument("--limit", type=int, default=10_000)
    p.add_argument("--retry-query-fingerprint", action="append", default=[])
    p.add_argument("--retry-reason")
    p.add_argument("--work-dir", type=Path, default=Path("data/work"))
    p.add_argument("--exchange-dir", type=Path, default=Path("data/tgos"))
    p.add_argument("--run-id")
    p = sub.add_parser("set-tgos-status")
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--batch", required=True)
    p.add_argument("--status", choices=["submitted", "submission_unknown", "cancelled"], required=True)
    p.add_argument("--reason")
    p.add_argument("--work-dir", type=Path, default=Path("data/work"))
    p.add_argument("--run-id")
    p = sub.add_parser("import-tgos")
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--batch", required=True)
    p.add_argument("--response", type=Path, required=True)
    p.add_argument("--work-dir", type=Path, default=Path("data/work"))
    p.add_argument("--run-id")
    p = sub.add_parser("revoke-alias")
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--alias-key", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--work-dir", type=Path, default=Path("data/work"))
    p.add_argument("--run-id")
    p = sub.add_parser("verify-tgos-state")
    p.add_argument("--input", type=Path, required=True)
    p = sub.add_parser("package-output")
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--notices", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, default=Path("data/output"))
    p.add_argument("--run-id")
    p = sub.add_parser("verify-output")
    p.add_argument("--input", type=Path, required=True)
    p = sub.add_parser("backfill-output")
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--previous", type=Path, required=True)
    p.add_argument("--notices", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, default=Path("data/output"))
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--run-id", required=True)
    p = sub.add_parser("fetch-output")
    p.add_argument("--manifest-url", required=True)
    p.add_argument("--manifest-sha256", required=True)
    p.add_argument("--target", type=Path, required=True)
    p.add_argument("--month", type=int)
    p.add_argument("--category", choices=["sales", "presale", "rent"])
    p.add_argument("--format", choices=["geoparquet", "geojson", "ndjson"])
    p.add_argument("--maintenance", action="store_true")
    p = sub.add_parser("publish-output")
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--expected-parent", required=True)
    p.add_argument("--checkout", type=Path, default=Path("."))
    p.add_argument("--receipt", type=Path, required=True)
    p = sub.add_parser("commit-release-pointer")
    p.add_argument("--receipt", type=Path, required=True)
    p.add_argument("--checkout", type=Path, default=Path("."))


def run(args):
    read = lambda path: json.loads(Path(path).read_text(encoding="utf-8"))
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
        path, exchange = prepare_tgos(
            args.state,
            args.work_dir,
            args.exchange_dir,
            service_date=args.service_date,
            external_used=args.external_used,
            limit=args.limit,
            retry_fingerprints=args.retry_query_fingerprint,
            retry_reason=args.retry_reason,
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
