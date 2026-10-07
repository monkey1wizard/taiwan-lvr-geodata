# -*- coding: utf-8 -*-
"""R04-6b: one-character district names (東/西/南/北/中區) must parse. Synthetic addresses only."""
import json

import pytest

from lvr_pipeline.addresses.identity import building_key_v2
from lvr_pipeline.addresses.parse import _CITY_RE

ONE_CHAR = [("臺中市", d) for d in "東西南北中"] + [("臺南市", d) for d in "東南北"] + \
           [("新竹市", d) for d in "東北"] + [("嘉義市", d) for d in "東西"]


def test_listed_one_character_districts():
    assert len(ONE_CHAR) == 12  # the card says 14 but lists 12 districts


@pytest.mark.parametrize("city,d", ONE_CHAR)
def test_one_character_district_parses(city, d):
    m = _CITY_RE.match(f"{city}{d}區和平路10號")
    assert m and m.group(1) == city and m.group(2) == d + "區"
    key = building_key_v2(f"{city}{d}區和平路10號")
    assert key is not None and json.loads(key[3:])["town"] == d + "區"


@pytest.mark.parametrize("name,city,town", [
    ("南投縣埔里鎮", "南投縣", "埔里鎮"), ("臺南市中西區", "臺南市", "中西區"), ("新竹縣竹北市", "新竹縣", "竹北市"),
    ("屏東縣里港鄉", "屏東縣", "里港鄉"), ("臺北市大安區", "臺北市", "大安區")])
def test_existing_districts_unchanged(name, city, town):
    m = _CITY_RE.match(name)
    assert m and (m.group(1), m.group(2)) == (city, town)
