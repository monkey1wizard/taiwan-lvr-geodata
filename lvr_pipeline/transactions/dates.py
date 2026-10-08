#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""民國日期字串 → tx_yyyymm (int)。"""
from __future__ import annotations

from datetime import date
import re


def validated_roc_to_tx_yyyymm(value: str, *, run_cutoff_yyyymm: int) -> int | None:
    """Validate a full calendar date using a recorded run cutoff, not today."""
    if not isinstance(run_cutoff_yyyymm, int) or isinstance(run_cutoff_yyyymm, bool):
        raise ValueError("Expected a fixed YYYYMM cutoff")
    try:
        date(run_cutoff_yyyymm // 100, run_cutoff_yyyymm % 100, 1)
    except ValueError as exc:
        raise ValueError("Invalid YYYYMM cutoff") from exc
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{7}", value.strip()):
        return None
    value = value.strip()
    if int(value[:3]) < 1:
        return None
    try:
        parsed = date(int(value[:3]) + 1911, int(value[3:5]), int(value[5:]))
    except ValueError:
        return None
    month = parsed.year * 100 + parsed.month
    return month if month <= run_cutoff_yyyymm else None


def roc_to_tx_yyyymm(s, max_yyyymm: int | None = None) -> int | None:
    """民國年月日字串（7 碼 YYYmmDD）→ 西元 YYYYMM (int)；無效回 None。

    規則：
    - 必須恰好 7 碼且全為數字
    - 月份 01–12
    - 若給 max_yyyymm，結果 > max_yyyymm 視為未來日期回 None
    """
    if not s:
        return None
    s = str(s).strip()
    if len(s) != 7 or not s.isdigit():
        return None
    roc_year = int(s[:3])
    month = int(s[3:5])
    if month < 1 or month > 12:
        return None
    western = roc_year + 1911
    yyyymm = western * 100 + month
    if max_yyyymm is not None and yyyymm > max_yyyymm:
        return None
    return yyyymm
