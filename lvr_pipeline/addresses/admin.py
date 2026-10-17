"""Towns that exist under each county (R07-6b), built from the pinned address source area tables.

Owner decision of 2026-10-13: resolve from large to small. The county is decided first, the town is
checked only inside that county, the road comes after. A town or road name never changes a county.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

from .parse import _COUNTY_CODE, norm

SCHEMA_VERSION = "1.0"
ADMIN_TOWNS_PATH = Path(__file__).resolve().parents[2] / "config/rules/admin_towns.json"
TOWN_SUFFIXES = "市區鄉鎮"
# Old county to the one new municipality that replaced it (owner decision R07-6b).
OLD_COUNTY_PAIRS = {
    "臺北縣": "新北市",
    "臺中縣": "臺中市",
    "臺南縣": "臺南市",
    "高雄縣": "高雄市",
    "桃園縣": "桃園市",
}
# Only these tables name old counties; area_2015 and area_custom never feed old_counties.
_OLD_COUNTY_FILES = {"area_1984.csv", "area_2010.csv", "area_2014.csv"}
_CURRENT_COUNTY = {code: name for name, code in _COUNTY_CODE.items() if not name.startswith("台")}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _rows(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            code = row["dgbas_id"].strip()
            if len(code) == 7 and code.isascii() and code.isdigit():
                yield norm(row["name"].replace("　", "").strip()), code


def _is_town(text: str) -> bool:
    return len(text) >= 2 and text[-1] in TOWN_SUFFIXES


def _known_towns(address_dir: Path, descriptor: dict) -> frozenset[str]:
    """Every town name any administrative table writes under any county code, old counties included."""
    known: set[str] = set()
    for item in descriptor["administrative_files"]:
        for name, _ in _rows(Path(address_dir) / item["path"]):
            for prefix in (*OLD_COUNTY_PAIRS, *_COUNTY_CODE):
                if name.startswith(prefix):
                    name = name[len(prefix):]
                    break
            if _is_town(name):
                known.add(name)
    return frozenset(known)


def _old_towns(address_dir: Path, descriptor: dict) -> tuple[dict[str, str], dict[str, frozenset[str]]]:
    """Old-county code (10012) to old county (高雄縣), and old town code (1001210) to the names it was written with."""
    address_dir = Path(address_dir)
    owners: dict[str, str] = {}
    for item in descriptor["administrative_files"]:
        for name, code in _rows(address_dir / item["path"]):
            old = next((o for o in OLD_COUNTY_PAIRS if name.startswith(o)), None)
            if old is not None and code[:2] not in _CURRENT_COUNTY and code[:5] not in _CURRENT_COUNTY:
                if owners.setdefault(code[:5], old) != old:
                    raise ValueError(f"Old county code {code[:5]} is written with two old counties")
    names: dict[str, set[str]] = {}
    for item in descriptor["administrative_files"]:
        for name, code in _rows(address_dir / item["path"]):
            old = owners.get(code[:5])
            if old is None:
                continue
            for prefix in (old, _CURRENT_COUNTY[_COUNTY_CODE[OLD_COUNTY_PAIRS[old]]]):
                if name.startswith(prefix):
                    name = name[len(prefix):]
                    break
            else:
                if any(name.startswith(other) for other in (*OLD_COUNTY_PAIRS, *_COUNTY_CODE)):
                    continue  # written with a different county than the dgbas_id says
            if _is_town(name):
                names.setdefault(code, set()).add(name)
    return owners, {code: frozenset(towns) for code, towns in names.items()}


def _villages(address_dir: Path, descriptor: dict, renamed_codes: frozenset[str]) -> tuple[list[dict], list[dict]]:
    """Village rows (NNNNNNN-NNN) of the area tables: accepted villages and rows excluded, each with a reason.

    The village belongs to the county and town code of its dgbas_id. Rows of an old-county code (高雄縣 10012)
    are listed under the new county (64) with the old town code and an old_county field. The written county
    (current or old) and the written town are cut off the name; a row that writes no town is accepted by its
    code. Excluded reasons: county_mismatch (another county written), renamed_town (the old town code was
    split or renamed and is not mapped) and town_mismatch (the written town is not a name of the town code).
    Rows of other unknown county codes (the old 臺中市 and 臺南市) and names that are not a 村 or 里 are skipped.
    """
    address_dir = Path(address_dir)
    names_by_code = town_names_by_code(address_dir, descriptor)
    owners, old_names = _old_towns(address_dir, descriptor)
    known = sorted(_known_towns(address_dir, descriptor), key=lambda t: -len(t))
    found: dict[tuple[str, str, str, str, str], set[str]] = {}
    excluded: dict[tuple[str, str, str], dict] = {}
    for item in descriptor["administrative_files"]:
        source = Path(item["path"]).name
        with (address_dir / item["path"]).open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                code = row["dgbas_id"].strip()
                if not re.fullmatch("[0-9]{7}-[0-9]{3}", code):
                    continue
                old = owners.get(code[:5])
                county_code = code[:2] if code[:2] in _CURRENT_COUNTY else code[:5]
                if old is not None:
                    county_code = _COUNTY_CODE[OLD_COUNTY_PAIRS[old]]
                    code_towns = old_names.get(code[:7], frozenset())
                elif county_code in _CURRENT_COUNTY:
                    code_towns = names_by_code.get((county_code, code[:7]), frozenset())
                else:
                    continue
                full = norm(row["name"].replace("　", "").strip())
                # The county names this code may be written with: its own, and for an old county the old name.
                allowed = {o for o, new in OLD_COUNTY_PAIRS.items() if _COUNTY_CODE[new] == county_code}
                if old is not None:
                    allowed = {old}
                allowed |= {n for n, c in _COUNTY_CODE.items() if c == county_code}
                name, written_county = full, ""
                for prefix in (*OLD_COUNTY_PAIRS, *_COUNTY_CODE):
                    if name.startswith(prefix):
                        if prefix in allowed:
                            name = name[len(prefix):]
                        else:
                            written_county = prefix
                        break
                reason = None
                if written_county:
                    rest = name[len(written_county):]
                    written_town = written_county + next((t for t in known if rest.startswith(t)), "")
                    reason = "county_mismatch"
                else:
                    town = next((t for t in sorted(code_towns, key=lambda t: -len(t)) if name.startswith(t)), None)
                    other = None if town is not None else next((t for t in known if name.startswith(t)), None)
                    written_town = town if town is not None else other
                    if town is not None:
                        name = name[len(town):]
                    if code[:7] in renamed_codes:
                        reason = "renamed_town"
                        written_town = written_town or ""
                    elif other is not None:
                        reason = "town_mismatch"
                if reason is not None:
                    excluded[(source, full, code)] = {
                        "source": source, "name": full, "code": code, "reason": reason,
                        "written_town": written_town, "code_towns": sorted(code_towns),
                    }
                elif len(name) >= 2 and name[-1] in "村里":
                    found.setdefault((county_code, code[:7], code, name, old or ""), set()).add(source)
    villages = []
    for (county, town_code, code, name, old), sources in sorted(found.items(), key=lambda i: (i[0][2], i[0][3], i[0][0])):
        entry = {"county": county, "town_code": town_code, "code": code, "name": name, "sources": sorted(sources)}
        if old:
            entry["old_county"] = old
        villages.append(entry)
    return villages, [excluded[key] for key in sorted(excluded, key=lambda k: (k[2], k[1], k[0]))]


def _town_codes(address_dir: Path, descriptor: dict, counties: dict[str, set[str]],
                old_counties: dict, renamed_codes: frozenset[str]) -> dict[str, dict[str, list[str]]]:
    """County code to town code to the town names that code has had.

    Current codes list the names that are towns of the county. Old-county codes are listed under the new county
    with their old names and the mapped new name (燕巢鄉, 燕巢區); renamed or split old codes are left out.
    """
    found: dict[str, dict[str, list[str]]] = {}
    for (county_code, code), names in sorted(town_names_by_code(address_dir, descriptor).items()):
        towns = sorted(names & counties.get(county_code, set()))
        if towns:
            found.setdefault(county_code, {})[code] = towns
    owners, old_names = _old_towns(address_dir, descriptor)
    for code, names in sorted(old_names.items()):
        old = owners[code[:5]]
        if code in renamed_codes:
            continue
        mapping = old_counties[old]["towns"]
        towns = sorted(names | {mapping[n] for n in names if n in mapping})
        found.setdefault(_COUNTY_CODE[old_counties[old]["county"]], {})[code] = towns
    return found


def build_admin_towns(address_dir: Path, descriptor: dict) -> dict:
    """The county to town table and the old-county mapping, from the descriptor's administrative files."""
    address_dir = Path(address_dir)
    counties: dict[str, set[str]] = {}
    old_names: dict[str, dict[str, set[str]]] = {old: {} for old in OLD_COUNTY_PAIRS}
    source_files = []
    for item in descriptor["administrative_files"]:
        path = address_dir / item["path"]
        if _sha256(path) != item["sha256"]:
            raise ValueError(f"Administrative source hash mismatch: {item['path']}")
        source_files.append({"path": item["path"], "sha256": item["sha256"]})
        for name, code in _rows(path):
            old = next((o for o in OLD_COUNTY_PAIRS if name.startswith(o)), None)
            if old is not None:
                # 桃園縣桃園市 is a real old town even though 桃園市 is also a county name.
                if Path(item["path"]).name in _OLD_COUNTY_FILES and _is_town(name[len(old):]):
                    old_names[old].setdefault(code, set()).add(name[len(old):])
                continue
            county_code = code[:2] if code[:2] in _CURRENT_COUNTY else code[:5]
            county = _CURRENT_COUNTY.get(county_code)
            if county is None:
                continue
            town = name[len(county):] if name.startswith(county) else name
            if name == town and any(name.startswith(other) for other in _COUNTY_CODE):
                continue  # written with a different county than the dgbas_id says
            if _is_town(town) and town not in _COUNTY_CODE:
                counties.setdefault(county_code, set()).add(town)
    # area_2015 also lists shortened spellings (高雄市前鎮, 臺南市左鎮, 桃園市平鎮) next to the full X區.
    # A shortened spelling is not a town, and it would shadow X區 when the town is read shortest first.
    for code, towns in counties.items():
        towns -= {t for t in towns if t + "區" in towns}
    old_counties, excluded, unmapped = {}, [], []
    for old, new_county in OLD_COUNTY_PAIRS.items():
        new_towns = counties.get(_COUNTY_CODE[new_county], set())
        banned, mapping = set(), {}
        for code, names in sorted(old_names[old].items()):
            if len({name[:-1] for name in names}) > 1:
                excluded.append({"county": old, "dgbas_id": code, "names": sorted(names)})
                banned |= names
        for names in old_names[old].values():
            for name in sorted(names - banned):
                new = name[:-1] + "區"
                if new in new_towns:
                    mapping[name] = new
                else:
                    unmapped.append({"county": old, "town": name})
        old_counties[old] = {"county": new_county, "towns": dict(sorted(mapping.items()))}
    # B-1: names one town code has had inside the same county (頭份市 and 頭份鎮), only where both are towns.
    same_town: dict[str, list[list[str]]] = {}
    for (county_code, _), names in sorted(town_names_by_code(address_dir, descriptor).items()):
        group = sorted(names & counties.get(county_code, set()))
        if len(group) > 1 and group not in same_town.setdefault(county_code, []):
            same_town[county_code].append(group)
    renamed = frozenset(e["dgbas_id"] for e in excluded)
    villages, village_excluded = _villages(address_dir, descriptor, renamed)
    return {
        "schema_version": SCHEMA_VERSION,
        "source_commit": descriptor["commit"],
        "source_files": source_files,
        "counties": {code: sorted(towns) for code, towns in sorted(counties.items())},
        "town_codes": _town_codes(address_dir, descriptor, counties, old_counties, renamed),
        "villages": villages,
        "village_excluded": village_excluded,
        "same_town": {code: groups for code, groups in sorted(same_town.items()) if groups},
        "old_counties": old_counties,
        "renamed_excluded": sorted(excluded, key=lambda e: (e["county"], e["dgbas_id"])),
        "unmapped": [{"county": c, "town": t} for c, t in sorted({(u["county"], u["town"]) for u in unmapped})],
    }


def county_name(county_code: str) -> str:
    """The current county name written for a county code (臺 spelling)."""
    return _CURRENT_COUNTY[county_code]


def town_names_by_code(address_dir: Path, descriptor: dict) -> dict[tuple[str, str], frozenset[str]]:
    """(county code, 7-digit town code) to every town name that code has had under that county (R07-6d).

    Read from the descriptor's administrative files after checking their sha256. The county is the one the
    town code belongs to; a written county (current or old) is cut off the name. 1000710 is both 員林鎮 and 員林市.
    """
    address_dir = Path(address_dir)
    names: dict[tuple[str, str], set[str]] = {}
    for item in descriptor["administrative_files"]:
        path = address_dir / item["path"]
        if _sha256(path) != item["sha256"]:
            raise ValueError(f"Administrative source hash mismatch: {item['path']}")
        for name, code in _rows(path):
            county_code = code[:2] if code[:2] in _CURRENT_COUNTY else code[:5]
            county = _CURRENT_COUNTY.get(county_code)
            if county is None:
                continue
            old = next((o for o in OLD_COUNTY_PAIRS if name.startswith(o)), None)
            if old is not None:
                town = name[len(old):]
            elif name.startswith(county):
                town = name[len(county):]
            elif any(name.startswith(other) for other in _COUNTY_CODE):
                continue  # written with a different county than the dgbas_id says
            else:
                town = name
            if _is_town(town):
                names.setdefault((county_code, code), set()).add(town)
    return {key: frozenset(towns) for key, towns in names.items()}


def write_admin_towns(address_dir: Path, descriptor: dict, output: Path) -> dict:
    document = build_admin_towns(address_dir, descriptor)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(document, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n",
    )
    return document


def _town_aliases(document: dict) -> dict[tuple[str, str], frozenset[str]]:
    """(county code, town) to every other name of the same town: one town code, or an old county's name."""
    groups: dict[tuple[str, str], set[str]] = {}
    for county_code, county_groups in document["same_town"].items():
        for group in county_groups:
            for name in group:
                groups.setdefault((county_code, name), set()).update(group)
    for info in document["old_counties"].values():
        county_code = _COUNTY_CODE[info["county"]]
        for old_name, new_name in info["towns"].items():
            groups.setdefault((county_code, new_name), {new_name}).add(old_name)
    return {key: frozenset(names - {key[1]}) for key, names in groups.items()}


def _town_village_names(document: dict) -> dict[tuple[str, str], tuple[str, ...]]:
    """(county code, town) to the sorted village names of every town code that has the town among its names."""
    by_code: dict[tuple[str, str], set[str]] = {}
    for entry in document["villages"]:
        by_code.setdefault((entry["county"], entry["town_code"]), set()).add(entry["name"])
    found: dict[tuple[str, str], set[str]] = {}
    for county_code, codes in document["town_codes"].items():
        for town_code, towns in codes.items():
            for town in towns:
                found.setdefault((county_code, town), set()).update(by_code.get((county_code, town_code), ()))
    return {key: tuple(sorted(names)) for key, names in found.items() if names}


@lru_cache(maxsize=1)
def load_admin_towns() -> dict:
    with ADMIN_TOWNS_PATH.open(encoding="utf-8") as stream:
        document = json.load(stream)
    return {
        "aliases": _town_aliases(document),
        "villages": _town_village_names(document),
        "document": document,
        "counties": {code: frozenset(towns) for code, towns in document["counties"].items()},
        "old_towns": {old: info["towns"] for old, info in document["old_counties"].items()},
        "old_county": {old: info["county"] for old, info in document["old_counties"].items()},
        "old_any": {
            old: sorted(
                set(info["towns"])
                | {u["town"] for u in document["unmapped"] if u["county"] == old}
                | {n for e in document["renamed_excluded"] if e["county"] == old for n in e["names"]},
                key=lambda t: -len(t),
            )
            for old, info in document["old_counties"].items()
        },
    }


def admin_towns_sha256() -> str:
    return _sha256(ADMIN_TOWNS_PATH)


def admin_towns_failures(address_source: dict) -> list[dict]:
    """Failures when admin_towns.json was not built from this address source (code admin_towns_stale)."""
    document = load_admin_towns()["document"]
    expected = [{"path": item["path"], "sha256": item["sha256"]} for item in address_source["administrative_files"]]
    if document.get("source_commit") != address_source.get("commit") or document.get("source_files") != expected:
        return [{"code": "admin_towns_stale",
                 "detail": "config/rules/admin_towns.json does not match the address source commit and "
                           "administrative files; run build-admin-towns"}]
    return []


def town_in_county(county_code: str, town: str) -> bool:
    return town in load_admin_towns()["counties"].get(county_code, ())


def town_aliases(county_code: str, town: str) -> frozenset[str]:
    """Other names the admin tables record for the same town: 頭份鎮 for 頭份市, 中壢市 for 中壢區."""
    return load_admin_towns()["aliases"].get((county_code, town), frozenset())


def town_villages(county_code: str, town: str) -> tuple[str, ...]:
    """Village names of the town, current and historical, from the area tables."""
    return load_admin_towns()["villages"].get((county_code, town), ())


def resolve_old_county(text: str) -> str | None:
    """Text unchanged unless it starts with an old county; then the new county and town, or None.

    The longest old town name of that old county wins. A town that does not map (not in the tables,
    renamed or split, or without an X區 under the paired new county) gives None.
    """
    data = load_admin_towns()
    for old, new_county in data["old_county"].items():
        if not text.startswith(old):
            continue
        rest = text[len(old):]
        for town in data["old_any"][old]:
            if rest.startswith(town):
                new_town = data["old_towns"][old].get(town)
                return new_county + new_town + rest[len(town):] if new_town else None
        return None
    return text
