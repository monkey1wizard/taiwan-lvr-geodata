#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility entry point; the code lives in lvr_pipeline.addresses."""
from __future__ import annotations

from .addresses.identity import building_key
from .addresses.parse import _CITY_RE, _dedup_prefix, arab_to_cjk, is_garbled, norm, parse

__all__ = ["_CITY_RE", "_dedup_prefix", "arab_to_cjk", "building_key", "is_garbled", "norm",
           "parse"]
