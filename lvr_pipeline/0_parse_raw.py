#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 0：raw LVR zip → meta_{sales,rent,presale}.csv + land.csv + parking.csv。

只做類別分流與結構展開：
  - 成屋(a)/預售屋(b)/租賃(c) → meta_{cat}.csv
  - 純土地/車位 → registry/land.csv、parking.csv（不進地理編碼）
  - 地號型地址（含「地號」「地段」「等N筆」）→ land.csv

raw_address = 土地位置建物門牌 zip 原值（含 PUA 造字）；
造字補正由 Step 1（1_normalize.py）負責。
多門牌偵測（頓號清單 / N-M號區間）僅做展開以建立 id/group_key 結構，
不對地址內容做任何正規化。

用法：
  python -m lvr_pipeline.0_parse_raw            # 全部 zip
  python -m lvr_pipeline.0_parse_raw 115q1      # 只跑 115q1
"""
from __future__ import annotations

import csv
import datetime
import glob
import io
import os
import re
import sys
import zipfile

from lvr_pipeline.addresses.parse import _CITY_RE as _ADDR_CITY_RE
from lvr_pipeline.tx_date import roc_to_tx_yyyymm

csv.field_size_limit(sys.maxsize)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RAW_DIR = os.path.join(ROOT, "data", "raw")
WORK_DIR = os.path.join(ROOT, "data", "work")
REGISTRY_DIR = os.path.join(ROOT, "data", "registry")

CATS = {"sales": "a", "presale": "b", "rent": "c"}

DATE_COL = {"sales": "交易年月日", "presale": "交易年月", "rent": "租賃年月日"}
AMOUNT_COL = {"sales": "總價元", "presale": "房地總價元", "rent": "月租金"}
AREA_COL = {"sales": "建物移轉總面積平方公尺", "presale": "建物移轉總面積平方公尺", "rent": "建物移轉總面積平方公尺"}

META_PREFIX = ["id", "src_batch", "tx_yyyymm", "group_key", "raw_address"]
LAND_PARK_COLS = ["id", "category", "src_batch", "交易標的", "raw_address", "tx_date", "amount", "area"]

# ── 多門牌偵測（不做正規化，僅用於展開結構）──────────────────────────────────

_FW = str.maketrans("０１２３４５６７８９", "0123456789")
_LIST_SEP = re.compile(r"[,，、；;]")
_NUMBER_END = re.compile(r"號\s*$")
_RANGE_RE = re.compile(r"^(.+?)([0-9０-９]+)[-－–]([0-9０-９]+)號\s*$")
_LAND_RE = re.compile(r"地號|地段|等[0-9０-９]+筆")


def _expand_list(addr: str) -> list[str] | None:
    """頓號/逗號多門牌清單 → 各自完整地址；非清單回 None。"""
    if not _LIST_SEP.search(addr) or not _NUMBER_END.search(addr):
        return None
    body = addr.rstrip().rstrip("號").rstrip()
    parts = _LIST_SEP.split(body)
    if len(parts) < 2:
        return None
    sep_m = _LIST_SEP.search(body)
    if sep_m is None:
        return None
    first_token = body[:sep_m.start()]
    # 路名前綴：到路/街/巷/弄/道之後（含段序）
    m = re.match(
        r"^(.*?(?:路|街|巷|弄|道|大道)(?:[一二三四五六七八九十0-9０-９]+段)?)"
        r"([0-9０-９]+(?:之[0-9０-９]+)?)$",
        first_token,
    )
    if not m:
        return None
    prefix, first_num = m.group(1), m.group(2)
    result = [prefix + first_num + "號"]
    city_town = ""
    cm = _ADDR_CITY_RE.match(prefix)
    if cm:
        city_town = prefix[:cm.end()]
    for part in parts[1:]:
        part = part.strip()
        if not part:
            continue
        if re.search(r"(?:路|街|道)", part):
            # RC-6: 道路變換片段 — 去尾號後補縣市區前綴（若缺）
            part_clean = part.rstrip("號")
            if city_town and not _ADDR_CITY_RE.match(part_clean):
                result.append(city_town + part_clean + "號")
            else:
                result.append(part_clean + "號")
        else:
            # RC-5: 同路名繼續 — 去尾號後加前綴（巷/弄亦繼承完整前綴）
            result.append(prefix + part.rstrip("號") + "號")
    return result if len(result) >= 2 else None


def _classify(addr: str) -> tuple[str, list[str]]:
    """(kind, [地址...])；kind ∈ single/list/range/land。"""
    if not addr:
        return ("single", [addr])
    if _LAND_RE.search(addr):
        return ("land", [])
    parts = _expand_list(addr)
    if parts:
        return ("list", parts)
    m = _RANGE_RE.match(addr)
    if m:
        lo = int(m.group(2).translate(_FW))
        hi = int(m.group(3).translate(_FW))
        if hi > lo:
            return ("range", [m.group(1) + m.group(2) + "號",
                               m.group(1) + m.group(3) + "號"])
    return ("single", [addr])


# ── ZIP 讀取 ─────────────────────────────────────────────────────────────────

def batch_of(zip_path: str) -> str:
    m = re.match(r"(\d+q\d)", os.path.basename(zip_path))
    return m.group(1) if m else os.path.basename(zip_path)


def county_of(inner_name: str) -> str:
    m = re.match(r"^([a-z]+)_lvr_land_", os.path.basename(inner_name))
    return m.group(1) if m else "?"


def main_csvs(zf: zipfile.ZipFile, suffix: str):
    pat = re.compile(rf"^[a-z]+_lvr_land_{suffix}\.csv$")
    for n in sorted(x for x in zf.namelist() if pat.match(os.path.basename(x))):
        rows = list(csv.reader(zf.read(n).decode("utf-8-sig", "replace").splitlines()))
        if len(rows) >= 3:
            yield n, rows[0], rows[2:]


# ── 主程式 ────────────────────────────────────────────────────────────────────

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    batch_filter = (argv[0] if argv else os.environ.get("LVR_BATCH", "")).strip()

    zips = sorted(glob.glob(os.path.join(RAW_DIR, "*lvr_landcsv.zip")))
    if batch_filter:
        zips = [z for z in zips if batch_of(z) == batch_filter]
    if not zips:
        sys.exit(f"[FATAL] data/raw/ 無符合 zip（filter={batch_filter!r}）")

    os.makedirs(WORK_DIR, exist_ok=True)
    os.makedirs(REGISTRY_DIR, exist_ok=True)
    max_yyyymm = _current_yyyymm()

    meta_f: dict = {}
    meta_w: dict = {}
    meta_hdr: dict = {}
    meta_count = {c: 0 for c in CATS}
    seq = {c: 0 for c in CATS}
    land_n = park_n = 0
    land_f = None
    park_f = None

    try:
        land_f = io.open(os.path.join(REGISTRY_DIR, "land.csv"), "w", encoding="utf-8-sig", newline="")
        park_f = io.open(os.path.join(REGISTRY_DIR, "parking.csv"), "w", encoding="utf-8-sig", newline="")
        land_w = csv.writer(land_f)
        park_w = csv.writer(park_f)
        land_w.writerow(LAND_PARK_COLS)
        park_w.writerow(LAND_PARK_COLS)

        for zp in zips:
            src_batch = batch_of(zp)
            print(f"[parse] {os.path.basename(zp)}")
            with zipfile.ZipFile(zp) as zf:
                for cat, suffix in CATS.items():
                    date_col = DATE_COL[cat]
                    amt_col = AMOUNT_COL[cat]
                    area_col = AREA_COL[cat]

                    for name, hdr, data in main_csvs(zf, suffix):
                        if "土地位置建物門牌" not in hdr or "交易標的" not in hdr:
                            continue
                        county = county_of(name)

                        if cat not in meta_hdr:
                            meta_hdr[cat] = META_PREFIX + hdr
                            f = io.open(
                                os.path.join(WORK_DIR, f"meta_{cat}.csv"),
                                "w", encoding="utf-8-sig", newline="",
                            )
                            meta_f[cat] = f
                            meta_w[cat] = csv.writer(f)
                            meta_w[cat].writerow(meta_hdr[cat])

                        base_hdr = meta_hdr[cat][len(META_PREFIX):]

                        for r in data:
                            d = dict(zip(hdr, r))
                            raw_addr = d.get("土地位置建物門牌", "")
                            tval = d.get("交易標的", "")
                            no = (d.get("編號", "") or "").strip()
                            seq[cat] += 1
                            cid = (f"{src_batch}-{county}-{no}" if no
                                   else f"{src_batch}-{county}-{seq[cat]}")
                            ty = roc_to_tx_yyyymm(d.get(date_col, ""), max_yyyymm)
                            ty_s = str(ty) if ty is not None else ""
                            ordered_raw = [d.get(c, "") for c in base_hdr]

                            # 純土地 / 車位 → registry
                            if tval in ("土地", "車位"):
                                row = [cid, cat, src_batch, tval, raw_addr,
                                       d.get(date_col, ""), d.get(amt_col, ""),
                                       d.get(area_col, "")]
                                if tval == "土地":
                                    land_w.writerow(row)
                                    land_n += 1
                                else:
                                    park_w.writerow(row)
                                    park_n += 1
                                continue

                            kind, exp_addrs = _classify(raw_addr)

                            # 地號型 → land registry
                            if kind == "land":
                                land_w.writerow([cid, cat, src_batch, "地號", raw_addr,
                                                 d.get(date_col, ""), d.get(amt_col, ""),
                                                 d.get(area_col, "")])
                                land_n += 1
                                continue

                            # 多門牌展開
                            if kind in ("list", "range"):
                                for i, ea in enumerate(exp_addrs):
                                    meta_w[cat].writerow(
                                        [f"{cid}#{i}", src_batch, ty_s, cid, ea]
                                        + ordered_raw
                                    )
                                    meta_count[cat] += 1
                            else:
                                addr_out = exp_addrs[0] if exp_addrs else raw_addr
                                meta_w[cat].writerow(
                                    [cid, src_batch, ty_s, cid, addr_out]
                                    + ordered_raw
                                )
                                meta_count[cat] += 1
    finally:
        for f in meta_f.values():
            f.close()
        if land_f is not None:
            land_f.close()
        if park_f is not None:
            park_f.close()

    print("[meta] 結果：")
    for cat in CATS:
        n = meta_count[cat]
        print(f"  meta_{cat}.csv  {n:>9,} 筆")
    print(f"[registry] land.csv {land_n:,} 筆 / parking.csv {park_n:,} 筆")


def _current_yyyymm() -> int:
    d = datetime.date.today()
    return d.year * 100 + d.month


if __name__ == "__main__":
    main()
