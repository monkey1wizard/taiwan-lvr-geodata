"""c32: read-only list of TGOS retries for the owner to approve, from the second review area.

Owner decision 2026-10-29, option A with the report of option C: a review row is listed when its entry
reason is selected (default ``tgos_not_found``) and its status is ``pending`` or ``deferred``. ``rejected``
and ``adopted`` rows are left out. A row whose door still has a waiting query (prepared, submitted,
``submission_unknown``) is left out, so is one whose latest query is not in a retryable state or whose
current text does not give its key back. Each listed row says whether the current query text is the
same as the text sent last time; the report counts both. The owner edits the list and passes it to
``prepare-tgos --retry-file`` with ``--retry-reason``.

Usage:
  .venv/Scripts/python.exe scripts/tgos_retry_list.py --review <review dir> --state <tgos-state dir> --out FILE [--entry-reason CODE ...]

The report is printed as JSON. Exit code: 0 when the list was written, 2 when the input cannot be read.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pyarrow.parquet as pq  # noqa: E402

from lvr_pipeline.addresses.pool import district_query  # noqa: E402
from lvr_pipeline.tgos.batches import district_text_or_none, key_query_text  # noqa: E402

LISTED = {"pending", "deferred"}
WAITING = {"prepared", "submitted", "submission_unknown"}
RETRYABLE = {"failed", "rejected", "cancelled"}
COLUMNS = ["query_fingerprint", "review_key", "key_kind", "entry_reason", "review_status", "sent_text",
           "current_text", "text_changed"]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def current_text(row: dict) -> str | None:
    if row["key_kind"] == "building_key":
        try:
            return key_query_text(row["building_key"])
        except ValueError:
            return None
    parsed = district_query(row["canonical_address"])
    return district_text_or_none(row["county_code"], parsed) if parsed is not None else None


def retry_list(review: Path, state: Path, reasons: set[str]) -> tuple[list[dict], Counter]:
    queries = pq.read_table(state / "tgos_queries.parquet").to_pylist()
    by_fingerprint = {q["query_fingerprint"]: q for q in queries}
    waiting = {q["building_key"] if q["query_kind"] == "building_key" else q["query_fingerprint"]
               for q in queries if q["status"] in WAITING}
    listed, counts = [], Counter()
    for row in pq.read_table(review / "second_review.parquet").to_pylist():
        if row["entry_reason"] not in reasons:
            continue
        if row["status"] not in LISTED:
            counts[f"left_out_{row['status']}"] += 1
            continue
        if row["key_kind"] not in {"building_key", "district_query"}:
            counts["left_out_keyless"] += 1
            continue
        query = by_fingerprint.get(row["query_fingerprint"])
        if row["review_key"] in waiting or query is None or query["status"] in WAITING:
            counts["left_out_waiting"] += 1
            continue
        if query["status"] not in RETRYABLE:
            counts["left_out_not_retryable"] += 1
            continue
        text = current_text(row)
        if text is None:
            counts["left_out_text_not_round_trip"] += 1
            continue
        changed = text != query["address"]
        counts["text_changed" if changed else "text_same"] += 1
        listed.append({"query_fingerprint": row["query_fingerprint"], "review_key": row["review_key"],
                       "key_kind": row["key_kind"], "entry_reason": row["entry_reason"],
                       "review_status": row["status"], "sent_text": query["address"], "current_text": text,
                       "text_changed": "yes" if changed else "no"})
    listed.sort(key=lambda r: (r["text_changed"], r["key_kind"], r["sent_text"]))
    return listed, counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--review", type=Path, required=True, help="review snapshot directory")
    parser.add_argument("--state", type=Path, required=True, help="TGOS state snapshot directory of the same run")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--entry-reason", action="append", default=None)
    args = parser.parse_args(argv)
    reasons = set(args.entry_reason or ["tgos_not_found"])
    try:
        listed, counts = retry_list(args.review, args.state, reasons)
    except (OSError, KeyError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(listed)
    report = {
        "entry_reasons": sorted(reasons),
        "review_manifest_sha256": sha256(args.review / "manifest.json"),
        "state_manifest_sha256": sha256(args.state / "manifest.json"),
        "listed": len(listed),
        "by_kind": dict(Counter(r["key_kind"] for r in listed)),
        "counts": dict(sorted(counts.items())),
        "out": str(args.out),
        "out_sha256": sha256(args.out),
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
