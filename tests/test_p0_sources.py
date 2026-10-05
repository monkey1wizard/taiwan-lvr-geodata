from pathlib import Path
import shutil
import zipfile

import pytest

from lvr_pipeline.sources import describe_zip, verify_raw

FIXTURES=Path(__file__).parent / "fixtures/p0"


def test_synthetic_manifest_and_english_row_not_header():
    path=FIXTURES / "115q1_lvr_landcsv.zip"
    entry=describe_zip(path)
    verify_raw(path,entry)
    assert entry["categories"] == ["presale","rent","sales"]
    assert entry["members"][0]["header"][-1] == "synthetic_unknown"
    assert entry["acquired_at"] is None and entry["download_uri"] is None


def test_known_empty_requires_exact_members(tmp_path):
    assert describe_zip(FIXTURES/"101q1_lvr_landcsv.zip")["known_empty"]
    bad=tmp_path/"102q1_lvr_landcsv.zip"
    shutil.copyfile(FIXTURES/"101q1_lvr_landcsv.zip",bad)
    with pytest.raises(ValueError,match="known-empty"):
        describe_zip(bad)


def test_missing_corrupt_and_hash_mismatch(tmp_path):
    source=FIXTURES/"115q1_lvr_landcsv.zip"
    entry=describe_zip(source)
    with pytest.raises(FileNotFoundError):
        verify_raw(tmp_path/"missing.zip",entry)
    bad=tmp_path/"bad.zip"
    bad.write_bytes(b"not zip")
    with pytest.raises(zipfile.BadZipFile):
        describe_zip(bad)
    bad.write_bytes(source.read_bytes()+b"changed")
    with pytest.raises(ValueError,match="hash"):
        verify_raw(bad,entry)


def test_tracked_actual_inventory():
    import json
    root=Path(__file__).resolve().parents[1]/"data/sources"
    raw=json.loads((root/"raw_manifest.json").read_text(encoding="utf-8"))
    address=json.loads((root/"address_source.json").read_text(encoding="utf-8"))
    assert raw["raw_count"] == len(raw["inputs"]) == 58
    assert len({r["batch"] for r in raw["inputs"]}) == 58
    assert [r["batch"] for r in raw["inputs"] if r["known_empty"]] == ["101q1"]
    assert address["road_count"] == len(address["roads"]) == 27175
    assert address["commit"] == "752c87d36a8e52d9b71680115c1c19d1a6d3e4ec"
