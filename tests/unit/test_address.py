# -*- coding: utf-8 -*-
"""lvr_pipeline.address 驗收測試。"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from lvr_pipeline.address import (
    _dedup_prefix,
    arab_to_cjk,
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
