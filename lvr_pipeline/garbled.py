#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""造字補正（garbled_override.csv）載入與套用。

公開 API：
    load_garbled(path) → (char_map, token_patterns)
    fix_garbled(addr, char_map, token_patterns) → str
"""
from __future__ import annotations

import csv
import os
import re


# token 規則的 ? 萬用字：比對真實資料裡的 literal ?(U+003F)與 PUA 造字。
# 防污染靠 scope 守門（fix_garbled 跳過 scope 不在地址中的規則），非靠縮小萬用字範圍。
_WILD = "[?" + chr(0xE000) + "-" + chr(0xF8FF) + "]"


def load_garbled(path: str) -> tuple[dict[str, str], list[tuple[re.Pattern, str, str]]]:
    """載入造字對照表。回傳 (char_map, token_patterns)。

    char_map:       {PUA char | variant char → correct char}
    token_patterns: [(compiled pattern, correct string, scope), ...]
                    scope 為空字串表示全域套用。
    """
    char_map: dict[str, str] = {}
    token_patterns: list[tuple[re.Pattern, str, str]] = []
    if not os.path.isfile(path):
        return char_map, token_patterns
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            garbled = row.get("garbled", "").strip()
            correct = row.get("correct", "").strip()
            kind = row.get("kind", "").strip()
            if not garbled or not correct:
                continue
            if kind == "char" and garbled.upper().startswith("U+"):
                try:
                    char_map[chr(int(garbled[2:], 16))] = correct
                except ValueError:
                    pass
            elif kind == "variant":
                char_map[garbled] = correct
            elif kind == "token" and "?" in garbled:
                scope = row.get("scope", "").strip()
                parts = [re.escape(c) if c != "?" else _WILD for c in garbled]
                token_patterns.append((re.compile("".join(parts)), correct, scope))
    return char_map, token_patterns


def fix_garbled(addr: str, char_map: dict[str, str],
                token_patterns: list[tuple[re.Pattern, str, str]]) -> str:
    """套用補正表；回傳補正後地址（無規則時原樣回傳）。"""
    for src, dst in char_map.items():
        addr = addr.replace(src, dst)
    for pat, dst, scope in token_patterns:
        if scope and scope not in addr:
            continue
        addr = pat.sub(dst, addr)
    return addr
