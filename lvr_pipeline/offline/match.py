"""Administrative-name matching of address text to town codes."""

from __future__ import annotations

import csv
from pathlib import Path
import re

from ..addresses.identity import drop_repeated_county
from ..addresses.parse import _COUNTY_CODE, _CITY_RE, norm
from ..transactions.normalize import normalize_address
from ..storage.runs import sha256_file

COUNTY_NAMES = {
    code: name for name, code in _COUNTY_CODE.items() if not name.startswith("台")
}


class AdministrativeNames:
    def __init__(self, root: Path, files: list[dict]):
        aliases = {}
        current = {}
        priorities = {}
        self.tree = {}
        for priority, item in enumerate(sorted(files, key=lambda x: x["path"])):
            path = root / item["path"]
            if sha256_file(path) != item["sha256"]:
                raise ValueError("Administrative source hash mismatch")
            within = {}
            with path.open(encoding="utf-8-sig", newline="") as stream:
                for row in csv.DictReader(stream):
                    name = norm(row["name"].replace("　", "").strip())
                    code = row["dgbas_id"].strip()
                    if not re.fullmatch("[0-9]{7}", code):
                        continue
                    within.setdefault(name, set()).add(code)
                    region = _CITY_RE.fullmatch(name)
                    county = code[:2] if code[:2] in COUNTY_NAMES else code[:5]
                    if (
                        item["path"] != "area_custom.csv"
                        and region
                        and region[1] != region[2]
                        and _COUNTY_CODE[region[1]] == county
                    ):
                        if priorities.get(code, -1) <= priority:
                            current[code] = name
                            priorities[code] = priority
            for name, codes in within.items():
                aliases[name] = next(iter(codes)) if len(codes) == 1 else None
        self.current = current
        for name, code in aliases.items():
            if code not in current:
                continue
            if any(name == city + city for city in _COUNTY_CODE):
                continue
            node = self.tree
            for character in name:
                node = node.setdefault(character, {})
            node[""] = code

    def canonicalize(self, address):
        # R05-7: the same repeated-county rule as building_key_v2 (新竹市新竹市東區… → 新竹市東區…).
        address = drop_repeated_county(normalize_address(address))
        node = self.tree
        found = None
        for index, character in enumerate(address):
            if character not in node:
                break
            node = node[character]
            if "" in node:
                found = index + 1, node[""]
        if not found:
            return address, "", ""
        end, town = found
        canonical = self.current[town]
        county = _COUNTY_CODE[_CITY_RE.fullmatch(canonical)[1]]
        return canonical + address[end:], county, town
