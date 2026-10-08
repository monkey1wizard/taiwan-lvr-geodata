#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R04-4 補字規則真實資料核對（唯讀）。

輸入：
  1. data/tmp/work/fast2/normalized/current.json 指向的正規化快照（observations/*/*.parquet 的 raw_address）。
  2. 舊專案 data/work/offline_in/*_garbled.csv（欄位 id, raw_address；內容是舊規則套用後的值）。
     --include-clean 時另讀 *_clean.csv，涵蓋舊規則已完整修好的列。

輸出：JSON 到 stdout（不寫入 data/ 底下任何位置）。

每條規則回報：
  in_scope        命中且 scope 為空，或 scope 字串在整個地址內（即 fix_garbled 實際會套用的條件）
  out_of_scope    命中但 scope 字串完全不在地址內（不會套用）
  scope_elsewhere in_scope 之中，scope 字串不在行政區字首（縣市+鄉鎮市區）內的命中
新舊比對：以快照 raw_address 為舊流程輸入，用舊專案 garbled.py + 舊規則檔得到 old_engine，
用新版得到 new；再與舊檔案內的值 old_file 比對。
"""
from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import importlib.util
import json
import os
import re
import sys

import pyarrow.parquet as pq

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lvr_pipeline.addresses.characters import fix_garbled, load_garbled  # noqa: E402

RULES = os.path.join(ROOT, "config", "rules", "character-fixes.csv")
NORMALIZED = os.path.join(ROOT, "data", "tmp", "work", "fast2", "normalized")
OLD_PROJECT = "C:/Code/taiwan-lvr-geojson"
CATS = ("sales", "presale", "rent")
NEGATIVE_CASES = ["臺北市士林區天母二路13巷12弄9號"]
MAX_EXAMPLES = 20

_PREFIX = re.compile(r"^.{2,3}?[市縣](?:.{1,3}?[區鄉鎮市])?")
_WILD = "[?" + chr(0xE000) + "-" + chr(0xF8FF) + "]"


def sha256(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def admin_prefix(addr: str) -> str:
    m = _PREFIX.match(addr)
    return addr[: m.end()] if m else addr[:6]


class Rule:
    def __init__(self, row: dict):
        self.garbled = row["garbled"].strip()
        self.correct = row["correct"].strip()
        self.kind = row["kind"].strip()
        self.scope = row["scope"].strip()
        self.status = row["status"].strip()
        if self.kind == "char":
            self.needle = chr(int(self.garbled[2:], 16))
            self.pattern = None
        elif self.kind == "variant":
            self.needle = self.garbled
            self.pattern = None
        else:
            self.needle = None
            self.pattern = re.compile(
                "".join(re.escape(c) if c != "?" else _WILD for c in self.garbled))
        self.label = f"{self.kind}:{self.garbled}"
        self.stat = {"matches": 0, "in_scope": 0, "out_of_scope": 0, "scope_elsewhere": 0}
        self.ex_in: list[str] = []
        self.ex_out: list[str] = []
        self.ex_else: list[str] = []
        self.diff_old_file = 0
        self.diff_old_engine = 0
        self.ex_diff_file: list[dict] = []
        self.ex_diff_engine: list[dict] = []

    def matches(self, addr: str) -> bool:
        if self.pattern is not None:
            return self.pattern.search(addr) is not None
        return self.needle in addr

    def fires(self, addr: str) -> bool:
        """fix_garbled 實際會套用本規則的條件（原始地址上，不含其他規則先改字的連鎖效果）。"""
        return self.matches(addr) and (not self.scope or self.scope in addr)


def load_rules() -> list[Rule]:
    with open(RULES, encoding="utf-8-sig", newline="") as f:
        return [Rule(r) for r in csv.DictReader(f) if r["garbled"].strip() and r["correct"].strip()]


def load_old_engine():
    spec = importlib.util.spec_from_file_location(
        "old_garbled", f"{OLD_PROJECT}/lvr_pipeline/garbled.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    reg = f"{OLD_PROJECT}/data/registry/garbled_override.csv"
    cm, tp = mod.load_garbled(reg)
    return (lambda a: mod.fix_garbled(a, cm, tp)), sha256(reg)


def snapshot_dir() -> str:
    cur = json.load(open(os.path.join(NORMALIZED, "current.json"), encoding="utf-8"))
    return os.path.join(NORMALIZED, "snapshots", cur["snapshot_id"])


def snapshot_rows(snap: str, cat: str):
    """yield (src_batch, source_serial, raw_address)。"""
    for f in sorted(glob.glob(os.path.join(snap, "observations", "*", f"{cat}.parquet"))):
        t = pq.read_table(f, columns=["src_batch", "source_serial", "raw_address"])
        yield from zip(t["src_batch"].to_pylist(), t["source_serial"].to_pylist(),
                       t["raw_address"].to_pylist())


def push(lst: list, item) -> None:
    if len(lst) < MAX_EXAMPLES:
        lst.append(item)


def scan_snapshot(rules: list[Rule], snap: str) -> int:
    total = 0
    for cat in CATS:
        for _b, _s, addr in snapshot_rows(snap, cat):
            total += 1
            if not addr:
                continue
            for r in rules:
                if not r.matches(addr):
                    continue
                r.stat["matches"] += 1
                if r.scope and r.scope not in addr:
                    r.stat["out_of_scope"] += 1
                    push(r.ex_out, addr)
                    continue
                r.stat["in_scope"] += 1
                push(r.ex_in, addr)
                if r.scope and r.scope not in admin_prefix(addr):
                    r.stat["scope_elsewhere"] += 1
                    push(r.ex_else, addr)
    return total


def old_files(include_clean: bool):
    kinds = ("garbled", "clean") if include_clean else ("garbled",)
    for cat in CATS:
        for kind in kinds:
            path = f"{OLD_PROJECT}/data/work/offline_in/{cat}_{kind}.csv"
            if os.path.isfile(path):
                yield cat, kind, path


def compare_old(rules, snap, char_map, token_patterns, old_engine, include_clean):
    out = {"files": {}, "joined": 0, "unjoined": 0, "ambiguous_serial": 0,
           "new_ne_old_file": 0, "new_ne_old_engine": 0, "unattributed_file_diff": 0,
           "unattributed_engine_diff": 0, "old_engine_ne_old_file": 0,
           "garbled_file_rows_changed_by_new_rules": 0}
    ex_unattr: list[dict] = []
    for cat in CATS:
        wanted: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for c, kind, path in old_files(include_clean):
            if c != cat:
                continue
            n = 0
            with open(path, encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    n += 1
                    p = row["id"].split("-")
                    wanted.setdefault((p[0], p[-1]), []).append((kind, row["raw_address"]))
            out["files"][os.path.basename(path)] = n
            if kind == "garbled":
                with open(path, encoding="utf-8-sig", newline="") as f:
                    for row in csv.DictReader(f):
                        a = row["raw_address"]
                        if fix_garbled(a, char_map, token_patterns) != a:
                            out["garbled_file_rows_changed_by_new_rules"] += 1
        seen: set[tuple[str, str]] = set()
        for batch, serial, raw in snapshot_rows(snap, cat):
            key = (batch, serial)
            if key not in wanted:
                continue
            if key in seen:
                out["ambiguous_serial"] += 1
                continue
            seen.add(key)
            new = fix_garbled(raw, char_map, token_patterns)
            eng = old_engine(raw)
            fired = [r for r in rules if r.fires(raw)]
            for kind, old_val in wanted[key]:
                out["joined"] += 1
                if eng != old_val:
                    out["old_engine_ne_old_file"] += 1
                if new != old_val:
                    out["new_ne_old_file"] += 1
                    if not fired:
                        out["unattributed_file_diff"] += 1
                        push(ex_unattr, {"raw": raw, "old_file": old_val, "new": new})
                    for r in fired:
                        r.diff_old_file += 1
                        push(r.ex_diff_file, {"raw": raw, "old_file": old_val, "new": new})
                if new != eng:
                    out["new_ne_old_engine"] += 1
                    if not fired:
                        out["unattributed_engine_diff"] += 1
                    for r in fired:
                        r.diff_old_engine += 1
                        push(r.ex_diff_engine, {"raw": raw, "old_engine": eng, "new": new})
        out["unjoined"] += sum(len(v) for k, v in wanted.items() if k not in seen)
    out["examples_unattributed_file_diff"] = ex_unattr
    return out


def decide(r: Rule) -> tuple[str, str]:
    if r.stat["in_scope"] == 0:
        return "unverified", "快照內範圍內命中數為 0"
    if r.stat["out_of_scope"] > 0:
        return "unverified", f"範圍外命中 {r.stat['out_of_scope']}"
    if r.stat["scope_elsewhere"] > 0:
        return "unverified", f"scope 出現在行政區字首以外 {r.stat['scope_elsewhere']}"
    if r.diff_old_file > 0:
        return "unverified", f"新舊差異 {r.diff_old_file} 筆待人工說明"
    return "candidate", "命中>0、範圍外=0、新舊差異=0"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--include-clean", action="store_true", help="另讀舊 *_clean.csv")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    snap = snapshot_dir()
    rules = load_rules()
    char_map, token_patterns = load_garbled(RULES)
    old_engine, old_hash = load_old_engine()

    total = scan_snapshot(rules, snap)
    cmp_ = compare_old(rules, snap, char_map, token_patterns, old_engine, args.include_clean)

    result = {
        "snapshot": os.path.basename(snap),
        "snapshot_rows": total,
        "rules_sha256": sha256(RULES),
        "old_registry_sha256": old_hash,
        "negative_cases": NEGATIVE_CASES,
        "comparison": cmp_,
        "rules": [],
    }
    for r in rules:
        verdict, why = decide(r)
        result["rules"].append({
            "rule": r.label, "correct": r.correct, "scope": r.scope, "status_now": r.status,
            **r.stat, "diff_vs_old_file": r.diff_old_file, "diff_vs_old_engine": r.diff_old_engine,
            "verdict": verdict, "reason": why,
            "examples_in_scope": r.ex_in, "examples_out_of_scope": r.ex_out,
            "examples_scope_elsewhere": r.ex_else,
            "examples_diff_vs_old_file": r.ex_diff_file,
            "examples_diff_vs_old_engine": r.ex_diff_engine,
        })
    json.dump(result, sys.stdout, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
