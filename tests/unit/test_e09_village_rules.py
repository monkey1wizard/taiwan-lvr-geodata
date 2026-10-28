# -*- coding: utf-8 -*-
"""e09 (owner-approved 2026-10-20, v2.9): a repeated village name is dropped once; a place name read as a
village before the door is a locality_road. Each rule has positive and negative cases; expected keys are exact."""
import json

import pytest

from lvr_pipeline.addresses.identity import NORMALIZATION_VERSION, _split_village, building_key
from lvr_pipeline.addresses.parse import _COUNTY_CODE


def key_fields(address):
    key = building_key(address)
    return None if key is None else json.loads(key[3:])


def expected(county, town, road, door, village=None, neighborhood=None):
    value = {"county": _COUNTY_CODE[county], "town": town, "locality_road": road, "door": door}
    if village:
        value["village"] = village
    if neighborhood:
        value["neighborhood"] = neighborhood
    return value


def test_version():
    assert NORMALIZATION_VERSION == "v2.11"


@pytest.mark.parametrize("address,want", [
    # Rule 1 positives: the full village name written twice, with and without 鄰.
    ("苗栗縣頭份市斗煥里斗煥里14鄰中正二路35巷20號", expected("苗栗縣", "頭份市", "中正二路35巷", "20", "斗煥里", "14")),
    ("彰化縣和美鎮嘉犁里嘉犁里嘉佃路712巷70號", expected("彰化縣", "和美鎮", "嘉佃路712巷", "70", "嘉犁里")),
    ("嘉義縣梅山鄉梅北村梅北村中正路84號", expected("嘉義縣", "梅山鄉", "中正路", "84", "梅北村")),
    ("苗栗縣三義鄉廣盛村廣盛村中正路5號", expected("苗栗縣", "三義鄉", "中正路", "5", "廣盛村")),
    # Rule 1 negatives: a partial repeat, a different road, and a village written once keep today's key.
    ("苗栗縣苗栗市嘉新里新里路5號", expected("苗栗縣", "苗栗市", "新里路", "5", "嘉新里")),
    ("苗栗縣苗栗市嘉新里新里5號", expected("苗栗縣", "苗栗市", "新里", "5", "嘉新里")),
    ("苗栗縣苗栗市嘉新里嘉新路5號", expected("苗栗縣", "苗栗市", "嘉新路", "5", "嘉新里")),
    ("苗栗縣三義鄉廣盛村20號", expected("苗栗縣", "三義鄉", "", "20", "廣盛村")),
])
def test_repeated_village_is_dropped_once(address, want):
    assert key_fields(address) == want


def test_road_starting_with_the_village_name_is_kept():
    # 中里 followed by 中里路: the second 中里 is the start of the road, not a repeat.
    assert _split_village("中里中里路5號") == (True, "中里", None, "中里路5號")
    assert _split_village("中里中里5號") == (True, "中里", None, "5號")


@pytest.mark.parametrize("address,want", [
    # Rule 2 positives: a token that is not a village of the town, directly before the door.
    ("新北市石門區石門新村6號二樓", expected("新北市", "石門區", "石門新村", "6")),
    ("金門縣金沙鎮民享村17號", expected("金門縣", "金沙鎮", "民享村", "17")),
    ("花蓮縣花蓮市豐村119號", expected("花蓮縣", "花蓮市", "豐村", "119")),
    # Edge case: the town has 豐里里, not 豐里; short forms are not matched.
    ("臺南市玉井區豐里18號", expected("臺南市", "玉井區", "豐里", "18")),
    # Rule 2 negatives: a village of the town, a token followed by a road, a token with a neighbourhood.
    ("雲林縣虎尾鎮興南里548號", expected("雲林縣", "虎尾鎮", "", "548", "興南里")),
    ("苗栗縣三義鄉廣盛村20號", expected("苗栗縣", "三義鄉", "", "20", "廣盛村")),
    ("新北市石門區石門新村中正路6號", expected("新北市", "石門區", "中正路", "6", "石門新村")),
    ("新北市石門區石門新村3鄰6號", expected("新北市", "石門區", "", "6", "石門新村", "3")),
])
def test_place_name_read_as_village_is_locality_road(address, want):
    assert key_fields(address) == want
