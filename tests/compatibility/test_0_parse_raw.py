# -*- coding: utf-8 -*-
"""Unit tests for 0_parse_raw.py — _classify/_expand_list (structural expansion only).

Garbled-correction tests have moved to test_garbled.py since that logic
now lives in lvr_pipeline/addresses/characters.py (Step 1 normalize, not Step 0).
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

_spec = importlib.util.spec_from_file_location(
    "_0_parse_raw",
    os.path.join(os.path.dirname(__file__), "..", "..", "lvr_pipeline", "0_parse_raw.py"),
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]
_classify = _mod._classify


def test_no_garbled_references_in_0_parse_raw():
    """0_parse_raw must not contain any garbled-correction logic."""
    src = os.path.join(os.path.dirname(__file__), "..", "..", "lvr_pipeline", "0_parse_raw.py")
    with open(src, encoding="utf-8-sig") as f:
        content = f.read()
    for symbol in ("_fix_garbled", "_load_garbled", "GARBLED_PATH"):
        assert symbol not in content, f"Found forbidden symbol {symbol!r} in 0_parse_raw.py"


# ── _classify / _expand_list (RC-5+6) ─────────────────────────────────────────

def test_expand_list_no_double_hao():
    """130號 continuation: no 號號; 126巷7號 inherits full road prefix."""
    kind, addrs = _classify("桃園市平鎮區湧光路128、130號、126巷7號")
    assert kind == "list", f"expected list, got {kind}"
    assert "桃園市平鎮區湧光路128號" in addrs
    assert "桃園市平鎮區湧光路130號" in addrs
    assert "桃園市平鎮區湧光路126巷7號" in addrs
    assert not any("號號" in a for a in addrs), f"double-號 found: {addrs}"


def test_expand_list_lane_inherits_full_prefix():
    """巷 fragment does NOT trigger road-change; inherits prefix including road."""
    kind, addrs = _classify("桃園市平鎮區湧光路128、126巷7號")
    assert kind == "list"
    matching = [a for a in addrs if "126巷7號" in a]
    assert matching, f"126巷7號 not in {addrs}"
    assert matching[0].startswith("桃園市平鎮區湧光路"), f"prefix missing: {matching[0]}"


def test_expand_list_road_change_gets_city_prefix():
    """Road-change fragment (路) without city gets city_town prepended."""
    kind2, addrs2 = _classify("臺北市萬華區峨眉街139、141號")
    assert kind2 == "list"
    assert "臺北市萬華區峨眉街139號" in addrs2
    assert "臺北市萬華區峨眉街141號" in addrs2


def test_mixed_list_sep_expands_with_road_change():
    """Mixed 、+；: each door fully expands; road-change fragment gets city prefix."""
    kind, addrs = _classify("臺北市萬華區峨眉街139、141、143號；環河南路一段75號")
    assert kind == "list", f"expected list, got {kind} addrs={addrs}"
    assert "臺北市萬華區峨眉街139號" in addrs
    assert "臺北市萬華區峨眉街141號" in addrs
    assert "臺北市萬華區峨眉街143號" in addrs
    road_change = [a for a in addrs if "環河南路" in a]
    assert road_change, f"環河南路 entry missing: {addrs}"
    assert road_change[0] == "臺北市萬華區環河南路一段75號", f"got {road_change[0]}"


def test_semicolon_only_every_token_not_expanded():
    """；-only every-token case (each token ends in 號): carve-out, NOT expanded."""
    kind, addrs = _classify("得才街51號；得才街53號")
    assert kind == "single", f"expected single (carve-out), got {kind} addrs={addrs}"
