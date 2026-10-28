# -*- coding: utf-8 -*-
"""B-1 (owner-approved 2026-10-18): repeated town and unit, other town, CJK main number before 之N號,
floor before the door. Synthetic addresses only; expected keys are exact."""
import json

import pytest

from lvr_pipeline.addresses.identity import NORMALIZATION_VERSION, building_key
from lvr_pipeline.addresses.parse import _COUNTY_CODE
from lvr_pipeline.offline.match import AdministrativeNames


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
    assert NORMALIZATION_VERSION == "v2.10"


@pytest.mark.parametrize("address,want", [
    # b03: the same town again, and another name of the same town code.
    ("花蓮縣瑞穗鄉瑞穗鄉瑞美村1號", expected("花蓮縣", "瑞穗鄉", "", "1", "瑞美村")),
    ("苗栗縣頭份市頭份鎮忠孝里6鄰長安街80巷1號", expected("苗栗縣", "頭份市", "長安街80巷", "1", "忠孝里", "6")),
    ("桃園市中壢區中壢市月眉里8鄰月眉路三段409巷32號", expected("桃園市", "中壢區", "月眉路三段409巷", "32", "月眉里", "8")),
    ("花蓮縣花蓮市花蓮市國裕里35鄰中央路四段288巷5弄17號", expected("花蓮縣", "花蓮市", "中央路四段288巷5弄", "17", "國裕里", "35")),
    # b03 negative: a road that starts like the town is not a repeat.
    ("花蓮縣吉安鄉吉安路5號", expected("花蓮縣", "吉安鄉", "吉安路", "5")),
    # b04 negative: a village that itself starts with the unit character is no repeat.
    ("彰化縣和美鎮鎮平里6鄰南平路6號", expected("彰化縣", "和美鎮", "南平路", "6", "鎮平里", "6")),
    ("屏東縣東港鎮鎮海里1鄰嘉新路二段36號", expected("屏東縣", "東港鎮", "嘉新路二段", "36", "鎮海里", "1")),
    # b04 negatives: a road or village that merely starts with the unit character stays as written.
    ("南投縣埔里鎮鎮江街33號", expected("南投縣", "埔里鎮", "鎮江街", "33")),
    ("彰化縣和美鎮鎮西路137巷12號", expected("彰化縣", "和美鎮", "鎮西路137巷", "12")),
    ("新竹縣關西鎮鎮光復路129號二樓", expected("新竹縣", "關西鎮", "鎮光復路", "129")),
    ("新竹縣湖口鄉鄉八德路一段822巷5號五樓之6", expected("新竹縣", "湖口鄉", "鄉八德路一段822巷", "5")),
    # b04: the unit character written twice.
    ("高雄市前鎮區區瑞昌里瑞泰街33號四樓", expected("高雄市", "前鎮區", "瑞泰街", "33", "瑞昌里")),
    ("苗栗縣造橋鄉鄉談文村5鄰育英八街101號一樓之一", expected("苗栗縣", "造橋鄉", "育英八街", "101", "談文村", "5")),
])
def test_repeated_town_and_unit_are_dropped(address, want):
    assert key_fields(address) == want


@pytest.mark.parametrize("address", [
    "苗栗縣苗栗市公館鄉玉谷村1號",  # b05: a different town of the same county
    "臺南市東區市東區文聖里6鄰裕農一街186號八樓之8",  # b05 extension: 市 plus the same town
    "臺中市北區市北區錦祥里22鄰育樂街39號10樓之2",
])
def test_other_town_gives_no_key(address):
    assert building_key(address) is None


@pytest.mark.parametrize("address", [
    "苗栗縣苗栗市公館鄉玉谷村1號",
    "臺南市東區市東區文聖里6鄰裕農一街186號八樓之8",
    "臺中市北區市北區錦祥里22鄰育樂街39號10樓之2",
])
def test_canonicalize_reads_other_town_as_invalid_admin(address, tmp_path):
    """The pool and the index record invalid_admin when canonicalize returns no county."""
    names = _names(tmp_path)
    canonical, county, town = names.canonicalize(address)
    assert (county, town) == ("", "")


@pytest.mark.parametrize("address", [
    "苗栗縣頭份市頭份鎮忠孝里6鄰長安街80巷1號",
    "高雄市前鎮區區瑞昌里瑞泰街33號四樓",
    "花蓮縣吉安鄉吉安路5號",
])
def test_canonicalize_keeps_repeats_and_roads_readable(address, tmp_path):
    names = _names(tmp_path)
    canonical, county, town = names.canonicalize(address)
    assert county and town
    assert building_key(canonical) == building_key(address)


def _names(tmp_path):
    """AdministrativeNames over a small synthetic area table."""
    import hashlib

    files = {
        "area_2015.csv": (
            "name,dgbas_id\n苗栗縣頭份市,1000505\n苗栗縣苗栗市,1000501\n苗栗縣公館鄉,1000502\n"
            "臺南市東區,6700100\n臺中市北區,6600100\n高雄市前鎮區,6400900\n花蓮縣吉安鄉,1001502\n"
        ),
    }
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    items = [{"path": n, "sha256": hashlib.sha256((tmp_path / n).read_bytes()).hexdigest()} for n in files]
    return AdministrativeNames(tmp_path, items)


@pytest.mark.parametrize("address,want", [
    # b06: the CJK numeral before 之N號 is the main door number, whatever notation follows 之.
    ("宜蘭縣五結鄉國民中路七之1號", expected("宜蘭縣", "五結鄉", "國民中路", "7之1")),
    ("宜蘭縣五結鄉國民中路七之一號", expected("宜蘭縣", "五結鄉", "國民中路", "7之1")),
    ("宜蘭縣五結鄉國光里國興街一巷一之3號", expected("宜蘭縣", "五結鄉", "國興街1巷", "1之3", "國光里")),
    ("宜蘭縣五結鄉國光里國興街一巷一之三號", expected("宜蘭縣", "五結鄉", "國興街1巷", "1之3", "國光里")),
    ("宜蘭縣五結鄉國民中路十二之十號", expected("宜蘭縣", "五結鄉", "國民中路", "12之10")),
    # b06 negatives: a road number stays a road name, 十二巷 and 12巷 are still not merged.
    ("宜蘭縣五結鄉七賢路10號", expected("宜蘭縣", "五結鄉", "七賢路", "10")),
    ("宜蘭縣五結鄉十二巷10號", expected("宜蘭縣", "五結鄉", "十二巷", "10")),
    ("宜蘭縣五結鄉12巷10號", expected("宜蘭縣", "五結鄉", "12巷", "10")),
])
def test_cjk_main_number_before_zhi(address, want):
    assert key_fields(address) == want


@pytest.mark.parametrize("address,want", [
    # b08: a floor or basement between the road and the door stays in locality_road, as before.
    ("嘉義縣大林鎮平林里16鄰潭底零售市場二樓18號", expected("嘉義縣", "大林鎮", "潭底零售市場二樓", "18", "平林里", "16")),
    ("臺中市北區東興市場二樓7號", expected("臺中市", "北區", "東興市場二樓", "7")),
    ("高雄市三民區大順二樓189號", expected("高雄市", "三民區", "大順二樓", "189")),
    ("桃園市桃園區和興街25巷五樓17號", expected("桃園市", "桃園區", "和興街25巷五樓", "17")),
    ("高雄市鳳山區武慶二路56巷地下層36之1號", expected("高雄市", "鳳山區", "武慶二路56巷地下層", "36之1")),
    ("彰化縣彰化市介壽北路地下室247之2號", expected("彰化縣", "彰化市", "介壽北路地下室", "247之2")),
    ("嘉義市東區吳鳳南路365巷76弄地下層55號", expected("嘉義市", "東區", "吳鳳南路365巷76弄地下層", "55")),
    ("嘉義市西區興仁街38巷地下層129號", expected("嘉義市", "西區", "興仁街38巷地下層", "129")),
    ("嘉義縣大林鎮吉林里1鄰福德大樓二樓1號", expected("嘉義縣", "大林鎮", "福德大樓二樓", "1", "吉林里", "1")),
])
def test_floor_before_door_keeps_head_key(address, want):
    assert key_fields(address) == want


@pytest.mark.parametrize("address", [
    "彰化縣彰化市成功路二一五之六樓之1號",
    "彰化縣彰化市成功路二一五之七樓之2號",
    "彰化縣彰化市成功路二一五之五樓之6號",
    "桃園市中壢區松義路80十樓，松義三街232號",
])
def test_unparsable_floor_door_gives_no_key(address):
    assert building_key(address) is None


@pytest.mark.parametrize("address,want", [
    # Place names that contain 樓 keep their key; floors after the door are still ignored.
    ("屏東縣里港鄉塔樓路1530號", expected("屏東縣", "里港鄉", "塔樓路", "1530")),
    ("屏東縣里港鄉塔樓路15之5號", expected("屏東縣", "里港鄉", "塔樓路", "15之5")),
    # v2.9 (e09 rule 2): 樓厝村 is not a village of 蘆洲區 in the admin table, so it is the place name.
    ("新北市蘆洲區樓厝村71號", expected("新北市", "蘆洲區", "樓厝村", "71")),
    ("臺南市北區中樓里7鄰東豐路133巷9號", expected("臺南市", "北區", "東豐路133巷", "9", "中樓里", "7")),
    ("臺南市北區中樓里開元路90巷67之11號", expected("臺南市", "北區", "開元路90巷", "67之11", "中樓里")),
    ("桃園市中壢區福里15鄰五光二街8號10樓", expected("桃園市", "中壢區", "五光二街", "8", "福里", "15")),
    # Unchanged negatives: no new rule applies to these.
    ("雲林縣虎尾鎮和里成和三街26號", expected("雲林縣", "虎尾鎮", "成和三街", "26", "和里")),
    ("苗栗縣三義鄉鯉魚村4鄰鯉魚口75之7號", expected("苗栗縣", "三義鄉", "鯉魚口", "75之7", "鯉魚村", "4")),
    ("高雄市仁武區文武村文武里38鄰文北街163號", expected("高雄市", "仁武區", "文北街", "163", "文武村文武里", "38")),
    ("苗栗縣苗栗市嘉新里新里10鄰嘉明1之65號", expected("苗栗縣", "苗栗市", "嘉明", "1之65", "嘉新里新里", "10")),
    ("高雄市鳳山區狟磳_里北賢街76巷20號", expected("高雄市", "鳳山區", "北賢街76巷", "20", "狟磳_里")),
])
def test_place_names_and_unchanged_negatives_keep_their_key(address, want):
    assert key_fields(address) == want


@pytest.mark.parametrize("address,want", [
    ("桃園市桃園區桃園市桃園區永順街99巷7號14樓", expected("桃園市", "桃園區", "永順街99巷", "7")),
    ("桃園市桃園區桃園市桃園區南平路290號", expected("桃園市", "桃園區", "南平路", "290")),
    ("新北市板橋區新北市板橋區文化路一段1號", expected("新北市", "板橋區", "文化路一段", "1")),
    ("新北市板橋區臺北縣板橋市文化路一段1號", expected("新北市", "板橋區", "文化路一段", "1")),
    ("桃園市桃園區桃園縣桃園市永順街99巷7號", expected("桃園市", "桃園區", "永順街99巷", "7")),
])
def test_repeated_county_and_town_are_dropped_and_query_text_round_trips(address, want):
    from lvr_pipeline.tgos.batches import key_query_text

    key = building_key(address)
    assert json.loads(key[3:]) == want
    text = key_query_text(key)
    assert building_key(text) == key


def test_city_plus_town_repeat_stays_invalid_admin():
    assert building_key("臺南市東區市東區文聖里6鄰裕農一街186號八樓之8") is None
