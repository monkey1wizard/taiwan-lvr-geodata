"""TGOS adoption rules 1 and 3 and the county-reform village rule (docs/地址規則.md)."""

import pytest

from lvr_pipeline.addresses.identity import building_key
from lvr_pipeline.tgos.adopt import same_door_for_adoption
from lvr_pipeline.transactions.normalize import normalize_address


def adopted(query: str, response: str) -> bool:
    return same_door_for_adoption(building_key(query), building_key(normalize_address(response)))


@pytest.mark.parametrize(
    ("query", "response"),
    [
        ("臺南市南區新忠路1之1號", "臺南市南區彰南里11鄰新忠路1號之1"),
        ("彰化縣彰化市埔北街238之4號", "彰化縣彰化市延平里18鄰埔北街238號之4"),
        ("臺南市南區新忠路臨10號之1", "臺南市南區新忠路臨10之1號"),
        ("臺南市南區新忠路臨10之1號", "臺南市南區新忠路臨10號之1"),
    ],
)
def test_door_notation_adopted(query, response):
    assert adopted(query, response)


@pytest.mark.parametrize(
    ("query", "response"),
    [
        ("苗栗縣三義鄉西湖村西湖11之1號", "苗栗縣三義鄉西湖村1鄰西湖11之2號"),
        ("臺北市中山區農安街125巷18之2號", "臺北市中山區農安街125巷18號"),
        ("新北市三重區重新路四段101之1之2號", "新北市三重區重新路四段101之1號"),
        ("嘉義縣水上鄉三和村頂寮1之4號", "嘉義縣水上鄉三和村頂寮1號附5"),
    ],
)
def test_door_notation_rejected(query, response):
    assert not adopted(query, response)


def test_keys_are_unchanged():
    assert building_key("臺南市南區新忠路1之1號") != building_key("臺南市南區新忠路1號之1")


@pytest.mark.parametrize(
    ("query", "response"),
    [
        ("彰化縣彰化市南瑤里天祥路30巷1號", "彰化縣彰化市南瑶里22鄰天祥路30巷1號"),
        ("嘉義縣民雄鄉豐收村好收116之5號", "嘉義縣民雄鄉豊收村12鄰好收116號之5"),
        ("嘉義縣民雄鄉雙福村好收1號", "嘉義縣民雄鄉双福村3鄰好收1號"),
        ("高雄市燕巢區橫山村裕農巷10之4號", "高雄市燕巢區橫山里5鄰裕農巷10之4號"),
    ],
)
def test_village_adopted(query, response):
    assert adopted(query, response)


def test_county_reform_codes_are_the_real_ones():
    from lvr_pipeline.tgos.adopt import _village_index

    names = _village_index()[("64", "燕巢區")]
    assert set(names["橫山村"]) == {"1001210-010"}
    assert set(names["橫山里"]) == {"6402100-010"}


@pytest.mark.parametrize(
    ("query", "response"),
    [
        ("雲林縣麥寮鄉瓦傜村霄仁86號", "雲林縣麥寮鄉瓦磘村12鄰霄仁86號"),
        ("彰化縣彰化市南瑤里天祥路30巷1號", "彰化縣彰化市南瑶里22鄰天祥路30巷2號"),
        ("彰化縣彰化市南瑤里天祥路30巷1號", "彰化縣彰化市天祥路30巷1號"),
        ("彰化縣彰化市南瑤里22鄰天祥路30巷1號", "彰化縣彰化市南瑶里23鄰天祥路30巷1號"),
        ("嘉義縣民雄鄉豐收村好收116之5號", "嘉義縣民雄鄉雙福村12鄰好收116號之5"),
    ],
)
def test_village_rejected(query, response):
    assert not adopted(query, response)


def test_same_name_in_another_town_is_not_the_same_village():
    from lvr_pipeline.tgos.adopt import _same_village

    assert not _same_village("10010", "民雄鄉", "豐收村", "雙福村")
    assert not _same_village("10010", "不存在鄉", "豐收村", "豊收村")


# c41: a response without the final 號 is read with 號 appended; its key is not stored.
def test_response_key_appends_the_missing_hao():
    from lvr_pipeline.tgos.adopt import response_key

    assert response_key("苗栗縣頭份市尖山里14鄰尖山141之25") == building_key("苗栗縣頭份市尖山里14鄰尖山141之25號")
    assert response_key("苗栗縣頭份市尖山里14鄰尖山141之25號") == building_key("苗栗縣頭份市尖山里14鄰尖山141之25號")
    assert response_key("苗栗縣頭份市尖山里14鄰尖山") is None
    assert response_key(None) is None


def test_second_review_response_key_uses_the_same_rule():
    from lvr_pipeline.results.second_review import _response_key

    key, why = _response_key("苗栗縣頭份市尖山里14鄰尖山141之25")
    assert key == building_key("苗栗縣頭份市尖山里14鄰尖山141之25號") and why is None
    assert _response_key("苗栗縣頭份市尖山里14鄰尖山") == (None, "building_key returned None")


# c42: ㄧ (U+3127) is read as 一 (U+4E00) in adoption only; the keys differ.
def test_bopomofo_one_is_read_as_the_numeral():
    assert adopted("彰化縣二林鎮正知ㄧ街15號", "彰化縣二林鎮後厝里9鄰正知一街15號")
    assert not adopted("彰化縣二林鎮正知ㄧ街15號", "彰化縣二林鎮後厝里9鄰正知二街15號")
    assert building_key("彰化縣二林鎮正知ㄧ街15號") != building_key("彰化縣二林鎮正知一街15號")
