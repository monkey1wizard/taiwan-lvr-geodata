# -*- coding: utf-8 -*-
"""Unit tests for lvr_pipeline.garbled — load_garbled / fix_garbled public API."""
import os
import tempfile

import pytest

from lvr_pipeline.addresses.characters import fix_garbled, load_garbled


def _write_garbled(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(content)


def test_char_kind_pua_substitution():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "garbled.csv")
        _write_garbled(p, "garbled,correct,scope,kind,evidence,noted_date\n"
                          "U+E001,槺,,char,test,2026-01-01\n")
        char_map, token_patterns = load_garbled(p)
        pua = chr(0xE001)
        result = fix_garbled(f"臺中市梧棲區{pua}榔七街10號", char_map, token_patterns)
        assert "槺" in result
        assert pua not in result


def test_variant_kind_substitution():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "garbled.csv")
        _write_garbled(p, "garbled,correct,scope,kind,evidence,noted_date\n"
                          "巿,市,,variant,test,2026-01-01\n")
        char_map, token_patterns = load_garbled(p)
        result = fix_garbled("屏東巿竹田鄉竹田路10號", char_map, token_patterns)
        assert "市" in result
        assert "巿" not in result


def test_token_kind_substitution():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "garbled.csv")
        _write_garbled(p, "garbled,correct,scope,kind,evidence,noted_date\n"
                          "東洋新?,東洋新邨,,token,test,2026-01-01\n")
        char_map, token_patterns = load_garbled(p)
        pua = chr(0xE001)
        result = fix_garbled(f"臺北市北投區東洋新{pua}街10號", char_map, token_patterns)
        assert "東洋新邨" in result


def test_token_kind_matches_literal_question_mark():
    """token ? 須比對真實資料裡的 literal ?(U+003F)，非僅 PUA。"""
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "garbled.csv")
        _write_garbled(p, "garbled,correct,scope,kind,evidence,noted_date\n"
                          "東洋新?,東洋新邨,,token,test,2026-01-01\n")
        char_map, token_patterns = load_garbled(p)
        result = fix_garbled("臺北市北投區東洋新?街10號", char_map, token_patterns)
        assert "東洋新邨" in result
        assert "?" not in result


def test_real_registry_question_mark_rules_fire():
    """回歸：config/rules 內既有 ? token 規則對 literal ? 生效，乾淨地址不動。"""
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    char_map, token_patterns = load_garbled(os.path.join(repo, "config", "rules", "character-fixes.csv"))
    assert "公舘路" in fix_garbled("臺北市北投區公?路100號", char_map, token_patterns)
    assert fix_garbled("臺北市北投區公館路100號", char_map, token_patterns) == "臺北市北投區公館路100號"


def test_token_scope_blocks_non_matching_district():
    """token 規則的 scope 須比對地址，不符合就不套用。"""
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "garbled.csv")
        _write_garbled(p, "garbled,correct,scope,kind,evidence,noted_date\n"
                          "公?路,公舘路,北投區,token,test,2026-01-01\n")
        char_map, token_patterns = load_garbled(p)
        pua = chr(0xE0C6)
        # 北投區 → 應替換
        assert "公舘路" in fix_garbled(f"臺北市北投區公{pua}路1號", char_map, token_patterns)
        # 臺中市中區 → 不替換（scope 不符）
        assert fix_garbled("臺中市中區公園路21號", char_map, token_patterns) == "臺中市中區公園路21號"


def test_missing_file_is_noop():
    char_map, token_patterns = load_garbled("/nonexistent/garbled.csv")
    addr = f"臺北市北投區{chr(0xE001)}路10號"
    assert fix_garbled(addr, char_map, token_patterns) == addr


def test_all_three_kinds_combined():
    """All three kinds in one table: char + variant + token all applied."""
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "garbled.csv")
        _write_garbled(p, "garbled,correct,scope,kind,evidence,noted_date\n"
                          "U+E001,槺,,char,test,2026-01-01\n"
                          "巿,市,,variant,test,2026-01-01\n"
                          "東洋新?,東洋新邨,,token,test,2026-01-01\n")
        char_map, token_patterns = load_garbled(p)

        pua = chr(0xE001)
        assert fix_garbled(f"臺中市梧棲區{pua}榔七街10號", char_map, token_patterns) == "臺中市梧棲區槺榔七街10號"
        assert fix_garbled("屏東巿竹田鄉竹田路10號", char_map, token_patterns) == "屏東市竹田鄉竹田路10號"
        pua2 = chr(0xE500)
        assert fix_garbled(f"臺北市北投區東洋新{pua2}街10號", char_map, token_patterns) == "臺北市北投區東洋新邨街10號"
        assert fix_garbled("普通地址無需補正", char_map, token_patterns) == "普通地址無需補正"


def test_status_column_is_loaded_and_does_not_change_replacements():
    import csv
    import tempfile
    from lvr_pipeline.addresses.characters import load_statuses
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(repo, "config", "rules", "character-fixes.csv")
    statuses = load_statuses(path)
    assert len(statuses) == 21 and set(statuses.values()) == {"unverified"}
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    with tempfile.TemporaryDirectory() as tmp:
        stripped = os.path.join(tmp, "no-status.csv")
        with open(stripped, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["garbled", "correct", "scope", "kind", "evidence", "noted_date"])
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row[k] for k in writer.fieldnames})
        assert load_garbled(stripped)[0] == load_garbled(path)[0]
        assert [(p.pattern, d, s) for p, d, s in load_garbled(stripped)[1]] == \
               [(p.pattern, d, s) for p, d, s in load_garbled(path)[1]]
        bad = os.path.join(tmp, "bad.csv")
        with open(bad, "w", encoding="utf-8", newline="") as f:
            f.write("garbled,correct,scope,kind,evidence,noted_date,status\n体,體,,variant,x,2026-01-01,maybe\n")
        with pytest.raises(ValueError):
            load_garbled(bad)
