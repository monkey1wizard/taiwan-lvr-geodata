"""Retained compatibility re-export; the implementation lives in transactions/dates.py."""
from __future__ import annotations

from .transactions.dates import validated_roc_to_tx_yyyymm

__all__ = ["validated_roc_to_tx_yyyymm"]
