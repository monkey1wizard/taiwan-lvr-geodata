"""Conservative v2 door identity; legacy keys are never accepted as evidence."""
from __future__ import annotations

import json
import re

from .address import _CITY_RE, _COUNTY_CODE, _REJECT_RE, arab_to_cjk, is_garbled, norm

KEY_VERSION = "v2"
_DOOR = re.compile(r"(?P<main>[0-9]+)(?P<before>(?:之[0-9]+)*)號(?P<after>(?:之[0-9]+)*)(?P<floor>.*)$")
_FLOOR = re.compile(r"(?:[0-9一二三四五六七八九十百千]+樓(?:之[0-9一二三四五六七八九十]+)?|地下[0-9一二三四五六七八九十]+樓)?$")
_ROAD = re.compile(r"(?:大道|路|街)")


def building_key_v2(address: str) -> str | None:
    if not isinstance(address, str) or not address.strip():
        return None
    text = arab_to_cjk(norm(address.strip()))
    if is_garbled(text) or _REJECT_RE.search(text) or text.count("號") != 1:
        return None
    # Hyphens can represent ranges or subnumbers; neither is guessed.
    if any(c in text for c in "-至~～、及與") or re.search(r"\s", text):
        return None
    region = _CITY_RE.match(text)
    if not region or region.group(2).endswith("里"):
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
