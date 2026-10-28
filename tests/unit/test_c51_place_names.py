"""c51 place names split into a village and a road (docs/地址規則.md, 地名表)."""

import json

import pytest

from lvr_pipeline.addresses.identity import _PLACE_NAMES, building_key


def fields(address: str) -> dict:
    return json.loads(building_key(address)[3:])


@pytest.mark.parametrize(
    ("address", "road", "door"),
    [
        ("新北市萬里區萬里加投132之6號", "萬里加投", "132之6"),
        ("新北市淡水區八里堆5之7號", "八里堆", "5之7"),
        ("臺南市佳里區佳里興238號", "佳里興", "238"),
        ("屏東縣屏東市建國新村35巷3號", "建國新村35巷", "3"),
        ("金門縣金城鎮富康一村6巷15號", "富康一村6巷", "15"),
        ("南投縣埔里鎮南村一巷1之13號", "南村一巷", "1之13"),
        ("臺東縣太麻里鄉利里武街12巷13號", "利里武街12巷", "13"),
    ],
)
def test_listed_place_name_is_the_road(address, road, door):
    value = fields(address)
    assert value["locality_road"] == road and value["door"] == door
    assert "village" not in value


def test_place_name_and_base_spelling_give_one_door():
    # The base writes the village and neighbourhood; the door signature is the same.
    pool = fields("新北市萬里區萬里加投132之6號")
    base = fields("新北市萬里區大鵬里14鄰萬里加投132之6號")
    assert {k: pool[k] for k in ("county", "town", "locality_road", "door")} == \
        {k: base[k] for k in ("county", "town", "locality_road", "door")}


@pytest.mark.parametrize(
    ("address", "village"),
    [
        # Not in the table: a village name that lost its first character, an old name, a missing base door.
        ("臺南市永康區康里大橋一街49之5號", "康里"),
        ("桃園市大園區果林村崁下207號", "果林村"),
        ("南投縣中寮鄉永嘉新村23巷1弄1號", "永嘉新村"),
        # A listed name in another town is not a place name there.
        ("臺南市新營區佳里興238號", "佳里"),
    ],
)
def test_other_village_tokens_are_kept(address, village):
    assert fields(address).get("village") == village


def test_a_neighbourhood_keeps_the_village():
    assert fields("新北市萬里區萬里3鄰加投132號").get("village") == "萬里"


def test_a_current_village_is_never_a_place_name():
    from lvr_pipeline.addresses.admin import town_villages

    for (county, town), names in _PLACE_NAMES.items():
        villages = town_villages(county, town)
        assert villages, (county, town)
        for name in names:
            assert not any(name == v for v in villages), (town, name)
