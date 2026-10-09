"""Building identity: conservative v2 door key (legacy keys are never accepted as evidence) and the legacy key."""
from __future__ import annotations

import json
import re

from .parse import (_CITY_RE, _COUNTY_CODE, _dedup_prefix, _LI_LIN_RE, _REJECT_RE, _JUNCTION_RE, _VAGUE_SUFFIX,
                    arab_to_cjk, is_garbled, norm)

KEY_VERSION = "v2"
# Bump whenever a change alters building_key_v2 output for some input. Recorded in the bindings of the
# normalize, address-pool and offline-index stages so snapshots made under older rules are not reused.
# v2.5 (R04-12): village and neighbourhood become sub-identifiers of the door; see building_key_v2.
NORMALIZATION_VERSION = "v2.5"
_COUNTIES = "|".join(sorted(_COUNTY_CODE, key=len, reverse=True))
# Town names end in 市, 區, 鄉 or 鎮, never 里 (R04-12: 太麻里鄉 was rejected by the old 里 alternative).
_REGION_RE = re.compile("^(" + _COUNTIES + r")([東西南北中]區|.{2,4}?(?:市|區|鄉|鎮))")
_REPEATED_COUNTY = re.compile("^(" + _COUNTIES + r")\1+")
_CJK_DIGITS = "一二三四五六七八九十百"
_DOOR = re.compile(
    r"(?P<pre>[臨建特])?(?P<main>[0-9]+[A-Z]?)(?P<before>(?:[之附][0-9]+[A-Z]?)*)號"
    r"(?P<after>(?:[之附][0-9]+[A-Z]?)*)(?P<floor>.*)$"
)
_FLOOR_NUMBER = "[0-9一二三四五六七八九十百壹貳參肆伍陸柒捌玖拾]+"
_ROOM = "[0-9A-Z一二三四五六七八九十甲乙丙丁東西南北]+室"
# Floor, room and basement text after the door never takes part in the door identity.
_FLOOR = re.compile(
    rf"(?:(?:地下室|地下[0-9一二三四五六七八九十]*[樓層]|底層|{_FLOOR_NUMBER}樓)"
    rf"(?:之?[0-9一二三四五六七八九十]+號?)?(?:{_ROOM})?|{_ROOM})?"
)
# 至, 及 and 與 mean a range or several doors only after a number or an address unit.
_RANGE_WORD = re.compile(r"[0-9號樓巷弄路街道段][至及與]")
_BRACKET = re.compile(r"\([^()]*\)|（[^（）]*）")
_NEIGHBORHOOD = re.compile(r"^(?P<village>[^0-9]{0,6}?)(?P<number>[0-9]+|[一二三四五六七八九十]+)鄰")
# After a village written without 鄰, the text must not continue a road name (美村路, 美村南路, 大里大道, 中里一街).
_ROAD_CONTINUES = re.compile(r"^(?:[東西南北中]?[一二三四五六七八九十0-9]*(?:路|街|大道|段)|巷|弄|村|里)")
_FULLWIDTH_LETTERS = str.maketrans(
    {chr(0xFF21 + i): chr(0x41 + i) for i in range(26)}
    | {chr(0xFF41 + i): chr(0x41 + i) for i in range(26)}
)
_CJK_DOOR = re.compile(r"(?<![0-9一二三四五六七八九十百千])([一二三四五六七八九十百]+)(?=(?:之[0-9]+)*號)")
_UNIT = re.compile("[路街道段巷弄衖衕]")


def cjk_number(text: str) -> int | None:
    """一 to 九百九十九 written with 十 and 百; None for any other text."""
    if not text or any(c not in _CJK_DIGITS for c in text):
        return None
    digits = {c: i for i, c in enumerate("一二三四五六七八九", 1)}
    total, current, last_unit = 0, 0, 1000
    for c in text:
        if c in digits:
            if current:
                return None
            current = digits[c]
        else:
            unit = 10 if c == "十" else 100
            if unit >= last_unit:
                return None
            total += (current or 1) * unit
            current, last_unit = 0, unit
    return total + current


def drop_repeated_county(text: str) -> str:
    """Drop an exactly repeated county name at the start (owner decision R04-6).

    Shared by building_key_v2 and AdministrativeNames.canonicalize (R05-7), so both read
    新竹市新竹市東區… as 新竹市東區…. Only an identical repeat is dropped (台北市臺北市 is kept).
    """
    return _REPEATED_COUNTY.sub(lambda m: m.group(1), text, count=1)


def _split_village(rest: str):
    """(valid, village, neighbourhood, remainder) for the address text after the town."""
    match = _NEIGHBORHOOD.match(rest)
    if match:
        village = match.group("village")
        if village and (village[-1] not in "村里" or len(village) < 2):
            return False, None, None, rest
        number = match.group("number")
        value = int(number) if number.isdigit() else cjk_number(number)
        if value is None:
            return False, None, None, rest
        return True, village or None, str(value), rest[match.end():]
    for end in range(2, 6):
        if end >= len(rest):
            break
        prefix = rest[:end]
        if prefix[-1] not in "村里" or re.search(r"[0-9巷弄段號]", prefix):
            continue
        if _ROAD_CONTINUES.match(rest[end:]):
            continue
        return True, prefix, None, rest[end:]
    return True, None, None, rest


def building_key_v2(address: str) -> str | None:
    """Door identity: county, town, street or place with lanes, door; plus written village and neighbourhood.

    R04-12 (owner-approved rule of 2026-10-09): floors, rooms and basements are not part of the door.
    A village and a neighbourhood written in the address are kept as separate fields, so two doors that
    differ only in village or neighbourhood never share a key. Notations are never merged: 10之1號 and
    10號之1, 30附1號 and 30號附1, 十二巷 and 12巷 give different keys. Bracketed notes are ignored.
    """
    if not isinstance(address, str) or not address.strip():
        return None
    text = arab_to_cjk(norm(address.strip())).translate(_FULLWIDTH_LETTERS)
    text = _BRACKET.sub("", text)
    if is_garbled(text) or _REJECT_RE.search(text):
        return None
    # Hyphens can represent ranges or subnumbers; neither is guessed.
    if any(c in text for c in "-~～、") or re.search(r"\s", text) or _RANGE_WORD.search(text):
        return None
    # Owner decision (R04-6): an exactly repeated county name is dropped; any other odd start is rejected.
    text = drop_repeated_county(text)
    region = _REGION_RE.match(text)
    if not region or region.group(2) in _COUNTY_CODE:
        return None
    ok, village, neighborhood, rest = _split_village(text[region.end():])
    if not ok:
        return None
    rest = _CJK_DOOR.sub(lambda m: str(cjk_number(m.group(1)) or m.group(1)), rest)
    door = _DOOR.search(rest)
    if not door or not _FLOOR.fullmatch(door.group("floor")):
        return None
    if door.group("before") and door.group("after"):
        return None
    locality_road = rest[:door.start()]
    if "號" in locality_road or "鄰" in locality_road:
        return None
    # A number not followed by a unit (測試路10一1號) is an unparsed door, not a place name.
    trailing = re.search(r"[0-9]([^0-9]*)$", locality_road)
    if trailing and not _UNIT.search(trailing.group(1)):
        return None
    if not locality_road and not village:
        return None
    identity = {
        "county": _COUNTY_CODE[region.group(1)],
        "town": region.group(2),
        "locality_road": locality_road,
        "door": (door.group("pre") or "") + door.group("main")
        + (door.group("before") or ("號" + door.group("after") if door.group("after") else "")),
    }
    if village:
        identity["village"] = village
    if neighborhood:
        identity["neighborhood"] = neighborhood
    return "v2:" + json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def door_parts(key: str) -> tuple[str, str | None, str | None]:
    """(door signature, village, neighbourhood) of a building_key_v2 key.

    The signature joins county, town, locality_road and door with U+001F; SQL builds the same
    string with DOOR_SIGNATURE_SQL.
    """
    value = json.loads(key[3:])
    signature = "\x1f".join([value["county"], value["town"], value["locality_road"], value["door"]])
    return signature, value.get("village"), value.get("neighborhood")


def door_signature_sql(column: str) -> str:
    """DuckDB expression giving door_parts(key)[0] for a building_key column."""
    parts = ", ".join(
        f"json_extract_string(substr({column}, 4), '$.{name}')"
        for name in ("county", "town", "locality_road", "door")
    )
    return f"concat_ws(chr(31), {parts})"


def sub_identity_sql(column: str, name: str) -> str:
    """DuckDB expression giving the village or neighborhood field (NULL when not written)."""
    return f"json_extract_string(substr({column}, 4), '$.{name}')"


def same_door(query_key: str | None, response_key: str | None) -> bool:
    """A response names the queried door: same signature, and no written village or neighbourhood differs.

    A part the query does not write may be present in the response (TGOS usually adds the 鄰).
    """
    if not query_key or not response_key:
        return False
    query, response = door_parts(query_key), door_parts(response_key)
    if query[0] != response[0]:
        return False
    return all(a is None or a == b for a, b in zip(query[1:], response[1:]))


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
