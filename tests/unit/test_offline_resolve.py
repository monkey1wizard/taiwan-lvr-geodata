# -*- coding: utf-8 -*-
"""lvr_pipeline.offline.resolve 驗收測試。"""
import os
import sys
import csv
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from lvr_pipeline.offline.resolve import (
    resolve,
    load_35321,
    build_offline_road_set,
    _make_road_pattern,
    _find_candidates,
)

_PUA1 = chr(0xE001)  # 槺
_PUA2 = chr(0xE01C)  # 雙


# ── _make_road_pattern ─────────────────────────────────────────────────────────

def test_pattern_single_pua():
    road = _PUA1 + "榔路"
    pat = _make_road_pattern(road)
    assert pat is not None
    assert pat.match("槺榔路")
    assert not pat.match("XX路")


def test_pattern_multi_pua_returns_none():
    road = _PUA1 + "榔" + _PUA2 + "路"
    assert _make_road_pattern(road) is None


def test_pattern_no_pua_returns_none():
    assert _make_road_pattern("力行街") is None


# ── literal ?(U+003F)當 garble，與 PUA 一視同仁 ─────────────────────────────

def test_pattern_single_question_mark():
    pat = _make_road_pattern("公?路")
    assert pat is not None
    assert pat.match("公舘路")


def test_pattern_multi_question_mark_returns_none():
    assert _make_road_pattern("公??路") is None


def test_resolve_question_mark_tier1_on_coord_hit():
    tier, cand, _ = resolve("臺北市北投區公?路100號", {"公舘路"}, {}, lambda a: True)
    assert tier == "tier1_apply"
    assert "公舘路" in cand


def test_resolve_question_mark_tier2_without_coord():
    tier, cand, _ = resolve("臺北市北投區公?路100號", {"公舘路"}, {}, lambda a: False)
    assert tier == "tier2_review"
    assert "公舘路" in cand


# ── _find_candidates ───────────────────────────────────────────────────────────

def test_candidates_from_offline():
    road = _PUA1 + "榔路"
    result = _find_candidates(road, {"槺榔路", "其他路"}, [])
    assert result == ["槺榔路"]


def test_candidates_from_35321():
    road = _PUA1 + "榔路"
    result = _find_candidates(road, set(), ["槺榔路"])
    assert result == ["槺榔路"]


def test_candidates_dedup_across_corpora():
    road = _PUA1 + "榔路"
    result = _find_candidates(road, {"槺榔路"}, ["槺榔路"])
    assert result == ["槺榔路"]


def test_candidates_multi_match():
    road = _PUA1 + "行街"
    result = _find_candidates(road, {"力行街", "義行街"}, [])
    assert len(result) == 2


def test_candidates_normalization_tai_trad():
    road = _PUA1 + "路"
    # corpus has 台/臺 variant — both normalize to same canonical form
    result = _find_candidates(road, {"槺路"}, ["槺路"])
    assert result == ["槺路"]


def test_candidates_arab_to_cjk_normalization():
    """道路序數 2段→二段 aligned in both road and corpus."""
    road = _PUA1 + "路2段"
    # corpus entry uses CJK: norm("路二段") == norm("路2段") after arab_to_cjk
    result = _find_candidates(road, {"槺路二段"}, [])
    assert result == ["槺路二段"]


# ── resolve ────────────────────────────────────────────────────────────────────

def _always_true(addr: str) -> bool:
    return True


def _always_false(addr: str) -> bool:
    return False


def test_resolve_tier1_apply():
    """Single PUA in road, coord probe true → tier1_apply."""
    addr = f"臺北市士林區{_PUA1}榔路10號"
    offline = {"槺榔路"}
    roads_35321 = {"臺北市士林區": ["槺榔路"]}
    tier, candidate, evidence = resolve(addr, offline, roads_35321, _always_true)
    assert tier == "tier1_apply"
    assert "槺榔路" in candidate
    assert "offline_coord_hit" in evidence


def test_resolve_tier2_no_coord():
    """Single PUA, single match, coord probe false → tier2_review."""
    addr = f"臺北市士林區{_PUA1}榔路10號"
    offline = {"槺榔路"}
    roads_35321: dict = {}
    tier, candidate, evidence = resolve(addr, offline, roads_35321, _always_false)
    assert tier == "tier2_review"
    assert "槺榔路" in candidate
    assert "corpus_match_no_coord" in evidence


def test_resolve_tier2_gap_county():
    """Gap county: offline_roads empty, 35321 has match, coord probe false → tier2_review."""
    addr = f"南投縣草屯鎮{_PUA1}榔路5號"
    offline: set = set()  # gap county — no taiwan-address-data coverage
    roads_35321 = {"南投縣草屯鎮": ["槺榔路"]}
    tier, candidate, evidence = resolve(addr, offline, roads_35321, _always_false)
    assert tier == "tier2_review"
    assert "corpus_match_no_coord" in evidence


def test_resolve_no_pua_returns_none():
    addr = "臺北市士林區力行街10號"
    tier, candidate, evidence = resolve(addr, set(), {}, _always_true)
    assert tier == "none"
    assert evidence == "no-pua-in-road"


def test_resolve_multi_pua_returns_none():
    addr = f"臺北市士林區{_PUA1}榔{_PUA2}路10號"
    tier, candidate, evidence = resolve(addr, set(), {}, _always_true)
    assert tier == "none"
    assert evidence == "multi-pua"


def test_resolve_multi_match_returns_none():
    """Multiple corpus matches → ambiguous → none."""
    addr = f"臺北市士林區{_PUA1}行街10號"
    offline = {"力行街", "義行街"}
    roads_35321: dict = {}
    tier, candidate, evidence = resolve(addr, offline, roads_35321, _always_true)
    assert tier == "none"
    assert "multi-match" in evidence


def test_resolve_no_corpus_match_returns_none():
    """No corpus road matches → none."""
    addr = f"臺北市士林區{_PUA1}榔路10號"
    offline: set = set()
    roads_35321: dict = {}
    tier, candidate, evidence = resolve(addr, offline, roads_35321, _always_true)
    assert tier == "none"
    assert evidence == "no-corpus-match"


def test_resolve_candidate_address_substitution():
    """Candidate address replaces PUA with correct char."""
    addr = f"臺北市士林區{_PUA1}榔路二段5號"
    offline = {"槺榔路"}
    roads_35321: dict = {}
    tier, candidate, evidence = resolve(addr, offline, roads_35321, _always_true)
    assert tier == "tier1_apply"
    assert candidate == "臺北市士林區槺榔路二段5號"
    assert _PUA1 not in candidate


# ── load_35321 ─────────────────────────────────────────────────────────────────

def test_load_35321_basic():
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8-sig", suffix=".csv", delete=False, newline=""
    ) as f:
        f.write("city,site_id,road\n")
        f.write("臺北市,臺北市士林區,力行街\n")
        f.write("臺北市,臺北市士林區,槺榔路\n")
        f.write("新北市,新北市板橋區,文化路\n")
        name = f.name
    try:
        index = load_35321(name)
        assert "臺北市士林區" in index
        assert set(index["臺北市士林區"]) == {"力行街", "槺榔路"}
        assert "新北市板橋區" in index
    finally:
        os.unlink(name)


def test_load_35321_missing_file():
    index = load_35321("/nonexistent/path.csv")
    assert index == {}


# ── build_offline_road_set ────────────────────────────────────────────────────

def test_build_offline_road_set():
    with tempfile.TemporaryDirectory() as tmp:
        roads_dir = os.path.join(tmp, "roads")
        os.makedirs(roads_dir)
        open(os.path.join(roads_dir, "63-力行街.csv"), "w").close()
        open(os.path.join(roads_dir, "63-槺榔路.csv"), "w").close()
        open(os.path.join(roads_dir, "65-文化路.csv"), "w").close()
        result = build_offline_road_set(tmp, "63")
        assert result == {"力行街", "槺榔路"}


def test_build_offline_road_set_missing_dir():
    result = build_offline_road_set("/nonexistent", "63")
    assert result == set()


# ── R04-7 採用條件 ─────────────────────────────────────────────────────────────

def test_resolve_several_candidates_is_not_adopted():
    """多個候選：不採用、不取第一個；結果交由覆核（候選位址為空）。"""
    addr = f"臺北市士林區{_PUA1}行街10號"
    tier, candidate, evidence = resolve(addr, {"力行街", "義行街"}, {}, _always_true)
    assert tier != "tier1_apply"
    assert candidate == ""
    assert evidence == "multi-match:2"


def test_resolve_unique_candidate_without_door_number_is_pending_review():
    """唯一候選但沒有有效門牌證據：即使座標探測為真也不採用。"""
    for addr in (
        f"臺北市士林區{_PUA1}榔路",
        f"臺北市士林區{_PUA1}榔路10-2號",
    ):
        tier, candidate, evidence = resolve(addr, {"槺榔路"}, {}, _always_true)
        assert tier == "tier2_review", addr
        assert "槺榔路" in candidate
        assert evidence == "corpus_match_no_door:槺榔路"


def test_resolve_unique_candidate_with_door_number_is_adopted_with_evidence():
    """唯一候選且有門牌證據：採用，並保存候選與依據。"""
    addr = f"臺北市士林區{_PUA1}榔路10號"
    tier, candidate, evidence = resolve(
        addr, {"槺榔路"}, {"臺北市士林區": ["槺榔路"]}, _always_true
    )
    assert tier == "tier1_apply"
    assert candidate == "臺北市士林區槺榔路10號"
    assert evidence == "offline_coord_hit:槺榔路"
