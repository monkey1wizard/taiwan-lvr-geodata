"""c51: read-only list of place names that building_key still splits into a village and a road.

The fixed table _PLACE_NAMES in lvr_pipeline/addresses/identity.py handles the known names (option B).
This check is option A, run after each full rerun: a pool key whose village is not a village of the
town, with no neighbourhood and some road text, is a candidate when the village joined to the road,
with the same county, town and door, is a valid door of the offline index. Candidates are proposed
to the owner; the table changes only by an owner decision.

Usage:
  .venv/Scripts/python.exe scripts/check_c51_place_names.py --state <offline-state dir> --index <offline-index dir> [--out FILE]

Exit code: 0 when there are no candidates, 1 when there are candidates, 2 when the input cannot be read.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pyarrow.parquet as pq  # noqa: E402

from lvr_pipeline.addresses.admin import town_villages  # noqa: E402


def signature(value: dict) -> tuple:
    return value["county"], value["town"], value["locality_road"], value["door"]


def candidates(state: Path, index: Path) -> list[dict]:
    base = defaultdict(list)
    for row in pq.read_table(index / "offline_rows.parquet", columns=["building_key", "validity", "normalized_address"]).to_pylist():
        if row["validity"] == "valid" and row["building_key"]:
            base[signature(json.loads(row["building_key"][3:]))].append(row["normalized_address"])
    found = []
    columns = ["building_key", "status", "canonical_address"]
    for row in pq.read_table(state / "address_index.parquet", columns=columns).to_pylist():
        if not row["building_key"]:
            continue
        value = json.loads(row["building_key"][3:])
        village, road = value.get("village"), value["locality_road"]
        if not village or value.get("neighborhood") or not road or village in town_villages(value["county"], value["town"]):
            continue
        hits = base.get((value["county"], value["town"], village + road, value["door"]))
        if hits:
            found.append({"county": value["county"], "town": value["town"], "village": village, "road": road,
                          "joined_road": village + road, "door": value["door"], "status": row["status"],
                          "pool_address": row["canonical_address"], "base_example": hits[0], "base_rows": len(hits)})
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    try:
        found = candidates(args.state, args.index)
    except (OSError, ValueError, KeyError) as error:
        print(f"cannot read input: {error}", file=sys.stderr)
        return 2
    by_name = Counter((row["county"], row["town"], row["village"]) for row in found)
    print(json.dumps({"candidate_keys": len(found), "villages": len(by_name),
                      "top": [[*key, count] for key, count in by_name.most_common(20)]}, ensure_ascii=False, indent=1))
    if args.out:
        with args.out.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(found[0]) if found else ["county"])
            writer.writeheader()
            writer.writerows(found)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
