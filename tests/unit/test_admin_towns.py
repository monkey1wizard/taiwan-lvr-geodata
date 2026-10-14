"""R07-6b: the county to town table, the old-county mapping and the checks that use them."""
import hashlib
import json
from pathlib import Path

import pytest

from lvr_pipeline.addresses import admin
from lvr_pipeline.addresses.admin import build_admin_towns, load_admin_towns, resolve_old_county, town_in_county
from lvr_pipeline.addresses.identity import building_key
from lvr_pipeline.pipeline import run_full
from lvr_pipeline.transactions.normalize import normalize
from tests.integration.test_p1_conversion import CODE, ingest, make_source, no_char_fixes, rules

FILES = {
    "area_1984.csv": (
        "name,dgbas_id\n"
        "臺北縣板橋市,1000101\n臺北縣樹林鎮,1000107\n臺北縣樹林市,1000107\n臺北縣烏來鄉,1000129\n"
        "臺北縣金山鄉,1000130\n"
        "高雄縣三民鄉,1001227\n高雄縣那瑪夏鄉,1001227\n高雄縣鳳山市,1001201\n"
        "臺北縣板橋市文化里,1000101-001\n"
    ),
    "area_2010.csv": "name,dgbas_id\n板橋區,6500100\n樹林區,6500700\n烏來區,6500900\n三民區,6400100\n鳳山區,6400200\n平鎮區,6800900\n",
    "area_2014.csv": "name,dgbas_id\n北區,6600100\n",
    "area_2015.csv": "name,dgbas_id\n新竹市北區,1001801\n桃園市平鎮,6800900\n",
    "area_custom.csv": "name,dgbas_id\n臺北市木柵區,6300800\n",
}


def synthetic(tmp_path, files=None):
    files = files or FILES
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    descriptor = {
        "commit": "a" * 40,
        "administrative_files": [
            {"path": name, "sha256": hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()} for name in sorted(files)
        ],
    }
    return descriptor


def test_union_of_tables_and_county_codes(tmp_path):
    document = build_admin_towns(tmp_path, synthetic(tmp_path))
    assert document["counties"]["65"] == ["板橋區", "樹林區", "烏來區"]
    assert document["counties"]["66"] == ["北區"]
    assert document["counties"]["10018"] == ["北區"]  # prefixed row of area_2015
    assert document["counties"]["63"] == ["木柵區"]  # area_custom counts in the union
    # A shortened spelling (桃園市平鎮) next to the full 平鎮區 is not a town.
    assert document["counties"]["68"] == ["平鎮區"]
    assert document["source_commit"] == "a" * 40
    assert [f["path"] for f in document["source_files"]] == sorted(FILES)


def test_suffix_only_difference_maps_and_new_town_must_exist(tmp_path):
    document = build_admin_towns(tmp_path, synthetic(tmp_path))
    towns = document["old_counties"]["臺北縣"]["towns"]
    assert document["old_counties"]["臺北縣"]["county"] == "新北市"
    assert towns == {"板橋市": "板橋區", "樹林市": "樹林區", "樹林鎮": "樹林區", "烏來鄉": "烏來區"}
    # 金山區 is not in the synthetic new-county table, so 金山鄉 is listed as unmapped.
    assert document["unmapped"] == [{"county": "臺北縣", "town": "金山鄉"}]
    assert document["old_counties"]["高雄縣"]["towns"] == {"鳳山市": "鳳山區"}


def test_renamed_town_code_is_excluded(tmp_path):
    document = build_admin_towns(tmp_path, synthetic(tmp_path))
    assert document["renamed_excluded"] == [{"county": "高雄縣", "dgbas_id": "1001227", "names": ["三民鄉", "那瑪夏鄉"]}]
    assert "三民鄉" not in document["old_counties"]["高雄縣"]["towns"]
    assert all(u["town"] != "三民鄉" for u in document["unmapped"])


def test_hash_mismatch_raises(tmp_path):
    descriptor = synthetic(tmp_path)
    (tmp_path / "area_2014.csv").write_text("name,dgbas_id\n北區,6600100\n南區,6600200\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        build_admin_towns(tmp_path, descriptor)


def test_committed_table():
    document = load_admin_towns()["document"]
    assert document["old_counties"]["臺北縣"]["towns"]["板橋市"] == "板橋區"
    assert document["old_counties"]["臺北縣"]["towns"]["樹林鎮"] == document["old_counties"]["臺北縣"]["towns"]["樹林市"]
    assert [e["dgbas_id"] for e in document["renamed_excluded"]] == ["1001227"]
    assert "三民鄉" not in document["old_counties"]["高雄縣"]["towns"]
    assert resolve_old_county("高雄縣那瑪夏鄉民權村1號") is None
    assert resolve_old_county("臺北縣板橋市文化路") == "新北市板橋區文化路"
    assert resolve_old_county("臺北市大安區忠孝東路") == "臺北市大安區忠孝東路"
    assert town_in_county("66", "北區") and not town_in_county("63", "北區")
    assert document["source_commit"] == json.loads(Path("config/sources/address_source.json").read_text(encoding="utf-8"))["commit"]


def test_different_admin_table_prevents_snapshot_reuse(tmp_path, monkeypatch):
    raw, manifest = make_source(tmp_path)
    work = tmp_path / "work"
    ingested = ingest(raw, manifest, ["115q1"], work, batch_rows=2, code_commit=CODE)
    first = normalize(ingested, work, cutoff=202610, rules_path=rules(tmp_path), char_fixes=no_char_fixes(), batch_rows=2, code_commit=CODE)
    again = normalize(ingested, work, cutoff=202610, rules_path=rules(tmp_path), char_fixes=no_char_fixes(), batch_rows=2, code_commit=CODE)
    assert again == first  # same table: the snapshot is reused
    monkeypatch.setattr(admin, "admin_towns_sha256", lambda: "0" * 64)
    other = normalize(ingested, work, cutoff=202610, rules_path=rules(tmp_path), char_fixes=no_char_fixes(), batch_rows=2, code_commit=CODE)
    assert other != first
    bindings = lambda p: json.loads((p / "manifest.json").read_text(encoding="utf-8"))["bindings"]
    assert bindings(other)["config_sha256"] != bindings(first)["config_sha256"]


def test_run_full_refuses_a_stale_town_table_before_writing(tmp_path):
    manifest = tmp_path / "raw_manifest.json"
    manifest.write_text(json.dumps({"rebuild_epoch": admin_epoch()}), encoding="utf-8")
    descriptor = json.loads(Path("config/sources/address_source.json").read_text(encoding="utf-8"))
    descriptor["commit"] = "b" * 40
    address = tmp_path / "address_source.json"
    address.write_text(json.dumps(descriptor), encoding="utf-8")
    runs = tmp_path / "runs"
    result = run_full(run_id="r1", cutoff=202610, address_dir=tmp_path, runs_root=runs, raw_dir=tmp_path,
                      manifest_path=manifest, address_source=address)
    assert result["completed"] is False
    assert [f["code"] for f in result["failures"]] == ["admin_towns_stale"]
    assert not runs.exists()
    descriptor = json.loads(Path("config/sources/address_source.json").read_text(encoding="utf-8"))
    descriptor["administrative_files"][0]["sha256"] = "c" * 64
    address.write_text(json.dumps(descriptor), encoding="utf-8")
    result = run_full(run_id="r2", cutoff=202610, address_dir=tmp_path, runs_root=runs, raw_dir=tmp_path,
                      manifest_path=manifest, address_source=address)
    assert [f["code"] for f in result["failures"]] == ["admin_towns_stale"]


def admin_epoch():
    from lvr_pipeline.storage.runs import REBUILD_EPOCH
    return REBUILD_EPOCH
