"""Offline indexing, output packaging and public handoff commands."""

import json
from pathlib import Path

from .address_pool import build_pool
from .address_state import resolve_offline, load_p2
from .distribution import fetch_output, publish_release, commit_pointer
from .offline_lookup import build_index
from .packaging import package_output, verify_output, write_json


def configure(sub):
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
    p = sub.add_parser("package-output")
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--notices", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, default=Path("data/output"))
    p.add_argument("--run-id")
    p = sub.add_parser("verify-output")
    p.add_argument("--input", type=Path, required=True)
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
    if command == "build-offline-index":
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
