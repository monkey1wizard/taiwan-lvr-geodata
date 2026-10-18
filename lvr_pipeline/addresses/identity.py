"""Building identity: the conservative door key (building_key)."""
from __future__ import annotations

import json
import re

from .admin import OLD_COUNTY_PAIRS, county_name, resolve_old_county, town_aliases, town_in_county, town_villages
from .parse import _COUNTY_CODE, _REJECT_RE, arab_to_cjk, is_garbled, norm

KEY_VERSION = "v2"
# Bump whenever a change alters building_key output for some input. Recorded in the bindings of the
# normalize, address-pool and offline-index stages so snapshots made under older rules are not reused.
# v2.5 (R04-12): village and neighbourhood become sub-identifiers of the door; see building_key.
# v2.6 (R07-6b): district must exist in its county; old county names mapped by admin_towns.json.
# v2.7 (B-1): repeated town and unit dropped, other town invalid, CJK main number before 之N號, unparsable floor door gives no key.
# v2.8 (B-1 follow-up): repeated county and town dropped.
NORMALIZATION_VERSION = "v2.8"
_COUNTIES = "|".join(sorted(_COUNTY_CODE, key=len, reverse=True))
# Town names end in 市, 區, 鄉 or 鎮, never 里 (R04-12: 太麻里鄉 was rejected by the old 里 alternative).
_COUNTY_RE = re.compile("^(" + _COUNTIES + ")")
_REPEATED_COUNTY = re.compile("^(" + _COUNTIES + r")\1+")
_CJK_DIGITS = "一二三四五六七八九十百"
_DOOR = re.compile(
    r"(?P<pre>[臨建特])?(?P<main>[0-9]+[A-Z]?)(?P<before>(?:[之附][0-9]+[A-Z]?)*)號"
    r"(?P<after>(?:[之附][0-9]+[A-Z]?)*)(?P<floor>.*)$"
)
_FLOOR_NUMBER = "[0-9一二三四五六七八九十百壹貳參肆伍陸柒捌玖拾]+"
_ROOM = "[0-9A-Z一二三四五六七八九十甲乙丙丁東西南北]+室"
# Floor, room and basement text after the door never takes part in the door identity.
_FLOOR_TOKEN = rf"(?:地下室|地下[0-9一二三四五六七八九十]*[樓層]|{_FLOOR_NUMBER}樓)"
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
# A floor between the road and the door stays in locality_road (大順二樓189號). Only an unparsable door gives no
# key (B-1): a floor followed by 之 (二一五之六樓之1號) or a floor token right after a digit (80十樓，…).
_UNPARSABLE_FLOOR_DOOR = re.compile(rf"{_FLOOR_TOKEN}之$|[0-9]{_FLOOR_TOKEN}")
# A CJK main number and any CJK sub-numbers of a door written 七之一號 or 七之1號 (B-1: 之 may be followed by CJK too).
_CJK_DOOR = re.compile(
    r"(?<![0-9一二三四五六七八九十百千])(?P<main>[一二三四五六七八九十百]+)"
    r"(?P<subs>(?:之(?:[0-9]+|[一二三四五六七八九十百]+))*)(?=號)"
)
_CJK_RUN = re.compile("[一二三四五六七八九十百]+")
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

    Shared by building_key and AdministrativeNames.canonicalize (R05-7), so both read
    新竹市新竹市東區… as 新竹市東區…. Only an identical repeat is dropped. The text is read after
    norm (台 becomes 臺), so an exact repeat is judged on normalized text: 台北市臺北市 is dropped too.
    """
    return _REPEATED_COUNTY.sub(lambda m: m.group(1), text, count=1)


def _split_town(text: str):
    """(county code, town, remainder) when the town exists in the county written first, else None.

    The county is decided first and the town is checked only inside it (owner decision R07-6b).
    Candidates grow from the shortest, so 北區 wins over a longer string that also ends in a suffix.
    """
    county = _COUNTY_RE.match(text)
    if not county:
        return None
    code = _COUNTY_CODE[county.group(1)]
    rest = text[county.end():]
    for end in range(2, 6):
        if end > len(rest):
            break
        town = rest[:end]
        if town[-1] in "市區鄉鎮" and town_in_county(code, town):
            return code, town, rest[end:]
    return None


def split_county_town(text: str):
    """(county code, town name, remainder) of a normalized address, or None.

    One parser for building_key and the address character fixes (R07-6d): an exactly repeated county
    is dropped, an old county name is mapped (桃園縣桃園市 is read as 桃園市桃園區), then the town must
    exist in that county. The town name is the one written after mapping.
    """
    text = resolve_old_county(drop_repeated_county(text))
    if text is None:
        return None
    return _split_town(text)


def after_town(county_code: str, town: str, rest: str) -> str | None:
    """The address text after the town with a repeated town or unit dropped; None when the town is a different one.

    B-1 (owner-approved 2026-10-18). Shared by building_key and AdministrativeNames.canonicalize, so the pool,
    the district fill and the keys agree. 市 plus the same town (臺南市東區市東區) and a different town of the
    county (苗栗市公館鄉) are not readable (invalid_admin). The same town again, or a name the admin tables
    record for the same town code (頭份市頭份鎮, 中壢區中壢市), is dropped once, as is a repeated county plus town (桃園市桃園區桃園市桃園區), and so is a repeated 區, 鄉 or 鎮
    when a village of the town follows it.
    """
    if rest.startswith("市" + town):
        return None
    names = sorted({town, *town_aliases(county_code, town)}, key=lambda n: -len(n))
    # The county and the town written again (桃園市桃園區桃園市桃園區, 新北市板橋區臺北縣板橋市) are dropped together.
    counties = (county_name(county_code), *(old for old, new in OLD_COUNTY_PAIRS.items() if new == county_name(county_code)))
    for county in counties:
        for name in names:
            if rest.startswith(county + name):
                return rest[len(county + name):]
    for name in names:
        if rest.startswith(name):
            return rest[len(name):]
    if rest[:1] == town[-1] and rest[:1] in "區鄉鎮":
        # Dropped only when a village of this town follows (前鎮區區瑞昌里); 鎮江街 and 鎮平里 stay as written.
        if rest[1:].startswith(town_villages(county_code, town)):
            return rest[1:]
    for end in range(2, 6):
        other = rest[:end]
        if len(other) == end and other[-1] in "市區鄉鎮" and town_in_county(county_code, other):
            return None
    return rest


def _arabic_door(match: re.Match) -> str:
    main = match.group("main")
    subs = _CJK_RUN.sub(lambda m: str(cjk_number(m.group()) or m.group()), match.group("subs"))
    return str(cjk_number(main) or main) + subs


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


def building_key(address: str) -> str | None:
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
    region = split_county_town(text)
    if region is None:
        return None
    county_code, town, remainder = region
    remainder = after_town(county_code, town, remainder)
    if remainder is None:
        return None
    ok, village, neighborhood, rest = _split_village(remainder)
    if not ok:
        return None
    rest = _CJK_DOOR.sub(_arabic_door, rest)
    door = _DOOR.search(rest)
    if not door or not _FLOOR.fullmatch(door.group("floor")):
        return None
    if door.group("before") and door.group("after"):
        return None
    locality_road = rest[:door.start()]
    if "號" in locality_road or "鄰" in locality_road or _UNPARSABLE_FLOOR_DOOR.search(locality_road):
        return None
    # A number not followed by a unit (測試路10一1號) is an unparsed door, not a place name.
    trailing = re.search(r"[0-9]([^0-9]*)$", locality_road)
    if trailing and not _UNIT.search(trailing.group(1)):
        return None
    if not locality_road and not village:
        return None
    identity = {
        "county": county_code,
        "town": town,
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
    """(door signature, village, neighbourhood) of a building_key key.

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
