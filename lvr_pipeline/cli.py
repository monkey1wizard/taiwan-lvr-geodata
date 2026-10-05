"""Explicit local P1 commands with reproducible cutoffs and scope."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import time

import duckdb
from jsonschema import ValidationError

from .converted import export_converted
from .ingest import ingest
from .normalize import normalize
from .processing import peak_rss_bytes, load_snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m lvr_pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ["ingest", "normalize", "export-converted", "verify-converted"]:
        p = sub.add_parser(command)
        if command in {"normalize", "verify-converted"}:
            p.add_argument("--input", type=Path, required=True)
        if command == "export-converted":
            p.add_argument("--input", type=Path, help="An existing normalized snapshot")
        if command in {"ingest", "export-converted"}:
            p.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
            p.add_argument("--manifest", type=Path, default=Path("data/sources/raw_manifest.json"))
            p.add_argument("--batch", action="append", default=[])
        if command != "verify-converted":
            p.add_argument("--work-dir", type=Path, default=Path("data/work"))
            p.add_argument("--batch-rows", type=int, default=1024)
            p.add_argument("--run-id")
        if command in {"normalize", "export-converted"}:
            p.add_argument("--cutoff", type=int, required=True)
            p.add_argument("--garbled-rules", type=Path, default=Path("data/registry/garbled_override.csv"))
    args = parser.parse_args(argv)
    start = time.perf_counter()
    try:
        if args.command == "verify-converted":
            _, report = load_snapshot(args.input, "converted")
            print(json.dumps({"snapshot": str(args.input), "verified": True, "dataset_counts": report["dataset_counts"]}, ensure_ascii=False))
            return 0
        if args.command == "ingest":
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
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
                manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
                raw = ingest(args.raw_dir, manifest, args.batch, args.work_dir, batch_rows=args.batch_rows,
                             run_id=args.run_id + "-ingest" if args.run_id else None)
                normalized = normalize(raw, args.work_dir, cutoff=args.cutoff, rules_path=args.garbled_rules, batch_rows=args.batch_rows,
                                       run_id=args.run_id + "-normalize" if args.run_id else None)
            path = export_converted(normalized, args.work_dir, run_id=args.run_id)
        print(json.dumps({"snapshot": str(path), "internal_only": True,
                          "elapsed_seconds": round(time.perf_counter() - start, 6), "process_peak_rss_bytes": peak_rss_bytes()}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, csv.Error, duckdb.Error, ValidationError, MemoryError) as exc:
        print(json.dumps({"error": str(exc), "completed": False}, ensure_ascii=False))
        return 1
