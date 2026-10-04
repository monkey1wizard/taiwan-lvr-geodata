#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""民國日期字串 → tx_yyyymm (int)。"""
from __future__ import annotations


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
