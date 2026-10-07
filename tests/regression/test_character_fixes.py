# -*- coding: utf-8 -*-
"""R04-3: one synthetic address per character-fix rule (V-05 evidence).

Every rule in config/rules/character-fixes.csv must fix a synthetic address, and each phrase (token) rule must
leave an address outside its scope untouched. Synthetic addresses only; no real data.

Not covered: whether the original address string is preserved. fix_garbled returns only the corrected string, so
the API does not expose the original. normalize() keeps it as the raw_address column of each observation; that
path is not asserted here (recorded as a next step in docs/records/R04執行紀錄.md).
"""
import csv
from pathlib import Path

import pytest

from lvr_pipeline.addresses.characters import fix_garbled, load_garbled, load_statuses

RULES = Path(__file__).resolve().parents[2] / "config" / "rules" / "character-fixes.csv"
ROWS = list(csv.DictReader(RULES.open(encoding="utf-8-sig", newline="")))
CHAR_ROWS = [r for r in ROWS if r["kind"] in ("char", "variant")]
TOKEN_ROWS = [r for r in ROWS if r["kind"] == "token"]
# No rule scope is a substring of this prefix, so it is outside every phrase rule's scope.
OUT_OF_SCOPE = "高雄市前鎮區"
PUA = chr(0xE000)


def _rule_id(row):
    return f"{row['kind']}:{row['garbled']}"


def _garbled_text(row):
    garbled = row["garbled"]
    return chr(int(garbled[2:], 16)) if garbled.upper().startswith("U+") else garbled


@pytest.fixture(scope="module")
def rules():
    return load_garbled(str(RULES))


def test_rule_inventory_is_21_with_5_phrase_rules():
    assert len(ROWS) == 21
    assert len(TOKEN_ROWS) == 5
    assert all(r["scope"] for r in TOKEN_ROWS)
    assert len(load_statuses(str(RULES))) == 21


@pytest.mark.parametrize("row", CHAR_ROWS, ids=_rule_id)
def test_character_rule_fixes_synthetic_address(row, rules):
    original = f"{OUT_OF_SCOPE}{_garbled_text(row)}路1號"
    assert fix_garbled(original, *rules) == f"{OUT_OF_SCOPE}{row['correct']}路1號"


@pytest.mark.parametrize("row", TOKEN_ROWS, ids=_rule_id)
@pytest.mark.parametrize("wildcard", ["?", PUA], ids=["literal-question-mark", "pua"])
def test_phrase_rule_fixes_address_inside_scope(row, wildcard, rules):
    original = f"{row['scope']}{row['garbled'].replace('?', wildcard)}路1號"
    assert fix_garbled(original, *rules) == f"{row['scope']}{row['correct']}路1號"


@pytest.mark.parametrize("row", TOKEN_ROWS, ids=_rule_id)
@pytest.mark.parametrize("wildcard", ["?", PUA], ids=["literal-question-mark", "pua"])
def test_phrase_rule_is_not_applied_outside_scope(row, wildcard, rules):
    assert row["scope"] not in OUT_OF_SCOPE
    original = f"{OUT_OF_SCOPE}{row['garbled'].replace('?', wildcard)}路1號"
    assert fix_garbled(original, *rules) == original
