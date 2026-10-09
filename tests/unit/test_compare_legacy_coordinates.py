# -*- coding: utf-8 -*-
"""R05-5 classification logic of scripts/compare_legacy_coordinates.py (synthetic inputs)."""
import importlib.util
import os

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "compare_legacy_coordinates.py")
_SPEC = importlib.util.spec_from_file_location("compare_legacy_coordinates", _PATH)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

KEY = "v2:{}"
OLD = (121.5, 25.0)


def new(status, lng=121.5, lat=25.0, basis=None):
    return {"status": status, "lng": lng, "lat": lat, "basis": basis}


def test_class1_within_tolerance():
    cls, sub, dist = mod.classify(KEY, OLD, new("located", 121.5001), 30.0)
    assert (cls, sub) == (mod.C1, None) and 0 < dist <= 30.0


def test_class2_beyond_tolerance():
    cls, sub, dist = mod.classify(KEY, OLD, new("located", 121.51), 30.0)
    assert (cls, sub) == (mod.C2, None) and dist > 30.0


def test_class3_legacy_only_subdivided():
    assert mod.classify(KEY, OLD, new("unmatched", None, None), 30.0)[:2] == (mod.C3, mod.SUB_MISS)
    assert mod.classify(KEY, OLD, new("conflict", basis="exceeds_tolerance"), 30.0)[:2] == (mod.C3, mod.SUB_EXCEEDS)
    assert mod.classify(KEY, OLD, new("conflict", basis="door_cross_check"), 30.0)[:2] == (mod.C3, mod.SUB_CROSS)
    assert mod.classify(KEY, OLD, None, 30.0)[:2] == (mod.C3, mod.SUB_NOT_IN_POOL)


def test_class5_no_key():
    assert mod.classify(None, OLD, None, 30.0) == (mod.C5, None, None)
    assert mod.classify(mod.building_key_v2("1至3號"), OLD, None, 30.0)[0] == mod.C5


def test_class4_is_not_a_record_class():
    # A new-only located key has no legacy record; the script reports it per key (summary only).
    assert mod.C1 != mod.C2 != mod.C3 != mod.C5
