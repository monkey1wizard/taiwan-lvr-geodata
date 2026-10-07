"""Read-only R04-6b measurement: how many invalid_admin pool rows now get a county/town via
AdministrativeNames.canonicalize (which builds names through _CITY_RE). Writes nothing under data/.

Usage: python scripts/measure_admin_parse.py [pool_dir] [descriptor.json] [address_dir]
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lvr_pipeline.addresses.parse import _COUNTY_CODE  # noqa: E402
from lvr_pipeline.offline_lookup import load_pinned_source  # noqa: E402

pool = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "data/work/address-pool/snapshots/fast-all58-v5-pool2")
descriptor = json.loads(Path(sys.argv[2] if len(sys.argv) > 2 else ROOT / "config/sources/address_source.json").read_text(encoding="utf-8"))
names = load_pinned_source(Path(sys.argv[3] if len(sys.argv) > 3 else "C:/Code/taiwan-address-data"), descriptor)

table = pq.read_table(pool / "address_occurrences.parquet", columns=["normalized_address", "reason"])
table = table.filter(pc.equal(table["reason"], "invalid_admin"))
counts = Counter(table["normalized_address"].to_pylist())
counties = sorted(_COUNTY_CODE, key=len, reverse=True)
total_rows = sum(counts.values())
with_prefix = {a: n for a, n in counts.items() if a.startswith(tuple(counties))}
print("invalid_admin rows", total_rows, "addresses", len(counts))
print("with county prefix: rows", sum(with_prefix.values()), "addresses", len(with_prefix))
fixed_rows, fixed_addr = Counter(), Counter()
left_rows, left_addr = Counter(), Counter()
for address, n in with_prefix.items():
    county = next(c for c in counties if address.startswith(c))
    code, town = names.canonicalize(address)[1:]
    if code:
        fixed_rows[county] += n
        fixed_addr[county] += 1
    else:
        left_rows[county] += n
        left_addr[county] += 1
print("now parse: rows", sum(fixed_rows.values()), "addresses", sum(fixed_addr.values()))
print("still invalid_admin (county prefix): rows", sum(left_rows.values()), "addresses", sum(left_addr.values()))
for county in sorted(set(fixed_rows) | set(left_rows), key=lambda c: -fixed_rows[c]):
    print(f"{county}\tnow_parse_rows={fixed_rows[county]}\tnow_parse_addr={fixed_addr[county]}\tremain_rows={left_rows[county]}\tremain_addr={left_addr[county]}")
