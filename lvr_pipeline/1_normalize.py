#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 1：靜態造字補正 — meta_{sales,presale,rent}.csv raw_address 原地回寫。

Phase 1（靜態補正）：讀 data/work/meta_*.csv，對 raw_address 套用 config/rules/character-fixes.csv
（char/variant/token 三 kind），補正後原地寫回同檔。冪等：重跑不再變動。

自動破字（offline 語料 wildcard + 座標 probe）已移至 Step2 `--mode resolve`。

用法：
  python -m lvr_pipeline.1_normalize            # 全部 meta
  python -m lvr_pipeline.1_normalize sales      # 只跑 meta_sales.csv
"""
from __future__ import annotations

import csv
import os
import sys

from lvr_pipeline.addresses.characters import fix_garbled, load_garbled
from lvr_pipeline.addresses.parse import is_garbled

csv.field_size_limit(sys.maxsize)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WORK_DIR = os.path.join(ROOT, "data", "work")
OFFLINE_IN_DIR = os.path.join(WORK_DIR, "offline_in")
REGISTRY_DIR = os.path.join(ROOT, "data", "registry")
GARBLED_PATH = os.path.join(ROOT, "config", "rules", "character-fixes.csv")

_CATS = ("sales", "presale", "rent")
_SPLIT_COLS = ["id", "raw_address"]


def normalize_meta(meta_path: str, char_map: dict, token_patterns: list) -> int:
    """Phase 1: apply garbled correction to raw_address in meta CSV in-place.

    Returns changed row count.
    """
    if not os.path.isfile(meta_path):
        return 0

    with open(meta_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        return 0

    fieldnames = list(rows[0].keys())
    if "raw_address" not in fieldnames:
        return 0

    changed = 0
    for row in rows:
        original = row["raw_address"]
        corrected = fix_garbled(original, char_map, token_patterns)
        if corrected != original:
            row["raw_address"] = corrected
            changed += 1

    with open(meta_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return changed


def split_meta(meta_path: str, cat: str, out_dir: str = OFFLINE_IN_DIR) -> dict[str, int]:
    """依 is_garbled 把 meta 每筆分流寫出 {cat}_clean.csv / {cat}_garbled.csv（覆寫）。

    兩檔欄位 id, raw_address。回傳 {"clean": N, "garbled": M}。
    """
    if not os.path.isfile(meta_path):
        return {"clean": 0, "garbled": 0}

    with open(meta_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows or "raw_address" not in rows[0]:
        return {"clean": 0, "garbled": 0}

    clean: list[tuple[str, str]] = []
    garbled: list[tuple[str, str]] = []
    for row in rows:
        addr = row["raw_address"]
        pair = (row.get("id", "").strip(), addr)
        (garbled if is_garbled(addr) else clean).append(pair)

    os.makedirs(out_dir, exist_ok=True)
    for name, data in ((f"{cat}_clean.csv", clean), (f"{cat}_garbled.csv", garbled)):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(_SPLIT_COLS)
            w.writerows(data)

    return {"clean": len(clean), "garbled": len(garbled)}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    batch_filter = (argv[0] if argv else "").strip()

    garbled_path = os.environ.get("GARBLED_PATH", GARBLED_PATH)
    char_map, token_patterns = load_garbled(garbled_path)

    cats = [batch_filter] if batch_filter in _CATS else list(_CATS)
    total_changed = 0
    for cat in cats:
        meta_path = os.path.join(WORK_DIR, f"meta_{cat}.csv")
        changed = normalize_meta(meta_path, char_map, token_patterns)
        if os.path.isfile(meta_path):
            split = split_meta(meta_path, cat)
            print(f"[normalize] meta_{cat}.csv  changed={changed:,}  "
                  f"clean={split['clean']:,}  garbled={split['garbled']:,}")
        total_changed += changed

    print(f"[normalize] Phase 1 total: {total_changed:,} corrected")


if __name__ == "__main__":
    main()
