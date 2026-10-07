"""Read-only impact measurement for R04-6 (building_key_v2 changes). Writes nothing under data/.

Usage: python scripts/measure_key_impact.py [snapshot_dir] [baseline_commit]
Counts addresses in a normalized snapshot whose building_key_v2 changes (a) because a doubled county prefix is
dropped (fix 1: baseline commit vs current code) and (b) because 號之N would become distinguishable from 之N號
(fix 2: counted from the address text, since the fix may not be applied yet).
"""
from __future__ import annotations

import glob
import re
import subprocess
import sys
import types
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lvr_pipeline.addresses import identity as cur  # noqa: E402
from lvr_pipeline.addresses.parse import arab_to_cjk, norm  # noqa: E402

snapshot = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "data/work/normalized/snapshots/fast-all58-v5-normalize")
baseline = sys.argv[2] if len(sys.argv) > 2 else "99b6226"
source = subprocess.check_output(["git", "-C", str(ROOT), "show", f"{baseline}:lvr_pipeline/addresses/identity.py"], text=True)
old = types.ModuleType("lvr_pipeline.addresses._identity_baseline")
old.__package__ = "lvr_pipeline.addresses"
exec(compile(source, "identity_baseline", "exec"), old.__dict__)

counts = Counter()
for path in glob.glob(str(snapshot / "address_components" / "*" / "*.parquet")):
    for address, n in Counter(pq.read_table(path, columns=["normalized_address"]).column(0).to_pylist()).items():
        counts[address] += n

summary = Counter()
examples: dict[str, list] = {"fix1_changed": [], "fix1_none": [], "fix2": []}
for address, n in counts.items():
    summary["distinct"] += 1
    summary["rows"] += n
    before, after = old.building_key_v2(address), cur.building_key_v2(address)
    if before != after:
        kind = "fix1_none" if after is None else "fix1_changed"
        summary[kind + "_distinct"] += 1
        summary[kind + "_rows"] += n
        if len(examples[kind]) < 5:
            examples[kind].append((address, before is None))
    if after is not None and re.search(r"號之[0-9]+", arab_to_cjk(norm(address.strip()))):
        summary["fix2_distinct"] += 1
        summary["fix2_rows"] += n
        if len(examples["fix2"]) < 5:
            examples["fix2"].append(address)
for key in sorted(summary):
    print(key, summary[key])
for key, value in examples.items():
    print(key, value)

# Breakdown of fix-1 effects by doubled county prefix.
by_prefix = Counter()
for address, n in counts.items():
    if old.building_key_v2(address) != cur.building_key_v2(address):
        by_prefix[(norm(address)[:6], cur.building_key_v2(address) is None)] += 1
print("fix1 by prefix (prefix, became_none):", dict(by_prefix.most_common(10)))
