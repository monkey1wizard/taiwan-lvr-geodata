#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 2 garbled resolver: two-corpus offline road-name matcher.

Pure logic module — no filesystem I/O except load_35321() and build_offline_road_set().
Caller supplies pre-loaded road indexes and a coord probe function.

resolve() returns (tier, candidate_address, evidence):
  "tier1_apply"   — coord-confirmed substitution; caller applies + caches
  "tier2_review"  — plausible candidate, no coord confirmation; caller logs only
  "none"          — nothing actionable (no PUA, multi-PUA, multi-match, no hit)

Invariants:
  - Never reads or writes garbled_override.csv
  - Handles single PUA in road-name portion only
  - Every Tier 1 result carries coord-hit evidence
"""
from __future__ import annotations

import csv
import os
import re
from typing import Callable

from lvr_pipeline.addresses.parse import arab_to_cjk, norm, parse


def _is_garble_char(c: str) -> bool:
    """單一亂碼字：literal ?(U+003F)或 PUA 造字(U+E000–U+F8FF)。"""
    return c == "?" or chr(0xE000) <= c <= chr(0xF8FF)


_CC_TO_CITY: dict[str, str] = {
    "63": "臺北市", "65": "新北市", "68": "桃園市",
    "66": "臺中市", "67": "臺南市", "64": "高雄市",
    "10017": "基隆市", "10018": "新竹市", "10020": "嘉義市",
    "10004": "新竹縣", "10005": "苗栗縣", "10007": "彰化縣",
    "10008": "南投縣", "10009": "雲林縣", "10010": "嘉義縣",
    "10013": "屏東縣", "10002": "宜蘭縣", "10015": "花蓮縣",
    "10014": "臺東縣", "10016": "澎湖縣", "09020": "金門縣",
    "09007": "連江縣",
}


def load_35321(csv_path: str) -> dict[str, list[str]]:
    """Load 35321 CSV → {site_id: [road, ...]}."""
    index: dict[str, list[str]] = {}
    if not os.path.isfile(csv_path):
        return index
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            site_id = (row.get("site_id") or "").strip()
            road = (row.get("road") or "").strip()
            if site_id and road:
                index.setdefault(site_id, []).append(road)
    return index


def build_offline_road_set(addr_db_dir: str, county_code: str) -> set[str]:
    """Enumerate roads/{county_code}-*.csv filenames → extract road names."""
    roads_dir = os.path.join(addr_db_dir, "roads")
    if not os.path.isdir(roads_dir):
        return set()
    prefix = f"{county_code}-"
    names: set[str] = set()
    for fname in os.listdir(roads_dir):
        if fname.startswith(prefix) and fname.endswith(".csv"):
            names.add(fname[len(prefix):-4])
    return names


def _make_road_pattern(road: str) -> re.Pattern | None:
    """Compile a regex matching road with its single PUA char as wildcard.

    Returns None when road has 0 or >1 PUA chars.
    """
    pua_count = sum(1 for c in road if _is_garble_char(c))
    if pua_count != 1:
        return None
    parts = []
    for c in road:
        if _is_garble_char(c):
            parts.append(".")
        else:
            parts.append(re.escape(c))
    return re.compile("".join(parts) + "$")


def _find_candidates(
    road_with_pua: str,
    offline_roads: set[str],
    roads_35321_for_site: list[str],
) -> list[str]:
    """Return deduplicated normalized road names matching road_with_pua (PUA=wildcard)."""
    norm_road = arab_to_cjk(norm(road_with_pua))
    pat = _make_road_pattern(norm_road)
    if pat is None:
        return []

    seen: set[str] = set()
    candidates: list[str] = []
    for corpus_road in (*offline_roads, *roads_35321_for_site):
        nc = arab_to_cjk(norm(corpus_road))
        if pat.match(nc) and nc not in seen:
            seen.add(nc)
            candidates.append(nc)
    return candidates


def resolve(
    address: str,
    offline_roads: set[str],
    roads_35321: dict[str, list[str]],
    coord_probe: Callable[[str], bool],
) -> tuple[str, str, str]:
    """Resolve single-PUA-in-road-name via two corpora + coord confirmation.

    Args:
        address:      raw_address from meta (may contain PUA)
        offline_roads: road names for the county from taiwan-address-data
        roads_35321:  {site_id: [road, ...]} loaded via load_35321()
        coord_probe:  fn(candidate_address) → True if offline geocode hits

    Returns:
        (tier, candidate_address, evidence)
        tier ∈ {"tier1_apply", "tier2_review", "none"}
    """
    county_code, town, road, _tail = parse(address)

    if not road or not county_code:
        return ("none", "", "no-road-parsed")

    pua_in_road = sum(1 for c in road if _is_garble_char(c))

    if pua_in_road == 0:
        return ("none", "", "no-pua-in-road")

    if pua_in_road > 1:
        return ("none", "", "multi-pua")

    city = _CC_TO_CITY.get(county_code, "")
    site_id = city + town
    roads_for_site = roads_35321.get(site_id, [])

    candidates = _find_candidates(road, offline_roads, roads_for_site)

    if len(candidates) == 0:
        return ("none", "", "no-corpus-match")

    if len(candidates) > 1:
        return ("none", "", f"multi-match:{len(candidates)}")

    candidate_road = candidates[0]
    norm_road = arab_to_cjk(norm(road))
    pua_char = next(c for c in norm_road if _is_garble_char(c))
    pua_idx = norm_road.index(pua_char)
    replacement_char = candidate_road[pua_idx]

    norm_addr = arab_to_cjk(norm(address))
    candidate_address = norm_addr.replace(pua_char, replacement_char)

    if coord_probe(candidate_address):
        return ("tier1_apply", candidate_address, f"offline_coord_hit:{candidate_road}")
    else:
        return ("tier2_review", candidate_address, f"corpus_match_no_coord:{candidate_road}")
