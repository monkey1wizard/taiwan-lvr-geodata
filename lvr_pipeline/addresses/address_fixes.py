"""Repair rare characters lost in transaction addresses with the pinned char_fixes.csv (R07-6d).

Owner rules of 2026-10-14. Only street, lane, village and area rows are used. A row whose evidence
cites only this project's transaction data is excluded. Resolution goes from large to small: the
county is decided first, then the town (the same parser building_key uses), and a fix applies only
inside the town code it was written for. The fix is applied to the text after the town.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..storage.runs import sha256_file
from . import admin
from .identity import split_county_town
from .parse import GARBLE_RE, arab_to_cjk, norm

USED_FIELDS = frozenset({"street", "lane", "village", "area"})
PLACEHOLDERS = "?■？"
_PRIVATE_USE = re.compile("[" + chr(0xE000) + "-" + chr(0xF8FF) + "]")
_OWN_PROJECT = "taiwan-lvr-geodata"
_OTHER_EVIDENCE = ("戶政司", "國土", "NLSC", "RIS", "2016", "擁有者")
DIAGNOSTIC_CODE = "address_char_fix"
COUNT_KEYS = ("kept", "excluded_by_field", "excluded_transaction_only", "skipped_no_placeholder", "unknown_town_code")


@dataclass(frozen=True)
class CharFix:
    county: str
    town_code: str
    field: str
    source_value: str  # as written in char_fixes.csv
    fixed_value: str
    evidence: str
    town_names: frozenset
    pattern: re.Pattern
    replacement: str


@dataclass(frozen=True)
class AddressCharFixes:
    """Kept fixes (longer source_value first) with counts and the hashes that identify the table."""
    rules: tuple
    counts: dict
    sha256: str  # char_fixes.csv
    towns_sha256: str  # the administrative files the town names were read from
    excluded_transaction_only: tuple = ()  # (county, town code, field, source_value) of each excluded row


def transaction_only(evidence: str) -> bool:
    """True when the evidence cites this project's transaction data and no other source."""
    return _OWN_PROJECT in evidence and not any(mark in evidence for mark in _OTHER_EVIDENCE)


def _text(value: str) -> str:
    return arab_to_cjk(norm(value.strip()))


def _pattern(source_value: str) -> re.Pattern:
    return re.compile("".join("[" + PLACEHOLDERS + "]" if c in PLACEHOLDERS else re.escape(c) for c in source_value))


def load_address_char_fixes(address_dir: Path, descriptor: dict) -> AddressCharFixes:
    """The usable fixes of the pinned char_fixes.csv; raises when a hash differs from the descriptor."""
    entry = descriptor.get("char_fixes")
    if not isinstance(entry, dict) or not entry.get("sha256"):
        raise ValueError("Address source descriptor has no char_fixes entry")
    path = Path(address_dir) / entry["path"]
    digest = sha256_file(path)
    if digest != entry["sha256"]:
        raise ValueError(f"char_fixes.csv hash mismatch: {digest} is not {entry['sha256']}")
    towns = admin.town_names_by_code(address_dir, descriptor)
    counts = dict.fromkeys(COUNT_KEYS, 0)
    kept, excluded = [], []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row["field"] not in USED_FIELDS:
                counts["excluded_by_field"] += 1
                continue
            if transaction_only(row["evidence"]):
                counts["excluded_transaction_only"] += 1
                excluded.append((row["county"], row["town"], row["field"], row["source_value"]))
                continue
            source = _text(row["source_value"])
            if not source or not (any(c in PLACEHOLDERS for c in source) or _PRIVATE_USE.search(source)):
                counts["skipped_no_placeholder"] += 1  # nothing was lost, so there is nothing to restore
                continue
            names = towns.get((row["county"], row["town"]))
            if not names:
                counts["unknown_town_code"] += 1
                continue
            fixed = _text(row["fixed_value"])
            kept.append(CharFix(row["county"], row["town"], row["field"], row["source_value"], row["fixed_value"],
                                row["evidence"], names, _pattern(source), fixed))
    seen: dict[tuple, str] = {}
    for rule in kept:
        key = (rule.county, rule.town_code, rule.field, _text(rule.source_value))
        if seen.setdefault(key, rule.replacement) != rule.replacement:
            raise ValueError(f"char_fixes.csv gives two fixes for {key}")
    counts["kept"] = len(kept)
    kept.sort(key=lambda rule: -len(_text(rule.source_value)))  # stable: file order among equal lengths
    towns_sha = hashlib.sha256(json.dumps(sorted((i["path"], i["sha256"]) for i in descriptor["administrative_files"])).encode()).hexdigest()
    return AddressCharFixes(tuple(kept), counts, digest, towns_sha, tuple(excluded))


def apply_address_char_fixes(address: str, fixes: AddressCharFixes) -> tuple[str, list[dict]]:
    """(address, applied fixes). The address is the normalized text; unchanged when nothing applies.

    Each applied item has the diagnostic fields county, town_code, field, source_value, fixed_value,
    evidence, before and after (the whole address before and after that fix) and the match count.
    """
    split = split_county_town(address)
    if split is None:
        return address, []
    county, town, remainder = split
    prefix = admin.county_name(county) + town
    applied = []
    for rule in fixes.rules:
        if rule.county != county or town not in rule.town_names:
            continue
        changed, count = rule.pattern.subn(lambda _: rule.replacement, remainder)
        if count:
            applied.append({"county": county, "town_code": rule.town_code, "field": rule.field,
                            "source_value": rule.source_value, "fixed_value": rule.fixed_value,
                            "evidence": rule.evidence, "before": prefix + remainder, "after": prefix + changed,
                            "matches": count})
            remainder = changed
    if not applied:
        return address, []
    return prefix + remainder, applied
