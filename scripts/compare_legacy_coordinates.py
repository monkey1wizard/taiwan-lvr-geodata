#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R05-5 read-only comparison of legacy point coordinates with the offline address state.

Legacy input: the NDJSON files of the old project's data/output/ (fields ``lng`` and ``lat`` are
named WGS84 degrees per that project's CONTRACT.md; no axis is inferred or swapped).
Only ``geom_type == "point"`` records are compared; other geometry types are counted only.
Each point's address is keyed with the current ``building_key_v2`` and looked up in the offline
state's ``address_index`` (and ``coordinate_resolutions`` for the conflict basis).

Classes (per legacy point record): 1 both located within tolerance; 2 both located beyond it;
3 legacy only (subdivided by the new state); 5 address has no building_key_v2.
Class 4 (new state located, no legacy point) is reported per distinct key only, since it has no
legacy record. Output goes to data/tmp/r05-5/ unless --out is given.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
import tomllib
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lvr_pipeline.addresses.identity import building_key_v2  # noqa: E402
from lvr_pipeline.results.reconcile import haversine_m  # noqa: E402

LEGACY_DIR = "C:/Code/taiwan-lvr-geojson/data/output"
STATE_DIR = os.path.join(ROOT, "data", "tmp", "work", "r05-7", "offline-state", "snapshots", "r05-7-offline")
CONFIG = os.path.join(ROOT, "config", "pipeline.example.toml")
OUT_DIR = os.path.join(ROOT, "data", "tmp", "r05-5")

C1, C2, C3, C5 = "1_both_within", "2_both_beyond", "3_legacy_only", "5_no_key"
SUB_MISS, SUB_EXCEEDS, SUB_CROSS, SUB_NOT_IN_POOL = (
    "unmatched", "conflict_exceeds_tolerance", "conflict_door_cross_check", "key_not_in_pool")


def classify(key, legacy_lnglat, new, tolerance_m):
    """Return (class, subclass-or-None, distance_m-or-None).

    ``new`` is None when the key is absent from the address index, else a dict with
    status, lng, lat, basis (basis only meaningful for conflicts).
    """
    if key is None:
        return C5, None, None
    if new is None:
        return C3, SUB_NOT_IN_POOL, None
    if new["status"] == "located":
        d = haversine_m(legacy_lnglat, (new["lng"], new["lat"]))
        return (C1 if d <= tolerance_m else C2), None, d
    if new["status"] == "conflict":
        return C3, (SUB_CROSS if new.get("basis") == "door_cross_check" else SUB_EXCEEDS), None
    return C3, SUB_MISS, None


def read_legacy(directory):
    """Yield (file name, line number, line) for every non-empty legacy line."""
    for path in sorted(glob.glob(os.path.join(directory, "*.ndjson"))):
        with open(path, encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                if line.strip():
                    yield os.path.basename(path), number, line


def quantiles(values, points):
    if not values:
        return {}
    values = sorted(values)
    out = {}
    for p in points:
        pos = p * (len(values) - 1)
        lo = int(pos)
        hi = min(lo + 1, len(values) - 1)
        out[str(p)] = values[lo] + (values[hi] - values[lo]) * (pos - lo)
    out["max"] = values[-1]
    return out


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--legacy", default=LEGACY_DIR)
    ap.add_argument("--state", default=STATE_DIR)
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args(argv)
    import duckdb

    with open(CONFIG, "rb") as stream:
        tolerance = float(tomllib.load(stream)["coordinate_tolerance_m"])
    os.makedirs(args.out, exist_ok=True)

    totals = Counter()
    points = []  # (file, line, id, address, key, lng, lat)
    for name, number, line in read_legacy(args.legacy):
        totals["lines"] += 1
        try:
            rec = json.loads(line)
        except ValueError:
            totals["unreadable_json"] += 1
            continue
        geom = rec.get("geom_type")
        lng, lat = rec.get("lng"), rec.get("lat")
        if geom != "point":
            totals["non_point:" + str(geom)] += 1
            continue
        if not is_number(lng) or not is_number(lat):
            totals["point_without_coordinates"] += 1
            continue
        totals["points"] += 1
        address = rec.get("address")
        points.append((name, number, rec.get("id"), address, building_key_v2(address), float(lng), float(lat)))

    keys = sorted({p[4] for p in points if p[4]})
    db = duckdb.connect()
    db.execute("CREATE TABLE legacy_keys(building_key VARCHAR)")
    db.executemany("INSERT INTO legacy_keys VALUES (?)", [(k,) for k in keys])
    index = os.path.join(args.state, "address_index.parquet").replace("\\", "/")
    resolutions = os.path.join(args.state, "coordinate_resolutions.parquet").replace("\\", "/")
    new_state = {}
    for key, status, lng, lat, basis in db.execute(
        f"""SELECT i.building_key, i.status, i.lng, i.lat, r.resolution_basis
            FROM legacy_keys k JOIN '{index}' i ON i.building_key = k.building_key AND i.key_version = 'v2'
            LEFT JOIN '{resolutions}' r ON r.building_key = i.building_key AND r.key_version = i.key_version"""
    ).fetchall():
        new_state[key] = {"status": status, "lng": lng, "lat": lat, "basis": basis}
    new_only_keys = db.execute(
        f"""SELECT count(*) FROM '{index}' i WHERE i.key_version = 'v2' AND i.status = 'located'
            AND NOT EXISTS (SELECT 1 FROM legacy_keys k WHERE k.building_key = i.building_key)"""
    ).fetchone()[0]
    located_total = db.execute(f"SELECT count(*) FROM '{index}' WHERE status = 'located'").fetchone()[0]

    records = Counter()
    sub = Counter()
    key_class = defaultdict(set)
    key_sub = defaultdict(set)
    c2_rows, c3_rows, distances = [], [], []
    for name, number, rid, address, key, lng, lat in points:
        cls, subcls, dist = classify(key, (lng, lat), new_state.get(key), tolerance)
        records[cls] += 1
        if subcls:
            sub[subcls] += 1
        if key:
            key_class[cls].add(key)
            if subcls:
                key_sub[subcls].add(key)
        row = [rid, address, key, lng, lat, name, number]
        if cls == C2:
            n = new_state[key]
            distances.append(dist)
            c2_rows.append([round(dist, 3)] + row + [n["lng"], n["lat"]])
        elif cls == C3:
            c3_rows.append([subcls] + row)

    c2_rows.sort(key=lambda r: (-r[0], r[3] or "", r[1] or ""))
    c3_rows.sort(key=lambda r: (r[3] or "", r[1] or ""))
    head = ["legacy_id", "address", "building_key", "legacy_lng", "legacy_lat", "legacy_file", "legacy_line"]
    for fname, header, rows in (
        ("class2_sample.csv", ["distance_m"] + head + ["new_lng", "new_lat"], c2_rows[:20]),
        ("class3_sample.csv", ["new_state"] + head, c3_rows[:20]),
    ):
        with open(os.path.join(args.out, fname), "w", encoding="utf-8-sig", newline="") as stream:
            csv.writer(stream).writerows([header] + rows)

    compared = sum(records.values())
    summary = {
        "tolerance_m": tolerance,
        "legacy_dir": args.legacy,
        "state_dir": args.state,
        "legacy_lines": totals["lines"],
        "unreadable_json": totals["unreadable_json"],
        "non_point": {k.split(":", 1)[1]: v for k, v in totals.items() if k.startswith("non_point:")},
        "non_point_total": sum(v for k, v in totals.items() if k.startswith("non_point:")),
        "point_without_coordinates": totals["point_without_coordinates"],
        "readable_points": totals["points"],
        "class_records": {c: records[c] for c in (C1, C2, C3, C5)},
        "class_records_sum": compared,
        "sum_matches_readable_points": compared == totals["points"],
        "class3_records_by_new_state": dict(sub),
        "class_distinct_keys": {c: len(key_class[c]) for c in (C1, C2, C3)},
        "class3_distinct_keys_by_new_state": {k: len(v) for k, v in key_sub.items()},
        "class5_distinct_addresses": len({p[3] for p in points if p[4] is None}),
        "keys_in_both_class1_and_class2": len(key_class[C1] & key_class[C2]),
        "class4_distinct_keys_new_located_without_legacy_point": new_only_keys,
        "new_state_located_keys": located_total,
        "legacy_distinct_keys": len(keys),
        "class2_distance_quantiles_m": quantiles(distances, [0.5, 0.75, 0.9, 0.95, 0.99]),
        "samples": "class2: largest distance first; class3: sorted by building_key then legacy_id",
    }
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["sum_matches_readable_points"] else 1


if __name__ == "__main__":
    sys.exit(main())
