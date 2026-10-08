"""Export located sales from monthly GeoParquet outputs to a Kepler.gl point CSV (read-only on outputs)."""
from __future__ import annotations

import argparse
import csv
import datetime
import json
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq

from lvr_pipeline.export import county_letters, read_wkb, spread_coordinate, spread_ranks
from lvr_pipeline.source_ref import make_source_ref

COUNTY_BY_LETTER = county_letters()
FIELDS = ["trade_date", "tx_yyyymm", "county", "district", "address", "building_type", "total_price",
          "unit_price_sqm", "building_area_sqm", "longitude", "latitude", "category", "source_ref"]
COLUMNS = ["src_batch", "source_row_number", "category", "member_path", "raw_address", "tx_date_raw", "tx_yyyymm",
           "props_json", "location_status", "is_approximation", "unique_point_count", "geometry"]


def roc_date(value: str) -> str | None:
    value = (value or "").strip()
    if len(value) != 7 or not value.isdigit():
        return None
    try:
        return datetime.date(int(value[:3]) + 1911, int(value[3:5]), int(value[5:])).isoformat()
    except ValueError:
        return None


def number(value):
    value = (value or "").strip()
    return value if value.replace(".", "", 1).isdigit() else ""


def located_point(row):
    """(lng, lat) of a fully located single-Point row, else None (same rule as the exclusion counts below)."""
    if row["location_status"] != "complete" or row["geometry"] is None:
        return None
    if row["is_approximation"] or row["unique_point_count"] != 1:
        return None
    geometry = read_wkb(row["geometry"])
    return tuple(geometry["coordinates"]) if geometry.get("type") == "Point" else None


def year_ranks(files, years, category):
    """Spread ranks as in the official yearly points files: every located row of the whole year and category
    counts, whatever --county or month filters select, so a filtered export matches the official file."""
    entries = []
    for path in files:
        if int(path.name.split("_")[0]) // 100 not in years:
            continue
        for row in pq.read_table(path, columns=COLUMNS).to_pylist():
            point = located_point(row)
            if point is not None:
                entries.append((
                    (int(path.name.split("_")[0]) // 100, *point),
                    roc_date(row["tx_date_raw"]) or "",
                    make_source_ref(row["src_batch"], row["member_path"], row["source_row_number"]),
                ))
    return spread_ranks(entries)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, required=True, help="package-output folder with monthly/")
    parser.add_argument("--category", default="sales", choices=["sales", "presale", "rent"])
    parser.add_argument("--county", action="append", default=[], help="County name, repeatable; default all")
    parser.add_argument("--from-yyyymm", type=int, default=0)
    parser.add_argument("--to-yyyymm", type=int, default=999999)
    parser.add_argument("--out", type=Path, required=True, help="CSV file, or a directory with --by-year")
    parser.add_argument("--by-year", action="store_true", help="Write one CSV per transaction year")
    parser.add_argument("--prefix", default="sales", help="File name prefix with --by-year")
    parser.add_argument("--no-id", action="store_true", help="Omit source_ref and category to shrink files")
    args = parser.parse_args(argv)
    fields = [f for f in FIELDS if not (args.no_id and f in {"source_ref", "category"})]

    counties = set(args.county)
    files = sorted((args.output_root / "monthly").glob(f"*/*/*_{args.category}.parquet"))
    selected_years = {m // 100 for m in (int(p.name.split("_")[0]) for p in files)
                      if args.from_yyyymm <= m <= args.to_yyyymm}
    ranks = year_ranks(files, selected_years, args.category)
    reasons = Counter()
    per_year = {}
    handles = {}

    def writer_for(year):
        key = year if args.by_year else "all"
        if key not in handles:
            final = args.out / f"{args.prefix}_{year}.csv" if args.by_year else args.out
            final.parent.mkdir(parents=True, exist_ok=True)
            stream = final.with_suffix(".tmp").open("w", encoding="utf-8-sig", newline="")
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            handles[key] = (stream, writer, final)
        return handles[key][1]

    for path in files:
        month = int(path.name.split("_")[0])
        if not args.from_yyyymm <= month <= args.to_yyyymm:
            continue
        year = month // 100
        stats = per_year.setdefault(year, Counter())
        for row in pq.read_table(path, columns=COLUMNS).to_pylist():
            county = COUNTY_BY_LETTER.get(row["member_path"][:1].lower(), "")
            if counties and county not in counties:
                continue
            reasons["selected"] += 1
            stats["selected"] += 1
            if row["location_status"] != "complete" or row["geometry"] is None:
                reasons["excluded_not_fully_located"] += 1
                continue
            if row["is_approximation"] or row["unique_point_count"] != 1:
                reasons["excluded_multi_point_or_approximation"] += 1
                continue
            geometry = read_wkb(row["geometry"])
            if geometry.get("type") != "Point":
                reasons["excluded_not_point"] += 1
                continue
            lng, lat = geometry["coordinates"]
            source_ref = make_source_ref(row["src_batch"], row["member_path"], row["source_row_number"])
            rank, count = ranks.get(source_ref, (0, 1))
            lng, lat = spread_coordinate(lng, lat, rank, count)  # contract 1.2 display spread, <= 1 m
            props = json.loads(row["props_json"])
            rent = row["category"] == "rent"  # rent: total_price is the rent total
            record = {
                "trade_date": roc_date(row["tx_date_raw"]) or "",
                "tx_yyyymm": row["tx_yyyymm"],
                "county": county,
                "district": props.get("鄉鎮市區", ""),
                "address": row["raw_address"],
                "building_type": props.get("建物型態", ""),
                "total_price": number(props.get("總額元" if rent else "總價元")),
                "unit_price_sqm": number(props.get("單價元平方公尺")),
                "building_area_sqm": number(props.get("建物總面積平方公尺" if rent else "建物移轉總面積平方公尺")),
                "longitude": lng,
                "latitude": lat,
                "category": row["category"],
                "source_ref": source_ref,
            }
            writer_for(year).writerow({k: record[k] for k in fields})
            reasons["written"] += 1
            stats["written"] += 1
    outputs = []
    for stream, _, final in handles.values():
        stream.close()
        final.with_suffix(".tmp").replace(final)
        outputs.append({"file": str(final), "bytes": final.stat().st_size})
    print(json.dumps({"totals": reasons, "per_year": {y: dict(s) for y, s in sorted(per_year.items())},
                      "outputs": outputs}, ensure_ascii=False))



if __name__ == "__main__":
    main()
