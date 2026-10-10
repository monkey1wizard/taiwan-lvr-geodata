"""Command-line parsing and the JSON error output."""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
from pathlib import Path

import duckdb
from jsonschema import ValidationError

from .pipeline import run


def _add_offline_and_output_commands(sub):
    p = sub.add_parser("pin-address-source")
    p.add_argument("--address-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, default=Path("config/sources/address_source.json"))
    p = sub.add_parser("verify-sources", help="Check raw ZIPs against the raw manifest (read-only)")
    p.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    p.add_argument("--manifest", type=Path, default=Path("config/sources/raw_manifest.json"))
    p = sub.add_parser("inventory-sources", help="Write a raw manifest from a directory of raw ZIPs")
    p.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("run-full", help="Full offline run over every manifest batch, committed stage by stage (R06-4)")
    p.add_argument("--run-id", required=True)
    p.add_argument("--cutoff", type=int, required=True)
    p.add_argument("--address-dir", type=Path, required=True)
    p.add_argument("--runs-root", type=Path, help="Run directories root (default data/runs)")
    p.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    p.add_argument("--manifest", type=Path, default=Path("config/sources/raw_manifest.json"))
    p.add_argument(
        "--address-source", type=Path, default=Path("config/sources/address_source.json")
    )
    p.add_argument("--garbled-rules", type=Path, default=Path("config/rules/character-fixes.csv"))
    p.add_argument(
        "--config",
        type=Path,
        help="Pipeline TOML with coordinate_tolerance_m and suspended_counties (default config/pipeline.example.toml)",
    )
    p.add_argument("--batch-rows", type=int, default=1024)
    p.add_argument("--notices", type=Path, required=True, help="Public source notices JSON for the output release")
    p = sub.add_parser("build-offline-index")
    p.add_argument("--address-dir", type=Path, required=True)
    p.add_argument(
        "--address-source", type=Path, default=Path("config/sources/address_source.json")
    )
    p.add_argument("--county", action="append")
    p.add_argument("--official-source", type=Path)
    p.add_argument("--official-file", type=Path)
    p.add_argument("--work-dir", type=Path, default=Path("data/work"))
    p.add_argument("--run-id")
    p = sub.add_parser("audit-address-source")
    p.add_argument("--address-dir", type=Path, required=True)
    p.add_argument(
        "--address-source", type=Path, default=Path("config/sources/address_source.json")
    )
    p.add_argument("--county", action="append")
    p.add_argument("--output-dir", type=Path, required=True)
    p = sub.add_parser("build-address-pool")
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--address-dir", type=Path, required=True)
    p.add_argument(
        "--address-source", type=Path, default=Path("config/sources/address_source.json")
    )
    p.add_argument("--index", type=Path, required=True)
    p.add_argument("--work-dir", type=Path, default=Path("data/work"))
    p.add_argument("--run-id")
    p = sub.add_parser("resolve-offline")
    p.add_argument("--pool", type=Path, required=True)
    p.add_argument("--index", type=Path, required=True)
    p.add_argument("--prior-state", type=Path)
    p.add_argument(
        "--config",
        type=Path,
        help="Pipeline TOML with coordinate_tolerance_m and suspended_counties (default config/pipeline.example.toml)",
    )
    p.add_argument("--work-dir", type=Path, default=Path("data/work"))
    p.add_argument("--run-id")
    p = sub.add_parser("verify-offline-state")
    p.add_argument("--input", type=Path, required=True)
    p = sub.add_parser("prepare-tgos")
    p.add_argument("--state", type=Path, required=True)
    p.add_argument(
        "--date-file",
        type=Path,
        default=Path("data/tgos/date.json"),
        help="Date log used only for the exchange folder name",
    )
    p.add_argument("--limit", type=int, default=10_000)
    p.add_argument("--retry-query-fingerprint", action="append", default=[])
    p.add_argument("--retry-reason")
    p.add_argument("--ledger", type=Path, help="Earlier TGOS state whose sent queries must not be resent")
    p.add_argument("--work-dir", type=Path, default=Path("data/work"))
    p.add_argument("--exchange-dir", type=Path, default=Path("data/tgos"))
    p.add_argument("--run-id")
    p = sub.add_parser("repair-tgos-exchange")
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--batch", required=True)
    p.add_argument(
        "--date-file",
        type=Path,
        default=Path("data/tgos/date.json"),
        help="Date log used only for the exchange folder name",
    )
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
    p = sub.add_parser("build-review")
    p.add_argument("--input", type=Path, required=True, help="Converted snapshot the offline state was built from")
    p.add_argument("--state", type=Path, required=True, help="Offline state snapshot")
    p.add_argument("--tgos-state", type=Path, help="TGOS state snapshot for isolated responses")
    p.add_argument(
        "--address-source", type=Path, default=Path("config/sources/address_source.json")
    )
    p.add_argument("--prior-review", type=Path, help="Earlier review snapshot that keeps first_run_id")
    p.add_argument("--work-dir", type=Path, default=Path("data/work"))
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
    p = sub.add_parser("export-address-patch")
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--area-file", type=Path, action="append", required=True)
    p.add_argument("--work-dir", type=Path, default=Path("data/work"))
    p.add_argument("--run-id")
    p = sub.add_parser("verify-address-patch")
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


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m lvr_pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    _add_offline_and_output_commands(sub)
    for command in ["ingest", "normalize", "export-converted", "verify-converted"]:
        p = sub.add_parser(command)
        if command in {"normalize", "verify-converted"}:
            p.add_argument("--input", type=Path, required=True)
        if command == "export-converted":
            p.add_argument("--input", type=Path, help="An existing normalized snapshot")
        if command in {"ingest", "export-converted"}:
            p.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
            p.add_argument("--manifest", type=Path, default=Path("config/sources/raw_manifest.json"))
            p.add_argument("--batch", action="append", default=[])
        if command != "verify-converted":
            p.add_argument("--work-dir", type=Path, default=Path("data/work"))
            p.add_argument("--batch-rows", type=int, default=1024)
            p.add_argument("--run-id")
        if command in {"normalize", "export-converted"}:
            p.add_argument("--cutoff", type=int, required=True)
            p.add_argument("--garbled-rules", type=Path, default=Path("config/rules/character-fixes.csv"))
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError, csv.Error, duckdb.Error, ValidationError, MemoryError) as exc:
        print(json.dumps({"error": str(exc), "completed": False}, ensure_ascii=False))
        return 1
