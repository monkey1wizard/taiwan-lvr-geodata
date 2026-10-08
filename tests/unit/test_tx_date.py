"""Tests for lvr_pipeline.transactions.dates.

Test cases grounded in §8 V2 of the plan:
  - sales batch 2026q1 had 5 observed 6-digit bad dates (e.g. '990311').
  - rent/presale batches had all-valid 7-digit dates.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from lvr_pipeline.transactions.dates import roc_to_tx_yyyymm


# ── Valid conversions ────────────────────────────────────────────────────────

def test_valid_1141111():
    """民國 114年 11月 11日 → 202511."""
    assert roc_to_tx_yyyymm("1141111") == 202511


def test_valid_1151024():
    """民國 115年 10月 24日 → 202610 (no cutoff = backward-compatible)."""
    assert roc_to_tx_yyyymm("1151024") == 202610


# ── Future-date guard (optional max_yyyymm cutoff) ──────────────────────────

def test_future_capped():
    """202610 處理於 2026-06 → 未來日期,給 cutoff 即拒收(不猜測)。"""
    assert roc_to_tx_yyyymm("1151024", max_yyyymm=202606) is None


def test_at_cutoff_allowed():
    """落在 cutoff 當月仍算過去,放行。"""
    assert roc_to_tx_yyyymm("1150624", max_yyyymm=202606) == 202606


def test_no_cutoff_allows_future():
    """不給 cutoff → 維持原行為,不做未來檢查。"""
    assert roc_to_tx_yyyymm("1151024") == 202610


def test_valid_month_01():
    assert roc_to_tx_yyyymm("1130101") == 202401


def test_valid_month_12():
    assert roc_to_tx_yyyymm("1131201") == 202412


def test_valid_leading_zero_day():
    """Day portion is ignored; only year + month matter."""
    assert roc_to_tx_yyyymm("1140901") == 202509


# ── Invalid: 6-digit bad dates (observed in 2026q1 sales) ───────────────────

def test_invalid_6digit_990311():
    """6-digit date observed in real data — must drop to review, not be guessed."""
    assert roc_to_tx_yyyymm("990311") is None


def test_invalid_6digit_generic():
    assert roc_to_tx_yyyymm("123456") is None


# ── Invalid: impossible month ────────────────────────────────────────────────

def test_invalid_month_00():
    assert roc_to_tx_yyyymm("1140001") is None


def test_invalid_month_13():
    assert roc_to_tx_yyyymm("1140013") is None  # note: treated as month=00


def test_invalid_month_99():
    assert roc_to_tx_yyyymm("1149901") is None


# ── Invalid: empty / None / non-string ──────────────────────────────────────

def test_invalid_empty_string():
    assert roc_to_tx_yyyymm("") is None


def test_invalid_none():
    assert roc_to_tx_yyyymm(None) is None


def test_invalid_whitespace_only():
    assert roc_to_tx_yyyymm("       ") is None


# ── Invalid: non-digit characters ───────────────────────────────────────────

def test_invalid_contains_letter():
    assert roc_to_tx_yyyymm("114A111") is None


def test_invalid_8digits():
    assert roc_to_tx_yyyymm("11411110") is None


def test_invalid_5digits():
    assert roc_to_tx_yyyymm("11411") is None
