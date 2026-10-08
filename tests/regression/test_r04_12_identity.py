# -*- coding: utf-8 -*-
"""R04-12: door identity rule approved by the owner on 2026-10-09. Synthetic addresses only.

Each case names the row of data/tmp/address-logic/report.md (sections 2, 4 and 5) it covers.
"""
import json

import pytest

from lvr_pipeline.addresses.identity import (
    NORMALIZATION_VERSION,
    building_key_v2,
    cjk_number,
    door_parts,
    same_door,
)


def fields(address):
    key = building_key_v2(address)
    assert key is not None, address
    return json.loads(key[3:])


def test_version_is_bumped():
    assert NORMALIZATION_VERSION == "v2.5"


# Section 4 and 5.1: village and neighbourhood are sub-identifiers, never deleted.
@pytest.mark.parametrize("a,b", [
    # 2 / 5.1: same road and door in two villages (高樹鄉中正路55號).
    ("屏東縣高樹鄉泰山村5鄰中正路55號", "屏東縣高樹鄉廣興村1鄰中正路55號"),
    # 2 / 5.1: same village, different neighbourhood (永平路358號, 元西路74巷14號).
    ("新北市永和區大同里6鄰永平路358號", "新北市永和區大同里8鄰永平路358號"),
    ("雲林縣元長鄉長南村26鄰元西路74巷14號", "雲林縣元長鄉長南村30鄰元西路74巷14號"),
    # 7: 豐年街52號 in 海山里 and in 豐年里.
    ("新北市新莊區海山里12鄰豐年街52號", "新北市新莊區豐年里7鄰豐年街52號二樓"),
    # Card: 新街里 (village name containing 街).
    ("雲林縣北港鎮新街里14鄰文昌路176號", "雲林縣北港鎮舊街里14鄰文昌路176號"),
])
def test_other_village_or_neighbourhood_is_another_door(a, b):
    ka, kb = building_key_v2(a), building_key_v2(b)
    assert ka and kb and ka != kb
    assert door_parts(ka)[0] == door_parts(kb)[0]


def test_village_and_neighbourhood_fields():
    value = fields("屏東縣高樹鄉泰山村5鄰中正路55號")
    assert value == {"county": "10013", "town": "高樹鄉", "locality_road": "中正路", "door": "55",
                     "village": "泰山村", "neighborhood": "5"}
    assert "village" not in fields("屏東縣高樹鄉中正路55號")
    # Zero padding and Chinese numerals of 鄰 are the same neighbourhood number.
    assert fields("新北市永和區大同里006鄰永平路358號")["neighborhood"] == "6"
    assert fields("新北市永和區大同里六鄰永平路358號")["neighborhood"] == "6"
    # 5.2: only the neighbourhood written.
    only = fields("新北市新莊區12鄰豐年街52號")
    assert only["neighborhood"] == "12" and "village" not in only


@pytest.mark.parametrize("address,village,road", [
    # 4: village name contains 路街巷弄 (新街里) was not deleted.
    ("雲林縣北港鎮新街里14鄰文昌路176號", "新街里", "文昌路"),
    ("雲林縣北港鎮新街里文昌路176號", "新街里", "文昌路"),
    # 4: 里 or 村 inside the village name (仁里村) was half deleted.
    ("彰化縣埔心鄉仁里村4鄰聖天路25號", "仁里村", "聖天路"),
    ("彰化縣埔心鄉仁里村聖天路25號", "仁里村", "聖天路"),
    # 4 / 5.1: a road name with 村 or 里 lost its start (美村路一段 became 路一段).
    ("臺中市西區美村路一段109號", None, "美村路一段"),
    ("臺中市南區美村南路273號", None, "美村南路"),
    ("新竹縣芎林鄉上山村路171號", None, "上山村路"),
    ("花蓮縣新城鄉嘉里三街260巷27號", None, "嘉里三街260巷"),
    ("彰化縣和美鎮健康新村五街20號", None, "健康新村五街"),
    ("臺中市西區大里大道5號", None, "大里大道"),
    ("臺中市西區某里美村路一段109號", "某里", "美村路一段"),
])
def test_village_split_keeps_road_names(address, village, road):
    value = fields(address)
    assert value.get("village") == village
    assert value["locality_road"] == road


def test_town_ending_in_li_is_accepted():
    # 4: 太麻里鄉 was rejected because the town ended in 里.
    value = fields("臺東縣太麻里鄉泰和村5鄰1號")
    assert value["town"] == "太麻里鄉" and value["village"] == "泰和村"
    assert fields("臺東縣太麻里鄉太麻里村大王路1號")["town"] == "太麻里鄉"
    for kept in ["南投縣埔里鎮中山路一段1號", "屏東縣里港鄉中山路1號", "臺南市新市區中興街1號"]:
        assert building_key_v2(kept) is not None


def test_no_street_door_keeps_village():
    # 5.2: no street; the village is part of the door identity; place names are kept.
    assert fields("臺南市鹽水區三和里3鄰14之2號") == {
        "county": "67", "town": "鹽水區", "locality_road": "", "door": "14之2",
        "village": "三和里", "neighborhood": "3"}
    assert building_key_v2("臺南市鹽水區14之2號") is None
    assert fields("宜蘭縣五結鄉四結村2鄰頂寮15號")["locality_road"] == "頂寮"


@pytest.mark.parametrize("address,road", [
    # 4: 路名含至、及、與 was rejected as a range.
    ("新竹市香山區茄苳里10鄰至善街27巷3號", "至善街27巷"),
    ("臺北市士林區至善路二段5號", "至善路二段"),
    ("臺中市北屯區及人路8號", "及人路"),
    ("臺中市北屯區與時路8號", "與時路"),
    # 4: 地名 or community after the street was rejected.
    ("宜蘭縣五結鄉四結村2鄰中正路一段篤行三村1號", "中正路一段篤行三村"),
    ("臺中市中區光復里27鄰光復路合作大樓3之53號", "光復路合作大樓"),
    # 4: 衖 and 衕 were rejected.
    ("桃園市中壢區振興里4鄰龍岡路二段226巷43弄44衖4號四樓", "龍岡路二段226巷43弄44衖"),
    ("新北市鶯歌區二橋里23鄰中正三路230巷11弄1衕13號", "中正三路230巷11弄1衕"),
    # 1.4: text lane name is kept.
    ("新北市新莊區豐年里7鄰豐年街豐二巷52號", "豐年街豐二巷"),
])
def test_street_text_is_kept(address, road):
    assert fields(address)["locality_road"] == road


@pytest.mark.parametrize("address", [
    "臺北市大安區和平東路二段5號至7號",
    "臺北市大安區和平東路二段5號及7號",
    "臺北市大安區和平東路二段5號與7號",
    "臺北市大安區和平東路二段5至7號",
    "臺北市大安區和平東路二段5號3樓至5樓",
    "臺北市大安區和平東路二段5號6號",
    "臺北市中正區測試路10一1號",
    "高雄市前鎮區中山二路5號B1",
    # Known limitation, owner pending: 新竹縣 I-, G-, H- codes.
    "新竹縣竹北市中興里7鄰19之8I-",
    "新竹縣竹北市中興里7鄰126G-1H-3號",
])
def test_ranges_and_unparsed_doors_stay_none(address):
    assert building_key_v2(address) is None


@pytest.mark.parametrize("variant", [
    # 4: floors, rooms and basements take no part in the door.
    "5號壹樓", "5號3樓5", "5號二樓1號", "5號3樓5室", "5號A室", "5號地下室", "5號地下一層",
    "5號地下1樓", "5號底層", "5號十五樓", "5號3樓之4",
    # 4: bracketed notes are ignored.
    "5號(農業資材室)", "5號（店面）",
    # 4: 國字主號.
    "五號",
])
def test_floor_room_basement_note_and_cjk_door(variant):
    base = "臺北市大安區和平東路二段"
    assert building_key_v2(base + variant) == building_key_v2(base + "5號")


def test_fullwidth_letter_door():
    base = "臺北市大安區和平東路二段"
    assert building_key_v2(base + "13之3Ｂ號") == building_key_v2(base + "13之3B號")
    assert fields(base + "13之3Ｂ號")["door"] == "13之3B"
    assert building_key_v2(base + "13之3B號") != building_key_v2(base + "13之3號")


@pytest.mark.parametrize("a,b,door_a,door_b", [
    # 4 / card: never merged, listed as pairs in the review table.
    ("臺北市大安區和平東路二段10之1號", "臺北市大安區和平東路二段10號之1", "10之1", "10號之1"),
    ("嘉義縣大林鎮大美里14鄰188附1號", "嘉義縣大林鎮大美里14鄰188號附1", "188附1", "188號附1"),
    ("高雄市鳳山區中崙里15鄰6號附9", "高雄市鳳山區中崙里15鄰6號", "6號附9", "6"),
    ("臺北市萬華區銘德里4鄰長泰街臨201號", "臺北市萬華區銘德里4鄰長泰街201號", "臨201", "201"),
    ("彰化縣鹿港鎮山崙里2鄰建97號", "彰化縣鹿港鎮山崙里2鄰97號", "建97", "97"),
    ("新北市三重區中興里30鄰中興北街特137號", "新北市三重區中興里30鄰中興北街137號", "特137", "137"),
])
def test_door_notations_are_not_merged(a, b, door_a, door_b):
    assert fields(a)["door"] == door_a and fields(b)["door"] == door_b
    assert building_key_v2(a) != building_key_v2(b)


def test_lane_numerals_and_named_lane_are_not_merged():
    # 4 / 5.2: 十二巷 and 12巷; 1.4: with and without a text lane name.
    assert building_key_v2("臺北市中山區中山北路十二巷2號") != building_key_v2("臺北市中山區中山北路12巷2號")
    assert building_key_v2("新北市新莊區豐年街豐二巷52號") != building_key_v2("新北市新莊區豐年街52號")


def test_same_door_allows_added_parts_only():
    query = building_key_v2("屏東縣高樹鄉中正路55號")
    with_lin = building_key_v2("屏東縣高樹鄉泰山村5鄰中正路55號")
    other = building_key_v2("屏東縣高樹鄉廣興村1鄰中正路55號")
    assert same_door(query, with_lin) and same_door(query, other)
    assert same_door(with_lin, with_lin)
    assert not same_door(with_lin, other)
    assert not same_door(building_key_v2("屏東縣高樹鄉泰山村中正路55號"), other)
    assert not same_door(query, building_key_v2("屏東縣高樹鄉中正路56號"))
    assert not same_door(query, None)


@pytest.mark.parametrize("text,value", [("一", 1), ("十", 10), ("十二", 12), ("二十", 20), ("一百二十三", 123),
                                        ("十十", None), ("二三", None), ("", None), ("甲", None)])
def test_cjk_number(text, value):
    assert cjk_number(text) == value
