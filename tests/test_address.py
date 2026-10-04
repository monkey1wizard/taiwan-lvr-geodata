# -*- coding: utf-8 -*-
"""lvr_pipeline.address 驗收測試。"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lvr_pipeline.address import (
    _dedup_prefix,
    arab_to_cjk,
    building_key,
    is_garbled,
    norm,
    parse,
)


# ── is_garbled（缺字 ? 與 PUA 造字）──────────────────────────────────────────

def test_is_garbled_detects_question_mark():
    # literal ?(U+003F，上游遺失字）
    assert is_garbled("桃園市雙?路186號") is True


def test_is_garbled_detects_pua():
    # PUA 造字（U+E000–U+F8FF）
    assert is_garbled("苗栗縣後龍鎮" + chr(0xE001) + "榔七街5號") is True


def test_is_garbled_false_for_clean():
    assert is_garbled("臺北市北投區公館路100號") is False


# ── arab_to_cjk ──────────────────────────────────────────────────────────────
# 錯誤輸入（阿拉伯）→ 官方正式（國字），只轉路/街/大道/段前序數

def test_arab_to_cjk_section():
    # 段序：錯誤輸入 "2段" → 官方 "二段"
    assert arab_to_cjk("建國路2段") == "建國路二段"


def test_arab_to_cjk_street_ordinal():
    # 街名序數：錯誤輸入 "24街" → 官方 "二十四街"
    assert arab_to_cjk("精誠24街") == "精誠二十四街"


def test_arab_to_cjk_no_change_when_already_cjk():
    # 官方形式不動
    assert arab_to_cjk("建國路二段") == "建國路二段"
    assert arab_to_cjk("精誠二十四街") == "精誠二十四街"


def test_arab_to_cjk_lane_number_unchanged():
    # 巷/號 前的數字不轉換
    assert arab_to_cjk("228巷") == "228巷"
    assert arab_to_cjk("111號") == "111號"


# ── norm ─────────────────────────────────────────────────────────────────────

def test_norm_shi_variant():
    result = norm("屏東巿")
    assert "市" in result
    assert "巿" not in result


def test_norm_fullwidth_digits():
    assert "1" in norm("１段")
    assert "２" not in norm("２段")


def test_norm_tai_to_official():
    # 台→臺：以官方離線庫形式為準
    assert norm("台北市") == "臺北市"
    assert norm("台中市") == "臺中市"
    assert norm("台南市") == "臺南市"
    assert norm("台東縣") == "臺東縣"


# ── parse ─────────────────────────────────────────────────────────────────────

def test_parse_taipei():
    code, town, road, tail = parse("臺北市北投區公館路228巷111號六樓")
    assert code == "63"
    assert town == "北投區"
    assert road == "公館路"
    assert "111號" in tail


def test_parse_with_li_lin():
    _, town, road, tail = parse("苗栗縣頭份市蟠桃里6鄰建國路二段280巷9號")
    assert town == "頭份市"
    assert road == "建國路"
    assert "9號" in tail


def test_parse_section_in_tail():
    _, _, road, tail = parse("臺北市中山區一江街148-3號")
    assert road == "一江街"
    assert "148" in tail


# ── building_key ──────────────────────────────────────────────────────────────

def test_building_key_drops_floor():
    k6 = building_key("臺北市北投區公館路228巷111號六樓")
    k7 = building_key("臺北市北投區公館路228巷111號七樓")
    assert k6 == k7
    assert k6.endswith("號")
    assert "樓" not in k6


def test_building_key_arabic_input_matches_cjk_db_form():
    # 錯誤輸入（阿拉伯全形/半形） → 歸一化成官方國字，與 DB 相符
    # "精誠２４街" 和 "精誠24街" 都是錯誤輸入；DB 存放 "精誠二十四街"
    k_fw  = building_key("臺南市東區精誠２４街10號")   # 全形 Arabic（錯）
    k_hw  = building_key("臺南市東區精誠24街10號")     # 半形 Arabic（錯）
    k_cjk = building_key("臺南市東區精誠二十四街10號") # 國字（正確/DB 形式）
    assert k_fw == k_cjk
    assert k_hw == k_cjk
    assert "二十四" in k_cjk   # canonical form 是國字


def test_building_key_arabic_section_matches_cjk():
    # 錯誤輸入 "建國路2段" → 歸一化成 "建國路二段"，與 DB 形式相符
    k_arabic = building_key("苗栗縣頭份市建國路2段280巷9號")
    k_cjk    = building_key("苗栗縣頭份市建國路二段280巷9號")
    assert k_arabic == k_cjk
    assert "二段" in k_cjk


def test_building_key_yi_to_zhi_typo():
    k = building_key("彰化縣彰化市中山路205巷3一3號")
    assert "3之3號" in k


def test_building_key_dash_to_zhi_when_m_le_n():
    assert "148之3號" in building_key("臺北市中山區一江街148-3號")


def test_building_key_empty_for_land_parcel():
    assert building_key("臺北市中山區農化段123地號") == ""
    assert building_key("臺中市西屯區惠民段7、9、9-1、9-2號地號等4筆") == ""


def test_building_key_empty_for_vague_location():
    assert building_key("臺南市佳里區下營156號對面") == ""
    assert building_key("高雄市大寮區鳳林三路492號旁") == ""


def test_building_key_empty_for_empty_input():
    assert building_key("") == ""


def test_building_key_strips_li_lin():
    # DB 有里鄰，raw input 無里鄰，兩者 building_key 應相同
    with_li = building_key("苗栗縣頭份市蟠桃里6鄰建國路二段280巷9號")
    without = building_key("苗栗縣頭份市建國路二段280巷9號")
    assert with_li == without


def test_building_key_dedup_prefix():
    assert building_key("北投區臺北市北投區公館路1號") == building_key("臺北市北投區公館路1號")


# ── _dedup_prefix loop-collapse ──────────────────────────────────────────────

def test_dedup_prefix_city_repeat():
    assert _dedup_prefix("嘉義市嘉義市東市路10號") == "嘉義市東市路10號"


def test_dedup_prefix_city_town_repeat():
    assert _dedup_prefix("新竹縣新埔鎮新竹縣新埔鎮信義路10號") == "新竹縣新埔鎮信義路10號"


def test_dedup_prefix_triple():
    assert _dedup_prefix("嘉義市嘉義市嘉義市東市路10號") == "嘉義市東市路10號"


def test_dedup_prefix_single_unchanged():
    assert _dedup_prefix("嘉義市東區東市路10號") == "嘉義市東區東市路10號"


def test_dedup_prefix_bare_city_no_town_unchanged():
    # 沒有行政區（市直接接路名）— 不應誤剝
    assert _dedup_prefix("新竹市信義街10號") == "新竹市信義街10號"


# ── TP-02b: building_key dual-key regression ─────────────────────────────────

def test_building_key_dup_prefix_equals_clean():
    dirty = building_key("嘉義市嘉義市東市路10號")
    clean = building_key("嘉義市東市路10號")
    assert dirty == clean
    assert dirty != ""


def test_building_key_tai_variant_normalizes_to_official():
    # 錯誤輸入 "台北市" → 歸一化成官方 "臺北市"，與 DB FULL_ADDR 相符
    k_wrong   = building_key("台北市北投區公館路1號")
    k_correct = building_key("臺北市北投區公館路1號")
    assert k_wrong == k_correct
    assert "臺北市" in k_correct
