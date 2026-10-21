"""b17: read-only check of the stored TGOS adoption decisions in a tgos-state snapshot.

Reads tgos_batches.parquet, tgos_queries.parquet and tgos_imports.parquet (and address_index.parquet
when the snapshot is merged) and checks that the stored results follow the current rules. It does
not import the adoption code (lvr_pipeline/tgos/validate.py); the rule strings it needs are copied
below and each copy names its source.

Usage:
  .venv/Scripts/python.exe scripts/check_tgos_v12.py --state <tgos-state dir> [--batch ID] [--out FILE]

Exit code: 0 when every check passes, 1 when a check fails, 2 when the input cannot be read.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pyarrow.parquet as pq  # noqa: E402

from lvr_pipeline.addresses.identity import building_key  # noqa: E402
from lvr_pipeline.contracts.validate import TAIWAN_BOUNDS  # noqa: E402
from lvr_pipeline.results.reconcile import haversine_m  # noqa: E402

# The 30 m adoption rule (地址規則「離線候選與定位」): a door with other evidence keeps the medoid.
TOLERANCE_M = 30.0
from lvr_pipeline.results.review import TGOS_REJECTION_CODES  # noqa: E402
from lvr_pipeline.tgos.adopt import DOOR, NEIGHBORHOOD, ROAD_SECTION, VILLAGE_AS_PLACE, VILLAGE_DOOR, adoption_route  # noqa: E402
from lvr_pipeline.tgos.adopt import response_key as adopt_response_key  # noqa: E402

# Copied from lvr_pipeline/tgos/validate.py (DISTRICT_FILL_REASON, KEYLESS_REASON, the differs text in
# apply_response). Copied, not imported, so this check does not load the adoption module.
DISTRICT_FILL_REASON = "tgos_district_fill"
KEYLESS_REASON = "TGOS query has no building key under the current rules"
DIFFERS_REASON = "TGOS response address differs from submitted address"
OUT_OF_BOUNDS_REASON = "TGOS coordinate is outside declared WGS84 Taiwan bounds"
OUTSIDE_GROUP_REASON = "tgos_neighborhood_outside_base_group"
# Rejection texts of the base group rules (lvr_pipeline/tgos/adopt.py) and the route each needs.
GROUP_REJECTIONS = {
    "TGOS neighbourhood differs and the base group has several locations": {NEIGHBORHOOD},
    "TGOS village and door have several locations in the base": {VILLAGE_DOOR, VILLAGE_AS_PLACE},
    "TGOS road section added and the base has several locations": {ROAD_SECTION},
}

RESULT_STATUSES = {"succeeded", "failed", "rejected"}
QUERY_KINDS = {"building_key", "district_missing", "keyless"}
MAX_SAMPLES = 5
NO_REASON = "(none)"
SAMPLE_FIELDS = (
    "result_id", "batch_id", "query_fingerprint", "address", "response_address", "status", "reason",
    "lng", "lat", "response_lng", "response_lat", "response_building_key",
)
CHECK_NAMES = (
    "1_one_to_one", "2_status_set", "3_succeeded_rows", "4_rejected_rows", "5_failed_rows",
    "6_keyless_never_succeeded", "7_address_index", "8_query_key_current",
)


class InputError(Exception):
    """The snapshot cannot be read; the script exits with code 2."""


class Check:
    def __init__(self, name: str):
        self.name = name
        self.failures = 0
        self.counts: Counter = Counter()
        self.samples: list[dict] = []
        self.extra: dict = {}

    def fail(self, view: dict, why: str) -> None:
        self.failures += 1
        self.counts[why] += 1
        if len(self.samples) < MAX_SAMPLES:
            self.samples.append({"why": why, **view})

    def result(self) -> dict:
        return {
            "pass": self.failures == 0,
            "failures": self.failures,
            "failure_counts": dict(self.counts),
            "samples": self.samples,
            **self.extra,
        }


def in_bounds(lng: float, lat: float) -> bool:
    # Inclusive bounds from TAIWAN_BOUNDS (west, east, south, north); NaN compares False.
    west, east, south, north = TAIWAN_BOUNDS
    return west <= lng <= east and south <= lat <= north


def response_key(text: str | None) -> str | None:
    """Door key of a response address, normalised the way the adoption step normalises it."""
    if not text:
        return None
    return adopt_response_key(text)


def view_of(result: dict, query: dict) -> dict:
    return {**{k: result.get(k) for k in SAMPLE_FIELDS}, "query_kind": query.get("query_kind")}


def differs_kind(query_key: str | None, response_text: str | None) -> str:
    """Which building_key fields differ; same rule as data/tmp/r09-7/differs.py."""
    if query_key is None:
        return "query_no_key"
    response = response_key(response_text)
    if response is None:
        return "response_no_key"
    a = json.loads(query_key[3:])
    b = json.loads(response[3:])
    diff = sorted(
        k for k in set(a) | set(b)
        if a.get(k) != b.get(k) and not (a.get(k) is None and k in ("village", "neighborhood"))
    )
    return ",".join(diff) or "none"


def check_batch(batch_id: str, queries: list[dict], results: list[dict], index: dict | None) -> dict:
    query_by_fp: dict[str, dict] = {}
    for q in queries:
        query_by_fp.setdefault(q["query_fingerprint"], q)
    query_count = Counter(q["query_fingerprint"] for q in queries)
    result_count = Counter(r["query_fingerprint"] for r in results)
    result_by_fp: dict[str, dict] = {}
    for r in results:
        result_by_fp.setdefault(r["query_fingerprint"], r)
    pairs = [(r, query_by_fp.get(r["query_fingerprint"], {})) for r in results]

    # 1. One-to-one between the batch's queries and its result rows.
    c1 = Check(CHECK_NAMES[0])
    for q in queries:
        fp = q["query_fingerprint"]
        view = {"query_fingerprint": fp, "address": q["address"], "query_kind": q.get("query_kind")}
        if query_count[fp] != 1:
            c1.fail(view, f"query fingerprint appears {query_count[fp]} times in the batch")
        elif result_count[fp] != 1:
            c1.fail(view, f"query has {result_count[fp]} tgos-result rows")
    for r, q in pairs:
        if not q:
            c1.fail(view_of(r, q), "result row belongs to no query of this batch")

    # 2. Status set.
    c2 = Check(CHECK_NAMES[1])
    for r, q in pairs:
        if r["status"] not in RESULT_STATUSES:
            c2.fail(view_of(r, q), f"status {r['status']!r} is not one of succeeded, failed, rejected")

    # 3. succeeded rows.
    c3 = Check(CHECK_NAMES[2])
    for r, q in pairs:
        if r["status"] != "succeeded":
            continue
        view, kind = view_of(r, q), q.get("query_kind")
        if kind not in QUERY_KINDS:
            c3.fail(view, f"query_kind {kind!r} is unknown")
        lng, lat = r["lng"], r["lat"]
        if lng is None or lat is None:
            c3.fail(view, "lng or lat is null")
            continue
        if not in_bounds(lng, lat):
            c3.fail(view, "lng/lat outside the Taiwan bounds")
        if (r["response_lng"], r["response_lat"]) != (lng, lat):
            c3.fail(view, "lng/lat differ from response_lng/response_lat")
        if kind == "building_key":
            route = adoption_route(q.get("building_key"), response_key(r["response_address"]))
            if route is None:
                c3.fail(view, "adoption_route(query building_key, response key) is None")
            elif r["reason"] == OUTSIDE_GROUP_REASON:
                if route != NEIGHBORHOOD:
                    c3.fail(view, f"reason {OUTSIDE_GROUP_REASON}, but the route is {route}")
            elif r["reason"] is not None:
                c3.fail(view, f"building_key row has unexpected reason {r['reason']!r}")
        elif kind == "district_missing":
            if r["reason"] != DISTRICT_FILL_REASON:
                c3.fail(view, f"reason is not {DISTRICT_FILL_REASON}")
            if r["response_building_key"] is None:
                c3.fail(view, "response_building_key is null for a district fill")

    # 4. rejected rows.
    c4 = Check(CHECK_NAMES[3])
    for r, q in pairs:
        if r["status"] != "rejected":
            continue
        view, kind, reason = view_of(r, q), q.get("query_kind"), r["reason"]
        if r["lng"] is not None or r["lat"] is not None:
            c4.fail(view, "rejected row has a non-null lng or lat")
        if reason == KEYLESS_REASON:
            if kind != "keyless":
                c4.fail(view, "keyless reason on a query that is not keyless")
        elif reason in TGOS_REJECTION_CODES:
            if reason == DIFFERS_REASON and q.get("building_key") is not None:
                if adoption_route(q["building_key"], response_key(r["response_address"])) == DOOR:
                    c4.fail(view, "differs reason, but the response names the queried door")
            if reason in GROUP_REJECTIONS:
                if adoption_route(q.get("building_key"), response_key(r["response_address"])) not in GROUP_REJECTIONS[reason]:
                    c4.fail(view, "base group rejection, but the response is not of that rule's route")
            if reason == OUT_OF_BOUNDS_REASON:
                x, y = r["response_lng"], r["response_lat"]
                if x is None or y is None or in_bounds(x, y):
                    c4.fail(view, "outside-bounds reason, but the response pair is missing or inside the bounds")
        else:
            c4.fail(view, f"reason {reason!r} is not a known rejection")

    # 5. failed rows.
    c5 = Check(CHECK_NAMES[4])
    for r, q in pairs:
        if r["status"] == "failed" and (r["lng"] is not None or r["lat"] is not None):
            c5.fail(view_of(r, q), "failed row has a non-null lng or lat")

    # 6. keyless queries never succeed.
    c6 = Check(CHECK_NAMES[5])
    keyless = [q for q in queries if q.get("query_kind") == "keyless"]
    for q in keyless:
        result = result_by_fp.get(q["query_fingerprint"])
        if result is not None and result["status"] == "succeeded":
            c6.fail({"query_fingerprint": q["query_fingerprint"], "address": q["address"]}, "keyless query succeeded")

    # 7. Merged snapshot only: succeeded building_key rows agree with address_index.
    c7 = Check(CHECK_NAMES[6])
    c7.extra = {"applicable": index is not None, "succeeded_building_key_rows": 0,
                "located_matching": 0, "located_with_other_evidence": 0, "conflict_listed": 0}
    if index is not None:
        for r, q in pairs:
            if r["status"] != "succeeded" or q.get("query_kind") != "building_key":
                continue
            c7.extra["succeeded_building_key_rows"] += 1
            view = view_of(r, q)
            row = index.get(q.get("building_key"))
            if row is None:
                c7.fail(view, "building_key is not in address_index")
            elif row["status"] == "conflict":
                c7.extra["conflict_listed"] += 1
            elif row["status"] != "located":
                c7.fail(view, f"address_index status is {row['status']!r}")
            elif (row["lng"], row["lat"]) != (r["lng"], r["lat"]):
                # B-1 rekeyed doors can also be located offline; the TGOS point then joins the
                # evidence and the 30 m rule picks the representative.
                if (row["coordinate_count"] or 0) > 1 and haversine_m(
                        (row["lng"], row["lat"]), (r["lng"], r["lat"])) <= TOLERANCE_M:
                    c7.extra["located_with_other_evidence"] += 1
                else:
                    c7.fail(view, "address_index coordinate differs from the TGOS coordinate")
            else:
                c7.extra["located_matching"] += 1

    # 8. Stored query key under the current rules. Query keys are built from the raw address
    # (pool.py, and batches.py _carry_ledger), so the same call is used here; normalize_address
    # applies to response addresses only.
    c8 = Check(CHECK_NAMES[7])
    c8.extra = {"building_key_queries": 0, "keyless_queries": 0}
    for q in queries:
        kind = q.get("query_kind")
        if kind not in ("building_key", "keyless"):
            continue
        current = building_key(q["address"])
        view = {"query_fingerprint": q["query_fingerprint"], "address": q["address"],
                "query_kind": kind, "building_key": q.get("building_key"), "current_building_key": current}
        if kind == "building_key":
            c8.extra["building_key_queries"] += 1
            if q.get("building_key") != current:
                c8.fail(view, "stored building_key differs from building_key(address)")
        else:
            c8.extra["keyless_queries"] += 1
            if current is not None:
                c8.fail(view, "keyless query, but building_key(address) is not None")

    checks = {c.name: c.result() for c in (c1, c2, c3, c4, c5, c6, c7, c8)}
    differs = Counter(
        differs_kind(q.get("building_key"), r["response_address"])
        for r, q in pairs if r["reason"] == DIFFERS_REASON
    )
    return {
        "batch_id": batch_id,
        "pass": all(v["pass"] for v in checks.values()),
        "query_rows": len(queries),
        "result_rows": len(results),
        "counts_by_status": dict(sorted(Counter(r["status"] for r in results).items())),
        "counts_by_reason": dict(sorted(Counter(r["reason"] or NO_REASON for r in results).items())),
        "differs_fields": dict(sorted(differs.items())),
        "keyless_queries": len(keyless),
        "checks": checks,
    }


def read_table(state: Path, name: str) -> list[dict]:
    path = state / name
    if not path.is_file():
        raise InputError(f"required file is missing: {path}")
    return pq.read_table(path).to_pylist()


def build_report(state: Path, only_batch: str | None) -> dict:
    if not state.is_dir():
        raise InputError(f"snapshot directory not found: {state}")
    batches = read_table(state, "tgos_batches.parquet")
    queries = read_table(state, "tgos_queries.parquet")
    results = read_table(state, "tgos_imports.parquet")
    index_path = state / "address_index.parquet"
    index = None
    if index_path.is_file():
        index = {row["building_key"]: row for row in pq.read_table(index_path).to_pylist()}

    known = {b["batch_id"] for b in batches}
    if only_batch is not None:
        if only_batch not in known:
            raise InputError(f"batch {only_batch} is not in tgos_batches.parquet")
        chosen = [b for b in batches if b["batch_id"] == only_batch]
    else:
        chosen = [b for b in batches if b["status"] == "completed"]

    orphan = Check("0_orphan_results")
    for r in results:
        if r["batch_id"] not in known:
            orphan.fail(view_of(r, {}), "result batch_id is not in tgos_batches")

    queries_by_batch, results_by_batch = defaultdict(list), defaultdict(list)
    for q in queries:
        queries_by_batch[q["batch_id"]].append(q)
    for r in results:
        results_by_batch[r["batch_id"]].append(r)
    checked = [
        check_batch(b["batch_id"], queries_by_batch[b["batch_id"]], results_by_batch[b["batch_id"]], index)
        for b in chosen
    ]
    report = {
        "state": str(state),
        "snapshot_kind": "merged" if index is not None else "ledger",
        "batches_checked": [b["batch_id"] for b in chosen],
        "checks": {orphan.name: orphan.result()},
        "batches": checked,
    }
    if not chosen:
        report["note"] = "no batch to check: no completed batch and no --batch given"
    report["pass"] = bool(chosen) and orphan.failures == 0 and all(b["pass"] for b in checked)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--state", required=True, type=Path, help="tgos-state snapshot directory")
    parser.add_argument("--batch", default=None, help="check only this batch_id")
    parser.add_argument("--out", default=None, type=Path, help="also write the JSON report to this file")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.state, args.batch)
    except InputError as exc:
        print(f"check_tgos_v12: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
