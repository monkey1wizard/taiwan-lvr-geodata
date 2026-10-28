"""c50 lane numbers and c52 doubled roads in building_key (docs/地址規則.md, 巷弄數字與重複街路)."""

import json

import pytest

from lvr_pipeline.addresses.identity import building_key


def road(address: str) -> str:
    return json.loads(building_key(address)[3:])["locality_road"]


# c50: a CJK lane or alley number right after a road unit, lane, alley or digit is Arabic.
@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("花蓮縣花蓮市大勇路一巷25號", "大勇路1巷"),
        ("臺東縣臺東市東富街十三巷二號", "東富街13巷"),
        ("南投縣埔里鎮九芎街90巷一弄4號", "九芎街90巷1弄"),
        ("臺北市中山區中山北路二段一二巷2號", "中山北路二段12巷"),
        ("臺北市中山區中山北路一百零五巷2號", "中山北路105巷"),
        ("臺北市中山區中山北路12巷十弄2號", "中山北路12巷10弄"),
    ],
)
def test_lane_number_after_a_road_is_arabic(address, expected):
    assert road(address) == expected


def test_cjk_and_arabic_lane_numbers_give_one_key():
    assert building_key("南投縣埔里鎮九芎街90巷一弄4號") == building_key("南投縣埔里鎮九芎街90巷1弄4號")
    assert building_key("臺北市中山區中山北路十二巷2號") == building_key("臺北市中山區中山北路12巷2號")


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("苗栗縣苗栗市季水路清水一巷一弄3號", "季水路清水一巷1弄"),
        ("新北市新莊區豐年街豐二巷52號", "豐年街豐二巷"),
        ("臺北市中山區七賢路10號", "七賢路"),
    ],
)
def test_number_inside_a_name_is_kept(address, expected):
    assert road(address) == expected


# c52: a road or section written twice in full is kept once.
@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("臺北市信義區信義路五段信義路五段126號", "信義路五段"),
        ("彰化縣和美鎮和美鎮和旺街和旺街26號", "和旺街"),
        ("臺南市西港區新興街新興街32號", "新興街"),
    ],
)
def test_doubled_road_is_kept_once(address, expected):
    assert road(address) == expected


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("臺北市大安區信義路信義路五段126號", "信義路信義路五段"),
        ("臺北市中山區中山北路12巷中山北路12巷2號", "中山北路12巷中山北路12巷"),
        ("新北市新莊區豐年豐年52號", "豐年豐年"),
    ],
)
def test_partial_or_other_repeats_are_kept(address, expected):
    assert road(address) == expected
