#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""地址正規化與棟級鍵計算。

公開介面：
    norm(addr)               → 字串正規化（巿→市, 全形→半形）
    arab_to_cjk(text)        → 街道序數阿拉伯→國字（2段→二段, 24街→二十四街）
    parse(addr)              → (county_code, town, road, tail)

設計原則：
    raw_address 中出現阿拉伯序數（建國路2段）是錯誤輸入。
    官方離線庫使用國字（建國路二段）。
    building_key 統一歸一化到 CJK，使錯誤輸入仍能命中離線庫。
    巷/弄/號 等實體號碼保留阿拉伯數字。
"""
from __future__ import annotations

import re


# ── 縣市代碼表 ────────────────────────────────────────────────────────────────

_COUNTY_CODE: dict[str, str] = {
    "臺北市": "63", "台北市": "63",
    "新北市": "65",
    "桃園市": "68",
    "臺中市": "66", "台中市": "66",
    "臺南市": "67", "台南市": "67",
    "高雄市": "64",
    "基隆市": "10017",
    "新竹市": "10018",
    "嘉義市": "10020",
    "新竹縣": "10004",
    "苗栗縣": "10005",
    "彰化縣": "10007",
    "南投縣": "10008",
    "雲林縣": "10009",
    "嘉義縣": "10010",
    "屏東縣": "10013",
    "宜蘭縣": "10002",
    "花蓮縣": "10015",
    "臺東縣": "10014", "台東縣": "10014",
    "澎湖縣": "10016",
    "金門縣": "09020",
    "連江縣": "09007",
}

# ── 全形→半形 ─────────────────────────────────────────────────────────────────

_FW = str.maketrans(
    "０１２３４５６７８９－",
    "0123456789-",
)

# ── 亂碼偵測（缺字 ? 與 PUA 造字）─────────────────────────────────────────────
# 單一真相：literal "?"(U+003F，上游遺失字替換符號)＋ PUA 私用區(U+E000–U+F8FF)。
# 全管線（normalize 切檔、offline.resolve、prepare_tgos 排除）共用此偵測，勿各自寫死 PUA-only。

GARBLE_RE = re.compile("[?" + chr(0xE000) + "-" + chr(0xF8FF) + "]")


def is_garbled(s: str) -> bool:
    """地址是否含亂碼字（literal ? 或 PUA 造字）。"""
    return bool(GARBLE_RE.search(s))

# ── 非門牌型地址排除 ──────────────────────────────────────────────────────────

_REJECT_RE = re.compile(r"地號|地段|等[0-9]+筆|對面|旁$|附近$|口$")
_JUNCTION_RE = re.compile(r"(?:路|街|巷|道)(?:與|和|及|、)?(?:[^\s]+)?(?:路|街|巷|道)口")
_VAGUE_SUFFIX = re.compile(r"(?:對面|旁|附近|口)$")

# ── 里鄰去除 ──────────────────────────────────────────────────────────────────

_LI_LIN_RE = re.compile(
    r"[^\s市縣區鄉鎮]{1,8}里\s*(?:[0-9０-９]{1,3}鄰\s*)?"
)

# ── 阿拉伯序數 → 國字（路/街/大道/段 前的序數，巷/弄/號 不動）────────────────

_ARAB_ORD_RE = re.compile(r"([0-9]+)(?=(?:路|街|大道|段))")
_CJK_UNITS = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九"]


def _int_to_cjk_ord(n: int) -> str:
    """1–99 整數 → 國字序數。24→二十四, 10→十, 2→二。"""
    if n <= 0 or n >= 100:
        return str(n)
    if n < 10:
        return _CJK_UNITS[n]
    tens, ones = n // 10, n % 10
    return (_CJK_UNITS[tens] if tens > 1 else "") + "十" + _CJK_UNITS[ones]


def arab_to_cjk(text: str) -> str:
    """路/街/大道/段 前的阿拉伯序數 → 國字。巷/弄/號不動。

    "建國路2段" → "建國路二段"
    "精誠24街"  → "精誠二十四街"
    "228巷"     → "228巷"（不動）
    """
    return _ARAB_ORD_RE.sub(lambda m: _int_to_cjk_ord(int(m.group(1))), text)


# ── norm ─────────────────────────────────────────────────────────────────────

def norm(addr: str) -> str:
    """基本字串正規化：全形→半形，巿→市，台→臺（縣市名）。"""
    addr = addr.translate(_FW)
    addr = addr.replace("巿", "市")
    # 四個有台/臺異體的縣市名，以官方離線庫形式為準
    addr = addr.replace("台北", "臺北")
    addr = addr.replace("台中", "臺中")
    addr = addr.replace("台南", "臺南")
    addr = addr.replace("台東", "臺東")
    return addr


# ── 縣市 regex ────────────────────────────────────────────────────────────────

_CITY_RE = re.compile(
    r"^(臺北市|台北市|新北市|桃園市|臺中市|台中市|臺南市|台南市|高雄市"
    r"|基隆市|新竹市|嘉義市"
    r"|新竹縣|苗栗縣|彰化縣|南投縣|雲林縣|嘉義縣|屏東縣"
    r"|宜蘭縣|花蓮縣|臺東縣|台東縣|澎湖縣|金門縣|連江縣)"
    r"(.{2,4}?(?:市|區|鄉|鎮|里))"
)

_CITY_BARE_RE = re.compile(
    r"臺北市|台北市|新北市|桃園市|臺中市|台中市|臺南市|台南市|高雄市"
    r"|基隆市|新竹市|嘉義市"
    r"|新竹縣|苗栗縣|彰化縣|南投縣|雲林縣|嘉義縣|屏東縣"
    r"|宜蘭縣|花蓮縣|臺東縣|台東縣|澎湖縣|金門縣|連江縣"
)

_ROAD_RE = re.compile(
    r"([^\s]+?(?:大道|路|街|巷|弄))"
    r"(?:[一二三四五六七八九十0-9０-９]+段)?"
)

_SECTION_RE = re.compile(r"([一二三四五六七八九十0-9０-９]+)段")


def _dedup_prefix(addr: str) -> str:
    """去除重複行政區前綴（北投區臺北市北投區 → 臺北市北投區）。

    支援三種重複形式（loop 直到穩定）：
      city-repeat:      嘉義市嘉義市…      → 嘉義市…
      city+town-repeat: 新竹縣新埔鎮新竹縣新埔鎮… → 新竹縣新埔鎮…
      triple:           嘉義市嘉義市嘉義市… → 嘉義市…（每輪消一個）
    """
    m = _CITY_BARE_RE.search(addr)
    if not m:
        return addr
    if m.start() > 0:
        addr = addr[m.start():]
    changed = True
    while changed:
        changed = False
        # 優先嘗試 city+town 整體重複（新竹縣新埔鎮新竹縣新埔鎮…）
        m = _CITY_RE.match(addr)
        if m:
            head = addr[:m.end()]
            rest = addr[m.end():]
            if rest.startswith(head):
                addr = head + rest[len(head):]
                changed = True
                continue
        # 再嘗試 bare-city 重複（嘉義市嘉義市…）
        # （_CITY_RE 對 新竹市新竹市 會把第二個市當 town，需 bare-city 分支補足）
        m2 = _CITY_BARE_RE.match(addr)
        if m2:
            city = addr[:m2.end()]
            rest = addr[m2.end():]
            if rest.startswith(city):
                addr = city + rest[len(city):]
                changed = True
    return addr


# ── parse ─────────────────────────────────────────────────────────────────────

def parse(addr: str) -> tuple[str, str, str, str]:
    """addr → (county_code, town, road, tail)。

    tail = 路名之後的原文（含段序和樓層）。
    若無法解析回傳 ("", "", "", addr)。
    """
    addr = norm(addr)
    addr = _dedup_prefix(addr)

    m = _CITY_RE.match(addr)
    if not m:
        return ("", "", "", addr)

    city = m.group(1)
    town = m.group(2)
    rest = addr[m.end():]

    rest = _LI_LIN_RE.sub("", rest)
    county_code = _COUNTY_CODE.get(city, "")

    rm = _ROAD_RE.match(rest)
    if not rm:
        return (county_code, town, "", rest)

    road = rm.group(1)
    tail = rest[rm.end():]

    sec_m = _SECTION_RE.match(rest[len(road):])
    if sec_m:
        tail = rest[len(road):]

    return (county_code, town, road, tail)
