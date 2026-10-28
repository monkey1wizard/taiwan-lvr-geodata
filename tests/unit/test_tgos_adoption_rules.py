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


# c46, rule 10: 台 is read as 臺 in adoption only; the keys differ.
def test_tai_is_read_as_the_standard_character():
    assert adopted("臺中市西屯區台灣大道三段282號", "臺中市西屯區惠來里1鄰臺灣大道三段282號")
    assert adopted("彰化縣彰化市台化街2之4號", "彰化縣彰化市中庄里1鄰臺化街2號之4")
    assert not adopted("彰化縣彰化市台化街2之4號", "彰化縣彰化市中庄里1鄰臺化街2號之5")
    assert building_key("彰化縣彰化市台化街2號") != building_key("彰化縣彰化市臺化街2號")


# c47, rule 11: a village one character away from the TGOS village, which is a current village of the
# town while the query village is not, is the same village.
@pytest.mark.parametrize(
    ("query", "response"),
    [
        ("彰化縣伸港鄉泉卅村中山路244號", "彰化縣伸港鄉泉州村16鄰中山路244號"),
        ("雲林縣麥寮鄉瓦傜村霄仁86號", "雲林縣麥寮鄉瓦磘村12鄰霄仁86號"),
        ("桃園市平鎮區北勢村11鄰和平路31巷35號", "桃園市平鎮區北勢里11鄰和平路31巷35號"),
    ],
)
def test_one_character_village_takes_the_tgos_name(query, response):
    assert adopted(query, response)


@pytest.mark.parametrize(
    ("query", "response"),
    [
        ("臺南市下營區茅港里茅港尾235之12號", "臺南市下營區茅營里5鄰茅港尾235之12號"),
        ("桃園市平鎮區北勢村11鄰和平路31巷35號", "桃園市平鎮區南勢里11鄰和平路31巷35號"),
        ("彰化縣伸港鄉泉卅村中山路244號", "彰化縣伸港鄉泉州村16鄰中山路245號"),
    ],
)
def test_one_character_village_rejected(query, response):
    assert not adopted(query, response)


# c49: the layer ambiguity check merges village names that rule 1 or the county-reform rule read as one.
def _layers(*addresses):
    from lvr_pipeline.addresses.identity import door_parts

    layers = {}
    for address in addresses:
        key = building_key(address)
        signature, village, neighborhood = door_parts(key)
        layers.setdefault(signature, []).append((key, village, neighborhood))
    return layers


def test_old_and_new_names_of_one_village_are_not_ambiguous():
    from lvr_pipeline.tgos.validate import _layer_is_ambiguous

    layers = _layers("臺南市西港區慶安村新興街8之185號", "臺南市西港區慶安里新興街8之185號")
    assert not _layer_is_ambiguous(building_key("臺南市西港區新興街8之185號"), layers)


def test_two_villages_are_still_ambiguous():
    from lvr_pipeline.tgos.validate import _layer_is_ambiguous

    layers = _layers("臺南市永康區大橋里大橋一街30之2號", "臺南市永康區西橋里大橋一街30之2號")
    assert _layer_is_ambiguous(building_key("臺南市永康區大橋一街30之2號"), layers)


def test_one_village_with_two_neighbourhoods_is_still_ambiguous():
    from lvr_pipeline.tgos.validate import _layer_is_ambiguous

    layers = _layers("桃園市大園區埔心村22鄰埔心96之19號", "桃園市大園區埔心里29鄰埔心96之19號")
    assert _layer_is_ambiguous(building_key("桃園市大園區埔心96之19號"), layers)
