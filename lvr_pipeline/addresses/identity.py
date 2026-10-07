"""Building identity: conservative v2 door key (legacy keys are never accepted as evidence) and the legacy key."""
from __future__ import annotations

import json
import re

from .parse import (_CITY_RE, _COUNTY_CODE, _dedup_prefix, _LI_LIN_RE, _REJECT_RE, _JUNCTION_RE, _VAGUE_SUFFIX,
                    arab_to_cjk, is_garbled, norm)

KEY_VERSION = "v2"
# Bump whenever a change alters building_key_v2 output for some input. Recorded in the bindings of the
# normalize, address-pool and offline-index stages so snapshots made under older rules are not reused.
NORMALIZATION_VERSION = "v2.2"
_DOOR = re.compile(r"(?P<main>[0-9]+)(?P<before>(?:之[0-9]+)*)號(?P<after>(?:之[0-9]+)*)(?P<floor>.*)$")
_FLOOR = re.compile(r"(?:[0-9一二三四五六七八九十百千]+樓(?:之[0-9一二三四五六七八九十]+)?|地下[0-9一二三四五六七八九十]+樓)?$")
_ROAD = re.compile(r"(?:大道|路|街)")
_REPEATED_COUNTY = re.compile("^(" + "|".join(sorted(_COUNTY_CODE, key=len, reverse=True)) + r")\1+")


def building_key_v2(address: str) -> str | None:
    if not isinstance(address, str) or not address.strip():
        return None
    text = arab_to_cjk(norm(address.strip()))
    if is_garbled(text) or _REJECT_RE.search(text) or text.count("號") != 1:
        return None
    # Hyphens can represent ranges or subnumbers; neither is guessed.
    if any(c in text for c in "-至~～、及與") or re.search(r"\s", text):
        return None
    # Owner decision (R04-6): an exactly repeated county name is dropped; any other odd start is rejected.
    text = _REPEATED_COUNTY.sub(lambda m: m.group(1), text, count=1)
    region = _CITY_RE.match(text)
    if not region or region.group(2).endswith("里") or region.group(2) in _COUNTY_CODE:
        return None
    rest = text[region.end():]
    door = _DOOR.search(rest)
    if not door or not _FLOOR.fullmatch(door.group("floor")):
        return None
    if door.group("before") and door.group("after"):
        return None
    locality_road = rest[:door.start()]
    if not locality_road:
        return None
    if _ROAD.search(locality_road):
        if not locality_road.endswith(("路", "街", "道", "段", "巷", "弄")):
            return None
        # Village/neighborhood are redundant only when a road is present.
        locality_road = re.sub(r"^[^0-9路街巷弄]+?[里村](?:[0-9]+鄰)?", "", locality_road)
    # Without a road, retain village/locality so two villages never collapse.
    identity = {
        "county": _COUNTY_CODE[region.group(1)],
        "town": region.group(2),
        "locality_road": locality_road,
        "door": door.group("main") + (door.group("before") or door.group("after")),
    }
    return "v2:" + json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ── building_key ──────────────────────────────────────────────────────────────

_NUM_END_RE = re.compile(r"號")
_FLOOR_RE = re.compile(r"(?:[一二三四五六七八九十百千0-9]+樓.*|[A-Za-z]棟.*)$")
_YI_TO_ZHI_RE = re.compile(r"([0-9]+)一([0-9]+號)")
_DASH_ZHI_RE = re.compile(r"([0-9]+)-([0-9]+)號")


def building_key(addr: str) -> str:
    """棟級唯一鍵（CJK 正規式）。

    - 全形→半形後，路/街/段前阿拉伯序數 → 國字（arab_to_cjk）
    - 去里鄰、截到「號」、去樓層
    - 非門牌（地號/路口/對面/旁/無「號」）→ ""
    """
    if not addr:
        return ""

    addr = norm(addr)           # 全形→半形
    addr = arab_to_cjk(addr)   # 2段→二段, 24街→二十四街（錯誤輸入修正）
    addr = _dedup_prefix(addr)

    if _REJECT_RE.search(addr):
        return ""
    if _JUNCTION_RE.search(addr):
        return ""
    if _VAGUE_SUFFIX.search(addr):
        return ""

    m = _CITY_RE.match(addr)
    if m:
        city_town = addr[:m.end()]
        rest = addr[m.end():]
        rest = _LI_LIN_RE.sub("", rest)
        addr = city_town + rest

    # 之/一誤植：3一3號 → 3之3號
    addr = _YI_TO_ZHI_RE.sub(r"\1之\2", addr)

    # N-M號 且 M<N → N之M號（子門牌，非多門牌區間）
    def _fix_dash(mo: re.Match) -> str:
        n, sub = int(mo.group(1)), int(mo.group(2))
        if sub < n:
            return f"{n}之{sub}號"
        return mo.group()

    addr = _DASH_ZHI_RE.sub(_fix_dash, addr)

    last_num = None
    for mo in _NUM_END_RE.finditer(addr):
        last_num = mo
    if last_num is None:
        return ""

    addr = addr[:last_num.end()]
    addr = _FLOOR_RE.sub("", addr)

    if not addr.endswith("號"):
        return ""

    return addr
