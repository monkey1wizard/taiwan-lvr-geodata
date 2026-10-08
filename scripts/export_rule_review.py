#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R04-10 待確認補字規則的離線查核表（唯讀快照）。

輸入：
  1. data/tmp/work/fast2/normalized/current.json 指向的正規化快照 observations（raw_address 為原始地址）。
  2. 指定的 ingest 快照 records/*/*.parquet（member_path、鄉鎮市區），以 raw_record_id 串接。
輸出（只寫在 data/tmp/review/character-rules/<YYYYMMDD>/，已被 .gitignore 排除）：
  rule_hits.csv  UTF-8 with BOM；每個「規則 x 不同原始地址」一列。
  summary.csv    每條規則的範圍內/範圍外命中數、不同地址數；無命中的規則列 0。

縣市：ZIP 成員檔名首字母對照 LVR 官方縣市代碼表（repo 內無此對照，故內建 COUNTY_BY_LETTER）。
區：原始欄位「鄉鎮市區」。取不到時留空。
補字後地址：只套用該規則本身（不含其他規則的連鎖效果）；範圍外時等於原始地址。
"""
from __future__ import annotations

import argparse
import csv
import datetime
import glob
import json
import os
import sys
from collections import defaultdict

import pyarrow.parquet as pq

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from audit_character_fixes import (  # noqa: E402
    CATS, ROOT, Rule, load_rules, snapshot_dir, snapshot_rows)

INGEST = os.path.join(ROOT, "data", "tmp", "work", "ingested", "snapshots", "fast-all58-v3-ingest")
OUT_BASE = os.path.join(ROOT, "data", "tmp", "review", "character-rules")

COUNTY_BY_LETTER = {
    "a": "臺北市", "b": "臺中市", "c": "基隆市", "d": "臺南市", "e": "高雄市", "f": "新北市",
    "g": "宜蘭縣", "h": "桃園市", "i": "嘉義市", "j": "新竹縣", "k": "苗栗縣", "m": "南投縣",
    "n": "彰化縣", "o": "新竹市", "p": "雲林縣", "q": "嘉義縣", "t": "屏東縣", "u": "花蓮縣",
    "v": "臺東縣", "w": "金門縣", "x": "澎湖縣", "z": "連江縣",
}
HIT_FIELDS = ["規則_原文", "規則_修正", "規則_範圍", "規則_狀態", "是否套用", "縣市", "區",
              "原始地址", "補字後地址", "出現次數", "批次清單", "類別清單", "範例raw_record_id"]
SUM_FIELDS = ["規則_原文", "規則_修正", "規則_範圍", "規則_狀態", "範圍內命中", "範圍外命中", "不同地址數"]


def apply_one(rule: Rule, addr: str) -> str:
    if rule.scope and rule.scope not in addr:
        return addr
    if rule.pattern is not None:
        return rule.pattern.sub(rule.correct, addr)
    return addr.replace(rule.needle, rule.correct)


def select_rules(rules: list[Rule]) -> list[Rule]:
    return [r for r in rules if r.status == "unverified" or r.garbled == "東洋新?"]


def collect(rules: list[Rule], snap: str):
    """hits[(rule_idx, addr)] = {count, batches, cats, ids[(batch,cat)->first id], in_scope}"""
    hits: dict = {}
    for cat in CATS:
        for f in sorted(glob.glob(os.path.join(snap, "observations", "*", f"{cat}.parquet"))):
            t = pq.read_table(f, columns=["raw_record_id", "src_batch", "raw_address"])
            for rid, batch, addr in zip(t["raw_record_id"].to_pylist(), t["src_batch"].to_pylist(),
                                        t["raw_address"].to_pylist()):
                if not addr:
                    continue
                for i, r in enumerate(rules):
                    if not r.matches(addr):
                        continue
                    h = hits.setdefault((i, addr), {"count": 0, "batches": set(), "cats": set(),
                                                    "rid": rid, "src": (batch, cat)})
                    h["count"] += 1
                    h["batches"].add(batch)
                    h["cats"].add(cat)
    return hits


def lookup_raw(hits: dict) -> dict[str, tuple[str, str]]:
    """raw_record_id -> (member_path, 鄉鎮市區)，只讀範例列。"""
    need: dict[tuple[str, str], set[str]] = defaultdict(set)
    for h in hits.values():
        need[h["src"]].add(h["rid"])
    out: dict[str, tuple[str, str]] = {}
    for (batch, cat), ids in need.items():
        f = os.path.join(INGEST, "records", batch, f"{cat}.parquet")
        t = pq.read_table(f, columns=["raw_record_id", "member_path", "raw_fields_json"])
        for rid, mp, js in zip(t["raw_record_id"].to_pylist(), t["member_path"].to_pylist(),
                               t["raw_fields_json"].to_pylist()):
            if rid in ids:
                try:
                    district = (json.loads(js).get("鄉鎮市區") or "").strip()
                except (ValueError, AttributeError):
                    district = ""
                out[rid] = (mp or "", district)
    return out


def county_of(member_path: str) -> str:
    base = os.path.basename(member_path or "")
    return COUNTY_BY_LETTER.get(base[:1].lower(), "") if len(base) > 1 and base[1] == "_" else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.date.today().strftime("%Y%m%d"))
    args = ap.parse_args()
    rules = select_rules(load_rules())
    snap = snapshot_dir()
    hits = collect(rules, snap)
    raw = lookup_raw(hits)
    outdir = os.path.join(OUT_BASE, args.date)
    os.makedirs(outdir, exist_ok=True)

    rows = []
    stats = {i: {"in": 0, "out": 0, "addr": set()} for i in range(len(rules))}
    for (i, addr), h in hits.items():
        r = rules[i]
        applied = not r.scope or r.scope in addr
        stats[i]["in" if applied else "out"] += h["count"]
        stats[i]["addr"].add(addr)
        mp, district = raw.get(h["rid"], ("", ""))
        county = county_of(mp)
        rows.append({
            "規則_原文": r.garbled, "規則_修正": r.correct, "規則_範圍": r.scope, "規則_狀態": r.status,
            "是否套用": "是" if applied else "否", "縣市": county, "區": district,
            "原始地址": addr, "補字後地址": apply_one(r, addr), "出現次數": h["count"],
            "批次清單": ";".join(sorted(h["batches"])), "類別清單": ";".join(sorted(h["cats"])),
            "範例raw_record_id": h["rid"]})
    rows.sort(key=lambda x: (x["規則_原文"], x["縣市"], x["區"], x["原始地址"]))
    with open(os.path.join(outdir, "rule_hits.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HIT_FIELDS)
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(outdir, "summary.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SUM_FIELDS)
        w.writeheader()
        for i, r in enumerate(rules):
            s = stats[i]
            w.writerow({"規則_原文": r.garbled, "規則_修正": r.correct, "規則_範圍": r.scope,
                        "規則_狀態": r.status, "範圍內命中": s["in"], "範圍外命中": s["out"],
                        "不同地址數": len(s["addr"])})
    no_county = sum(1 for x in rows if not x["縣市"])
    no_district = sum(1 for x in rows if not x["區"])
    print(json.dumps({"outdir": outdir, "snapshot": snap, "rule_hit_rows": len(rows),
                      "blank_county_rows": no_county, "blank_district_rows": no_district},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
