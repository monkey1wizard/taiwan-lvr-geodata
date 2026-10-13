"""Independent specification test for building_key, derived only from docs/地址規則.md."""
from __future__ import annotations

import json

import pytest

from lvr_pipeline.addresses.identity import building_key


def E(county, town, road, door, village=None, nb=None):
    d = {"county": county, "town": town, "locality_road": road, "door": door}
    if village is not None:
        d["village"] = village
    if nb is not None:
        d["neighborhood"] = nb
    return d


def amb(case_id, address, why, quote):
    return pytest.param(case_id, address, None, quote,
                        marks=pytest.mark.skip(reason="ambiguous: " + why), id=case_id)


Q_FIELDS = "含六欄"
Q_DOOR = "寫法保留，例如 `10`、`10之1`、`10號之1`、`30附1`、`6號附9`、`臨201`"
Q_CJK = "國字主號轉為阿拉伯數字，全形英文字母轉半形"
Q_FLOOR = "樓、樓之、樓後接數字、室、地下室、地下層、地下 N 樓或層、底層及括號註記不參與門牌辨識"
Q_RANGE = "只有在前一字為數字或地址單位時才視為區間符號"
Q_VILL = "地址全文寫出的村里，位於區名之後、街路之前。未寫時不出現"
Q_NB = "`006鄰` 與 `六鄰` 都表示 `6`，未寫時不出現"
Q_HC = "`I-`、`G-`、`H-` 編碼目前無法建立鍵"
Q_LI = "以「里」結尾的鄉名也可解析"
Q_REP = "只刪除完全重複的縣市名"
Q_A = "區名必須存在於該縣市"
Q_B = "鄉、鎮、市改為區，區名不變"
Q_BX = "鄉鎮改名或拆併、`嘉義市市世賢路` 這類錯字不對應，仍記 `invalid_admin`"

BASE_E = E("63", "大安區", "忠孝東路", "100")

CASES = [
    # --- six fields / door forms ---
    ("basic", "臺北市大安區忠孝東路100號", BASE_E, Q_FIELDS),
    ("basic_new_taipei", "新北市板橋區文化路一段25號", E("65", "板橋區", "文化路一段", "25"), Q_FIELDS),
    ("basic_taichung", "臺中市西屯區臺灣大道三段301號", E("66", "西屯區", "臺灣大道三段", "301"), Q_FIELDS),
    ("basic_taiwan_alt_char", "台北市大安區忠孝東路100號", BASE_E, "county 縣市代碼 (台/臺 same code)"),
    ("door_plain", "高雄市新興區中山一路10號", E("64", "新興區", "中山一路", "10"), Q_DOOR),
    ("door_zhi", "臺北市大安區忠孝東路10之1號", E("63", "大安區", "忠孝東路", "10之1"), Q_DOOR),
    ("door_hao_zhi", "臺北市大安區忠孝東路10號之1", E("63", "大安區", "忠孝東路", "10號之1"), Q_DOOR),
    ("door_fu_after_digit", "臺北市大安區復興南路30附1號", E("63", "大安區", "復興南路", "30附1"), Q_DOOR),
    ("door_hao_fu", "臺北市大安區復興南路6號附9", E("63", "大安區", "復興南路", "6號附9"), Q_DOOR),
    ("door_lin", "臺北市大安區復興南路臨201號", E("63", "大安區", "復興南路", "臨201"), Q_DOOR),
    ("door_jian", "臺北市大安區復興南路建123號", E("63", "大安區", "復興南路", "建123"), "臨、建、特及號"),
    ("door_te", "臺北市大安區復興南路特5號", E("63", "大安區", "復興南路", "特5"), "臨、建、特及號"),
    ("lane_alley", "臺北市大安區和平東路二段39巷15弄3號",
     E("63", "大安區", "和平東路二段39巷15弄", "3"), "再加巷、弄、衖、衕"),
    ("lane_only", "臺北市大安區和平東路二段39巷15號",
     E("63", "大安區", "和平東路二段39巷", "15"), "再加巷、弄、衖、衕"),
    # --- CJK / full width ---
    ("cjk_main_100", "臺北市大安區忠孝東路一百號", BASE_E, Q_CJK),
    ("cjk_main_25", "新北市板橋區文化路一段二十五號", E("65", "板橋區", "文化路一段", "25"), Q_CJK),
    ("cjk_main_10", "高雄市新興區中山一路十號", E("64", "新興區", "中山一路", "10"), Q_CJK),
    ("fullwidth_letter", "臺北市大安區和平東路二段12Ａ號",
     E("63", "大安區", "和平東路二段", "12A"), Q_CJK),
    # --- ignored floor/room text ---
    ("floor_lou", "臺北市大安區忠孝東路100號5樓", BASE_E, Q_FLOOR),
    ("floor_lou_zhi", "臺北市大安區忠孝東路100號5樓之3", BASE_E, Q_FLOOR),
    ("floor_lou_digit", "臺北市大安區忠孝東路100號5樓2", BASE_E, Q_FLOOR),
    ("floor_room", "臺北市大安區忠孝東路100號5樓503室", BASE_E, Q_FLOOR),
    ("floor_room_only", "臺北市大安區忠孝東路100號503室", BASE_E, Q_FLOOR),
    ("floor_basement_room", "臺北市大安區忠孝東路100號地下室", BASE_E, Q_FLOOR),
    ("floor_basement_layer", "臺北市大安區忠孝東路100號地下層", BASE_E, Q_FLOOR),
    ("floor_basement_n_lou", "臺北市大安區忠孝東路100號地下2樓", BASE_E, Q_FLOOR),
    ("floor_basement_n_ceng", "臺北市大安區忠孝東路100號地下二層", BASE_E, Q_FLOOR),
    ("floor_ground", "臺北市大安區忠孝東路100號底層", BASE_E, Q_FLOOR),
    ("bracket_halfwidth", "臺北市大安區忠孝東路100號(後棟)", BASE_E, Q_FLOOR),
    ("bracket_fullwidth", "臺北市大安區忠孝東路100號（後棟）", BASE_E, Q_FLOOR),
    ("floor_with_zhi_door", "臺北市大安區忠孝東路100號之1三樓",
     E("63", "大安區", "忠孝東路", "100號之1"), Q_FLOOR),
    ("floor_with_zhi_door2", "臺北市大安區忠孝東路100之1號3樓",
     E("63", "大安區", "忠孝東路", "100之1"), Q_FLOOR),
    # --- range symbols inside names ---
    ("road_with_yu", "高雄市三民區建國與民族路5號", E("64", "三民區", "建國與民族路", "5"), Q_RANGE),
    ("road_with_ji", "高雄市三民區中山及大同路5號", E("64", "三民區", "中山及大同路", "5"), Q_RANGE),
    ("road_with_zhi", "高雄市三民區建國至民族路5號", E("64", "三民區", "建國至民族路", "5"), Q_RANGE),
    ("range_zhi_door", "臺北市大安區忠孝東路100至102號", None, Q_RANGE),
    ("road_starting_zhi", "臺北市士林區至善路二段221號", E("63", "士林區", "至善路二段", "221"), Q_RANGE),
    # --- village / neighbourhood ---
    ("village", "高雄市鳳山區文衡里中正路10號", E("64", "鳳山區", "中正路", "10", village="文衡里"), Q_VILL),
    ("village_nb_digit", "臺北市大安區仁愛里006鄰忠孝東路100號",
     E("63", "大安區", "忠孝東路", "100", village="仁愛里", nb="6"), Q_NB),
    ("village_nb_cjk", "臺北市大安區仁愛里六鄰忠孝東路100號",
     E("63", "大安區", "忠孝東路", "100", village="仁愛里", nb="6"), Q_NB),
    ("nb_cjk_twelve", "臺北市大安區仁愛里十二鄰忠孝東路100號",
     E("63", "大安區", "忠孝東路", "100", village="仁愛里", nb="12"), Q_NB),
    ("nb_only", "臺北市大安區六鄰忠孝東路100號", E("63", "大安區", "忠孝東路", "100", nb="6"), Q_NB),
    ("village_only_empty_road", "花蓮縣秀林鄉富世村1號",
     E("10015", "秀林鄉", "", "1", village="富世村"), "只有村里時為空字串"),
    # --- Hsinchu county codes ---
    ("hc_I", "新竹縣竹北市I-5號", None, Q_HC),
    ("hc_G", "新竹縣新埔鎮G-3號", None, Q_HC),
    ("hc_H", "新竹縣湖口鄉H-7號", None, Q_HC),
    ("hc_normal_ok", "新竹縣竹北市中正西路100號", E("10004", "竹北市", "中正西路", "100"), Q_FIELDS),
    # --- town ending in 里 ---
    ("town_li", "臺東縣太麻里鄉中山路100號", E("10014", "太麻里鄉", "中山路", "100"), Q_LI),
    # --- repeated county ---
    ("repeat_county", "新竹市新竹市東區光復路二段101號", E("10018", "東區", "光復路二段", "101"), Q_REP),
    ("repeat_county_base", "新竹市東區光復路二段101號", E("10018", "東區", "光復路二段", "101"), Q_REP),
    ("repeat_county_taiwan_variant", "台北市臺北市大安區忠孝東路100號", BASE_E, Q_REP),
    # --- rule A ---
    ("A_tainan_typo", "臺南市市東區中華路10號", None, Q_A),
    ("A_kaohsiung_v", "高雄市v新興區中山一路10號", None, Q_A),
    ("A_wrong_county", "臺北市板橋區文化路一段25號", None, Q_A),
    ("A_wrong_county2", "新北市大安區忠孝東路100號", None, Q_A),
    ("A_pos_tainan", "臺南市東區中華東路一段10號", E("67", "東區", "中華東路一段", "10"), Q_A),
    ("A_pos_taoyuan", "桃園市中壢區中正路100號", E("68", "中壢區", "中正路", "100"), Q_A),
    ("A_pos_keelung", "基隆市仁愛區仁一路10號", E("10017", "仁愛區", "仁一路", "10"), Q_A),
    ("A_pos_chiayi_city", "嘉義市西區中山路100號", E("10020", "西區", "中山路", "100"), Q_A),
    ("A_pos_miaoli", "苗栗縣苗栗市中正路100號", E("10005", "苗栗市", "中正路", "100"), Q_A),
    ("A_pos_changhua", "彰化縣彰化市中山路二段100號", E("10007", "彰化市", "中山路二段", "100"), Q_A),
    ("A_pos_nantou", "南投縣埔里鎮中山路一段100號", E("10008", "埔里鎮", "中山路一段", "100"), Q_A),
    ("A_pos_yunlin", "雲林縣斗六市中山路100號", E("10009", "斗六市", "中山路", "100"), Q_A),
    ("A_pos_pingtung", "屏東縣屏東市中山路100號", E("10013", "屏東市", "中山路", "100"), Q_A),
    ("A_pos_yilan", "宜蘭縣羅東鎮中正路100號", E("10002", "羅東鎮", "中正路", "100"), Q_A),
    ("A_pos_penghu", "澎湖縣馬公市中正路100號", E("10016", "馬公市", "中正路", "100"), Q_A),
    ("A_pos_kinmen", "金門縣金城鎮民生路100號", E("09020", "金城鎮", "民生路", "100"), Q_A),
    ("A_pos_lienchiang", "連江縣南竿鄉介壽村100號", E("09007", "南竿鄉", "", "100", village="介壽村"), Q_A),
    ("A_north_district_taipei", "臺北市北區中山北路100號", None, Q_A),
    ("A_north_district_taichung", "臺中市北區三民路三段100號", E("66", "北區", "三民路三段", "100"), Q_A),
    ("A_north_district_hsinchu", "新竹市北區中正路100號", E("10018", "北區", "中正路", "100"), Q_A),
    ("A_bad_town_suffix", "臺中市西屯市臺灣大道三段301號", None, Q_A),
    # --- rule B ---
    ("B_banqiao", "臺北縣板橋市文化路一段25號", E("65", "板橋區", "文化路一段", "25"), Q_B),
    ("B_fengyuan", "臺中縣豐原市中正路100號", E("66", "豐原區", "中正路", "100"), Q_B),
    ("B_xinying", "臺南縣新營市中山路100號", E("67", "新營區", "中山路", "100"), Q_B),
    ("B_fengshan", "高雄縣鳳山市中山路100號", E("64", "鳳山區", "中山路", "100"), Q_B),
    ("B_zhongli", "桃園縣中壢市中正路100號", E("68", "中壢區", "中正路", "100"), Q_B),
    ("B_taoyuan", "桃園縣桃園市中正路100號", E("68", "桃園區", "中正路", "100"), Q_B),
    ("B_wulai", "臺北縣烏來鄉烏來街1號", E("65", "烏來區", "烏來街", "1"), Q_B),
    ("B_current_banqiao", "新北市板橋區文化路一段25號", E("65", "板橋區", "文化路一段", "25"), Q_B),
    ("B_current_wulai", "新北市烏來區烏來街1號", E("65", "烏來區", "烏來街", "1"), Q_B),
    ("B_current_taoyuan", "桃園市桃園區中正路100號", E("68", "桃園區", "中正路", "100"), Q_B),
    ("B_shulin_zhen", "臺北縣樹林鎮中山路100號", E("65", "樹林區", "中山路", "100"), Q_B),
    ("B_shulin_shi", "臺北縣樹林市中山路100號", E("65", "樹林區", "中山路", "100"), Q_B),
    ("B_chiayi_typo", "嘉義市市世賢路100號", None, Q_BX),
    ("B_renamed_sanmin", "高雄縣三民鄉民權村1號", None, Q_BX),
    ("B_renamed_namaxia", "高雄縣那瑪夏鄉民權村1號", None, Q_BX),
    ("B_old_county_wrong_town", "臺北縣豐原市中正路100號", None, Q_A),
]


def _params():
    return [c if hasattr(c, "values") else pytest.param(*c, id=c[0]) for c in CASES]


@pytest.mark.parametrize("case_id,address,expected,quote", _params())
def test_building_key_spec(case_id, address, expected, quote):
    key = building_key(address)
    if expected is None:
        assert key is None, f"{quote}: expected None, got {key}"
        return
    assert key is not None, f"{quote}: expected {expected}, got None"
    assert key.startswith("v2:")
    assert json.loads(key[3:]) == expected, quote


INEQUAL = [
    ("village_differs", "臺北市大安區仁愛里忠孝東路100號", "臺北市大安區敦化里忠孝東路100號"),
    ("village_vs_none", "臺北市大安區仁愛里忠孝東路100號", "臺北市大安區忠孝東路100號"),
    ("nb_differs", "臺北市大安區仁愛里006鄰忠孝東路100號", "臺北市大安區仁愛里007鄰忠孝東路100號"),
    ("nb_vs_none", "臺北市大安區仁愛里006鄰忠孝東路100號", "臺北市大安區仁愛里忠孝東路100號"),
    ("zhi_vs_hao_zhi", "臺北市大安區忠孝東路10之1號", "臺北市大安區忠孝東路10號之1"),
    ("fu_before_vs_after_hao", "臺北市大安區復興南路6附9號", "臺北市大安區復興南路6號附9"),
    ("lane_cjk_vs_arabic", "臺北市大安區和平東路二段十巷5號", "臺北市大安區和平東路二段10巷5號"),
]


@pytest.mark.parametrize("case_id,a,b", INEQUAL, ids=[c[0] for c in INEQUAL])
def test_keys_not_equal(case_id, a, b):
    ka, kb = building_key(a), building_key(b)
    assert ka is not None and kb is not None
    assert ka != kb


SAME_KEY = [
    ("banqiao_old_new", "臺北縣板橋市文化路一段25號", "新北市板橋區文化路一段25號"),
    ("shulin_zhen_shi", "臺北縣樹林鎮中山路100號", "臺北縣樹林市中山路100號"),
    ("shulin_new", "臺北縣樹林鎮中山路100號", "新北市樹林區中山路100號"),
    ("taoyuan_old_new", "桃園縣桃園市中正路100號", "桃園市桃園區中正路100號"),
    ("wulai_old_new", "臺北縣烏來鄉烏來街1號", "新北市烏來區烏來街1號"),
    ("repeat_taiwan_variant", "台北市臺北市大安區忠孝東路100號", "臺北市大安區忠孝東路100號"),
]


@pytest.mark.parametrize("case_id,a,b", SAME_KEY, ids=[c[0] for c in SAME_KEY])
def test_keys_equal(case_id, a, b):
    assert building_key(a) is not None
    assert building_key(a) == building_key(b)
