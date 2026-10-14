"""Towns that exist under each county (R07-6b), built from the pinned address source area tables.

Owner decision of 2026-10-13: resolve from large to small. The county is decided first, the town is
checked only inside that county, the road comes after. A town or road name never changes a county.
"""
from __future__ import annotations

import csv
import hashlib
import json
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
    return {
        "schema_version": SCHEMA_VERSION,
        "source_commit": descriptor["commit"],
        "source_files": source_files,
        "counties": {code: sorted(towns) for code, towns in sorted(counties.items())},
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


@lru_cache(maxsize=1)
def load_admin_towns() -> dict:
    with ADMIN_TOWNS_PATH.open(encoding="utf-8") as stream:
        document = json.load(stream)
    return {
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
