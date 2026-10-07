# -*- coding: utf-8 -*-
"""R04-5: building_key_v2 same-address regression cases (V-06 evidence). Synthetic addresses only.

Fixed-expectation cases assert equality or inequality. Cases marked "record" assert nothing about equality;
the actual outputs are recorded in docs/records/R04執行紀錄.md. This file does not change identity.py.
"""
import pytest

from lvr_pipeline.addresses.identity import building_key_v2

R = "臺北市大安區和平東路二段"


def _keys(a, b):
    return building_key_v2(a), building_key_v2(b)


def _assert_same(a, b):
    ka, kb = _keys(a, b)
    assert ka is not None and ka == kb


def _assert_different(a, b):
    ka, kb = _keys(a, b)
    assert ka is not None and kb is not None and ka != kb


SAME = [
    ("variant_tai", "台北市大安區和平東路二段100號", R + "100號"),
    ("floor_suffix", R + "100號", R + "100號5樓"),
    ("same_road_other_li_lin", "臺北市大安區仁愛里3鄰和平東路二段100號", "臺北市大安區建安里5鄰和平東路二段100號"),
]
DIFFERENT = [
    ("sub_door_10_vs_10_1", R + "10號", R + "10之1號"),
    ("sub_door_58_25_vs_58_22", R + "58之25號", R + "58之22號"),
    ("door_341_vs_201", R + "341號", R + "201號"),
    ("door_64_vs_62", R + "64號", R + "62號"),
    ("other_road_same_door", "新北市三峽區中山路10號", "新北市三峽區民權街10號"),
    ("no_road_other_village", "嘉義縣竹崎鄉文峰村1號", "嘉義縣竹崎鄉內樹村1號"),
]


@pytest.mark.parametrize("a,b", [c[1:] for c in SAME], ids=[c[0] for c in SAME])
def test_same_address(a, b):
    _assert_same(a, b)


@pytest.mark.parametrize("a,b", [c[1:] for c in DIFFERENT], ids=[c[0] for c in DIFFERENT])
def test_different_address(a, b):
    _assert_different(a, b)


def test_duplicated_prefix_same_as_single_or_both_none():
    ka, kb = _keys("臺北市臺北市大安區和平東路二段100號", R + "100號")
    assert ka == kb or (ka is None and kb is None)


@pytest.mark.parametrize("doubled", ["臺北市臺北市大安區和平東路二段100號", "台北市臺北市大安區和平東路二段100號",
                                     "臺北市臺北市臺北市大安區和平東路二段100號"])
def test_doubled_county_prefix_equals_single_prefix_key(doubled):
    # R04-6 owner decision: an exactly repeated county name (after norm) is dropped before the key is built.
    ka, kb = _keys(doubled, R + "100號")
    assert ka is not None and ka == kb


@pytest.mark.parametrize("address", ["北投區臺北市北投區和平東路二段100號", "大安區臺北市大安區和平東路二段100號",
                                     "新北市臺北市大安區和平東路二段100號"])
def test_other_unparseable_start_is_none(address):
    assert building_key_v2(address) is None


def test_range_door_is_none():
    assert building_key_v2(R + "10-12號") is None


def test_sub_door_before_vs_after_number_recorded_only():
    # Record only: no assertion about equality; sub-door equivalence is not inferred (see R04 record).
    ka, kb = _keys(R + "10之1號", R + "10號之1")
    assert ka is None or isinstance(ka, str)
    assert kb is None or isinstance(kb, str)
