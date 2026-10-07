# -*- coding: utf-8 -*-
"""Lock the retained legacy entry points before the R-03 restructuring.

All addresses below are synthetic. Nothing here reads real data.
"""
import importlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Interfaces R-02 (docs/records/R02盤點.md, 程式去向) marks 保留相容, plus the contracts
# re-exports required by plan 5.7 and the tx_date function named in AGENTS.md.
RETAINED_SYMBOLS = {
    "lvr_pipeline.address": ["_CITY_RE", "_dedup_prefix", "arab_to_cjk", "building_key", "building_key_v2",
                             "is_garbled", "norm", "parse"],
    "lvr_pipeline.contracts": ["SCHEMAS", "SCHEMA_VERSION", "component_id", "observation_id", "schema_validator",
                              "validate_relations", "validate_rows"],
    "lvr_pipeline.tx_date": ["validated_roc_to_tx_yyyymm"],
}

SUBCOMMANDS = {
    # lvr_pipeline.cli
    "ingest", "normalize", "export-converted", "verify-converted",
    # registered by lvr_pipeline.cli
    "pin-address-source", "build-offline-index", "build-address-pool", "resolve-offline",
    "verify-offline-state", "prepare-tgos", "repair-tgos-exchange", "set-tgos-status", "import-tgos",
    "revoke-alias", "verify-tgos-state", "package-output", "verify-output", "backfill-output",
    "export-address-patch", "verify-address-patch", "fetch-output", "publish-output",
    "commit-release-pointer",
}


@pytest.mark.parametrize("module_name", sorted(RETAINED_SYMBOLS))
def test_retained_symbols_exist(module_name):
    module = importlib.import_module(module_name)
    missing = []
    for name in RETAINED_SYMBOLS[module_name]:
        if hasattr(module, name):
            continue
        try:  # `from package import submodule` resolves a submodule on demand
            importlib.import_module(f"{module_name}.{name}")
        except ImportError:
            missing.append(name)
    assert not missing, f"{module_name} lost {missing}"


def test_cli_subcommand_set_is_exactly_23():
    assert len(SUBCOMMANDS) == 23
    result = subprocess.run([sys.executable, "-m", "lvr_pipeline", "--help"], cwd=ROOT, capture_output=True,
                            text=True, encoding="utf-8", check=True)
    match = re.search(r"\{([a-z0-9,\-]+)\}", result.stdout)
    assert match, result.stdout
    assert set(match.group(1).split(",")) == SUBCOMMANDS


@pytest.mark.parametrize("filename", ["0_parse_raw.py", "1_normalize.py"])
def test_numbered_scripts_load_by_path(filename):
    path = ROOT / "lvr_pipeline" / filename
    spec = importlib.util.spec_from_file_location("_legacy_" + path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def _v2(county, town, road, door):
    return "v2:" + json.dumps({"county": county, "door": door, "locality_road": road, "town": town},
                              ensure_ascii=False, sort_keys=True, separators=(",", ":"))


BUILDING_KEY_V2_CASES = [
    ("臺北市中正區重慶南路一段122號", _v2("63", "中正區", "重慶南路一段", "122")),
    ("台北市中正區重慶南路一段122號", _v2("63", "中正區", "重慶南路一段", "122")),
    ("新北市板橋區文化路二段100號5樓", _v2("65", "板橋區", "文化路二段", "100")),
    ("新北市板橋區文化路二段100號十五樓", _v2("65", "板橋區", "文化路二段", "100")),
    ("臺中市西屯區臺灣大道三段99之1號", _v2("66", "西屯區", "臺灣大道三段", "99之1")),
    ("臺中市西屯區臺灣大道三段99號之1", _v2("66", "西屯區", "臺灣大道三段", "99之1")),
    ("高雄市前鎮區中山二路5號", _v2("64", "前鎮區", "中山二路", "5")),
    ("桃園市中壢區中正路100巷3弄2號", _v2("68", "中壢區", "中正路100巷3弄", "2")),
    ("嘉義縣民雄鄉雙福村12鄰10號", _v2("10010", "民雄鄉", "雙福村12鄰", "10")),
    ("宜蘭縣礁溪鄉大忠村五峰路1號", _v2("10002", "礁溪鄉", "五峰路", "1")),
    ("臺北市士林區天母二路13巷12弄9號", _v2("63", "士林區", "天母二路13巷12弄", "9")),
    ("臺北市大安區和平東路二段106號一樓", _v2("63", "大安區", "和平東路二段", "106")),
    ("新北市新莊區中正路2號3樓之4", _v2("65", "新莊區", "中正路", "2")),
    # rejected: no door number, suffix noise, ranges, unresolved characters, empty input
    ("高雄市前鎮區中山二路5號B1", None),
    ("南投縣埔里鎮", None),
    ("新竹縣竹北市中正西路10-2號3樓", None),
    ("臺南市東區崇學路1~3號", None),
    ("基隆市仁愛區愛四路", None),
    ("臺北市北投區公?路10號", None),
    ("", None),
]


def test_building_key_v2_table_has_enough_cases():
    assert len(BUILDING_KEY_V2_CASES) >= 15


@pytest.mark.parametrize("address,expected", BUILDING_KEY_V2_CASES)
def test_building_key_v2_both_entry_points_agree(address, expected):
    from lvr_pipeline.address import building_key_v2 as via_address
    from lvr_pipeline.addresses.identity import building_key_v2 as via_v2
    assert via_address(address) == expected
    assert via_v2(address) == expected


def test_all_five_json_schemas_load():
    from lvr_pipeline.contracts import schema_validator
    expected_files = {"address-component.schema.json", "converted-parquet.json", "diagnostic.schema.json",
                      "exclusion.schema.json", "observation.schema.json"}
    schema_dir = _schema_dir()
    assert {p.name for p in schema_dir.glob("*.json")} == expected_files
    for name in ("observation", "address-component", "exclusion", "diagnostic"):
        assert schema_validator(name) is not None
    # converted-parquet is an Arrow table contract, not a Draft 2020-12 schema,
    # so schema_validator does not accept it; it must still be valid JSON.
    document = json.loads((schema_dir / "converted-parquet.json").read_text(encoding="utf-8"))
    assert isinstance(document, dict) and document
    with pytest.raises(ValueError):
        schema_validator("converted-parquet")


def _schema_dir():
    from lvr_pipeline.contracts import SCHEMAS
    return Path(SCHEMAS)
