# -*- coding: utf-8 -*-
"""Tests for lvr_pipeline.1_normalize — garbled correction, idempotency."""
import csv
import importlib.util
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

_spec = importlib.util.spec_from_file_location(
    "_1_normalize",
    os.path.join(os.path.dirname(__file__), "..", "..", "lvr_pipeline", "1_normalize.py"),
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]
normalize_meta = _mod.normalize_meta


def _write_garbled(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(content)


def _write_meta(path: str, rows: list[dict]) -> None:
    if not rows:
        return
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _read_meta(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def test_pua_corrected_after_normalize():
    """meta containing U+E001 → raw_address is canonical after normalize."""
    with tempfile.TemporaryDirectory() as tmp:
        garbled_p = os.path.join(tmp, "garbled.csv")
        _write_garbled(garbled_p, "garbled,correct,scope,kind,evidence,noted_date\n"
                                  "U+E001,槺,,char,test,2026-01-01\n")
        from lvr_pipeline.garbled import load_garbled
        char_map, token_patterns = load_garbled(garbled_p)

        pua = chr(0xE001)
        meta_p = os.path.join(tmp, "meta_sales.csv")
        _write_meta(meta_p, [
            {"id": "1", "raw_address": f"臺中市梧棲區{pua}榔七街10號"},
            {"id": "2", "raw_address": "臺北市中正區重慶南路一段10號"},
        ])

        changed = normalize_meta(meta_p, char_map, token_patterns)
        assert changed == 1

        rows = _read_meta(meta_p)
        assert "槺" in rows[0]["raw_address"]
        assert pua not in rows[0]["raw_address"]
        assert rows[1]["raw_address"] == "臺北市中正區重慶南路一段10號"


def test_idempotent_second_run_no_change():
    """Second run on already-corrected meta produces 0 changes."""
    with tempfile.TemporaryDirectory() as tmp:
        garbled_p = os.path.join(tmp, "garbled.csv")
        _write_garbled(garbled_p, "garbled,correct,scope,kind,evidence,noted_date\n"
                                  "U+E001,槺,,char,test,2026-01-01\n")
        from lvr_pipeline.garbled import load_garbled
        char_map, token_patterns = load_garbled(garbled_p)

        pua = chr(0xE001)
        meta_p = os.path.join(tmp, "meta_sales.csv")
        _write_meta(meta_p, [{"id": "1", "raw_address": f"臺中市梧棲區{pua}榔七街10號"}])

        normalize_meta(meta_p, char_map, token_patterns)
        changed2 = normalize_meta(meta_p, char_map, token_patterns)
        assert changed2 == 0


def test_missing_meta_file_returns_zero():
    from lvr_pipeline.garbled import load_garbled
    char_map, token_patterns = load_garbled("/nonexistent/garbled.csv")
    assert normalize_meta("/nonexistent/meta_sales.csv", char_map, token_patterns) == 0


def test_no_raw_address_column_skipped():
    """Meta without raw_address column: returns 0, file unchanged."""
    with tempfile.TemporaryDirectory() as tmp:
        from lvr_pipeline.garbled import load_garbled
        char_map, token_patterns = load_garbled("/nonexistent/garbled.csv")
        meta_p = os.path.join(tmp, "meta_sales.csv")
        _write_meta(meta_p, [{"id": "1", "other": "value"}])
        assert normalize_meta(meta_p, char_map, token_patterns) == 0


split_meta = _mod.split_meta


def test_split_partitions_by_garble():
    """is_garbled 切檔：clean 無 garble、garbled 全 garble、聯集 = 輸入、帶 id。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        meta = os.path.join(tmp, "meta_sales.csv")
        _write_meta(meta, [
            {"id": "c1", "raw_address": "臺北市北投區公館路1號"},
            {"id": "g1", "raw_address": "桃園市雙?路186號"},
            {"id": "g2", "raw_address": "臺中市梧棲區" + chr(0xE001) + "榔七街10號"},
        ])
        out = os.path.join(tmp, "offline_in")
        result = split_meta(meta, "sales", out_dir=out)

        assert result == {"clean": 1, "garbled": 2}
        with open(os.path.join(out, "sales_clean.csv"), encoding="utf-8-sig", newline="") as f:
            clean = list(csv.DictReader(f))
        with open(os.path.join(out, "sales_garbled.csv"), encoding="utf-8-sig", newline="") as f:
            garbled = list(csv.DictReader(f))
        assert [r["id"] for r in clean] == ["c1"]
        assert sorted(r["id"] for r in garbled) == ["g1", "g2"]
        assert all("?" not in r["raw_address"] and chr(0xE001) not in r["raw_address"] for r in clean)
        assert len(clean) + len(garbled) == 3


def test_split_overwrites_not_appends():
    """重跑覆寫，不殘留舊內容。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        meta = os.path.join(tmp, "meta_sales.csv")
        out = os.path.join(tmp, "offline_in")
        _write_meta(meta, [{"id": "a", "raw_address": "臺北市北投區公館路1號"}])
        split_meta(meta, "sales", out_dir=out)
        split_meta(meta, "sales", out_dir=out)  # rerun
        with open(os.path.join(out, "sales_clean.csv"), encoding="utf-8-sig", newline="") as f:
            assert len(list(csv.DictReader(f))) == 1
