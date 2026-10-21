"""TGOS adoption check: whether a response names the queried door (docs/地址規則.md, 採用比對的放寬)."""

from __future__ import annotations

import json
import math
import re
import time
from functools import lru_cache
from pathlib import Path

import duckdb

from ..addresses.admin import load_admin_towns, town_villages
from ..addresses.identity import _split_village, building_key
from ..results.reconcile import adopt_coordinates
from ..transactions.normalize import normalize_address

GROUP_TOLERANCE_M = 30.0
NEIGHBORHOOD_OUTSIDE_REASON = "tgos_neighborhood_outside_base_group"

_NOTATION = re.compile(r"^(?P<pre>[臨建特]?)(?P<n>[0-9]+)號之(?P<m>[0-9]+)$")
_VILLAGE_SUFFIX = ("村", "里")
_SECTION = re.compile(r"^(?P<road>.*?[路街道])(?P<section>[一二三四五六七八九十]+段)(?P<rest>.*)$")


def response_key(address: str | None) -> str | None:
    """The door key of a TGOS response address, for adoption and review (c41).

    ``address`` is the raw response text; it is normalised like a query address. When that has no
    key and ends with an ASCII digit, TGOS left out the final 號: the key is tried once more with
    "號" appended. The stored response address is not changed, and neither is ``building_key``.
    """
    if not address:
        return None
    text = normalize_address(address)
    key = building_key(text)
    if key is None and text[-1:].isascii() and text[-1:].isdigit():
        key = building_key(text + "號")
    return key


def _text(value):
    """Adoption comparisons read the bopomofo letter ㄧ (U+3127) as the numeral 一 (U+4E00) (c42)."""
    return value.replace("ㄧ", "一") if isinstance(value, str) else value


def _door_notation(door: str) -> str:
    """Rule 3: ``N號之M`` is read as ``N之M``; every other door is unchanged."""
    found = _NOTATION.match(door)
    return f"{found['pre']}{found['n']}之{found['m']}" if found else door


def _place_from_village(value: dict) -> str | None:
    """The place name of a key whose locality_road the address rules moved from the village (e09 rule 2).

    Such a key writes no village and no neighbourhood, its whole locality_road is what the address rules
    read as a village token right before the door, and that name is not a village of the town. A road
    that only ends in 村 or 里 (東崎路五段雪山一村) is not such a name. Any other key gives None.
    Adoption reads that name as the query's village text.
    """
    road = value["locality_road"]
    if value.get("village") is not None or value.get("neighborhood") is not None or road[-1:] not in _VILLAGE_SUFFIX:
        return None
    valid, village, neighborhood, _ = _split_village(road + value["door"] + "號")
    if not valid or village != road or neighborhood is not None:
        return None
    return None if road in town_villages(value["county"], value["town"]) else road


def _without_section(road: str) -> str:
    """Rule 9: the road with its section (一段, 二段, ...) removed; a road without a section is unchanged."""
    found = _SECTION.match(road)
    return found["road"] + found["rest"] if found else road


@lru_cache(maxsize=1)
def _village_index() -> dict:
    """(county, town) -> village name -> {code: row}, for current and old-county rows alike.

    An old-county row belongs to the current town its old town name maps to.
    """
    data = load_admin_towns()
    document = data["document"]
    index: dict = {}
    for row in document["villages"]:
        county, town_code = row["county"], row["town_code"]
        names = document["town_codes"].get(county, {}).get(town_code, ())
        old_county = row.get("old_county")
        towns = set(names)
        if old_county:
            mapping = document["old_counties"].get(old_county, {}).get("towns", {})
            towns |= {mapping[name] for name in names if name in mapping}
        for town in towns:
            index.setdefault((county, town), {}).setdefault(row["name"], {})[row["code"]] = row
    return index


def _old_town_maps_to(row: dict, town: str) -> bool:
    document = load_admin_towns()["document"]
    names = document["town_codes"].get(row["county"], {}).get(row["town_code"], ())
    mapping = document["old_counties"].get(row.get("old_county"), {}).get("towns", {})
    return any(mapping.get(name) == town for name in names)


def _same_village(county: str, town: str, query_village: str, response_village: str) -> bool:
    """Rule 1 and the county-reform extension; anything undecided is not the same village."""
    query_village, response_village = _text(query_village), _text(response_village)
    if query_village == response_village:
        return True
    names = _village_index().get((county, town), {})
    codes = [names.get(query_village, {}), names.get(response_village, {})]
    if any(len(rows) != 1 for rows in codes):
        return False
    (code_a, row_a), (code_b, row_b) = (next(iter(rows.items())) for rows in codes)
    if code_a == code_b:
        return True
    old, current = (row_a, row_b) if row_a.get("old_county") else (row_b, row_a)
    if not old.get("old_county") or current.get("old_county") or not _old_town_maps_to(old, town):
        return False
    document = load_admin_towns()["document"]
    if town not in document["town_codes"].get(current["county"], {}).get(current["town_code"], ()):
        return False
    return (
        old["code"].rsplit("-", 1)[-1] == current["code"].rsplit("-", 1)[-1]
        and old["name"][-1:] in _VILLAGE_SUFFIX
        and current["name"][-1:] in _VILLAGE_SUFFIX
        and old["name"][:-1] == current["name"][:-1]
    )


def _door_and_village(query: dict, response: dict) -> bool:
    """Same county, town, road and door (rule 3); a written village matches as text or by code (rule 1)."""
    for field in ("county", "town"):
        if query[field] != response[field]:
            return False
    if _text(query["locality_road"]) != _text(response["locality_road"]):
        return False
    if _door_notation(query["door"]) != _door_notation(response["door"]):
        return False
    village = query.get("village")
    return village is None or _same_village(
        query["county"], query["town"], village, response.get("village") or ""
    )


def same_door_for_adoption(query_key: str | None, response_key: str | None) -> bool:
    """A response names the queried door under the adoption rules; the keys themselves are not changed.

    Same county, town, road and door (door notation read by rule 3). A written village must match
    as text or by village code (rule 1); a written neighbourhood must match exactly. A layer the
    query does not write may be present in the response.
    """
    if not query_key or not response_key:
        return False
    query, response = json.loads(query_key[3:]), json.loads(response_key[3:])
    if _place_from_village(query) is not None or not _door_and_village(query, response):
        return False
    neighborhood = query.get("neighborhood")
    return neighborhood is None or neighborhood == response.get("neighborhood")


DOOR = "door"
NEIGHBORHOOD = "neighborhood"
VILLAGE_DOOR = "village_door"
VILLAGE_AS_PLACE = "village_as_place"
ROAD_SECTION = "road_section"


def adoption_route(query_key: str | None, response_key: str | None) -> str | None:
    """Which rule family names the queried door: DOOR (rules 1 and 3), NEIGHBORHOOD (rule 2),
    VILLAGE_DOOR (rule 4), VILLAGE_AS_PLACE (rule 5), ROAD_SECTION (rule 9) or None. The routes after DOOR also
    need the base group check."""
    if not query_key or not response_key:
        return None
    if same_door_for_adoption(query_key, response_key):
        return DOOR
    query, response = json.loads(query_key[3:]), json.loads(response_key[3:])
    place = _place_from_village(query)
    if place is not None:
        return _place_route(place, query, response)
    neighborhood = query.get("neighborhood")
    if neighborhood is not None and response.get("neighborhood") != neighborhood and _door_and_village(query, response):
        return NEIGHBORHOOD
    if (
        query["locality_road"] == ""
        and query.get("village") is not None
        and response["locality_road"] != ""
        and (query["county"], query["town"]) == (response["county"], response["town"])
        and _door_notation(query["door"]) == _door_notation(response["door"])
        and _same_village(query["county"], query["town"], query["village"], response.get("village") or "")
        and (neighborhood is None or neighborhood == response.get("neighborhood"))
    ):
        return VILLAGE_DOOR
    if (
        query["locality_road"] == ""
        and query.get("village") is not None
        and _text(response["locality_road"]) == _text(query["village"])
        and (query["county"], query["town"]) == (response["county"], response["town"])
        and _door_notation(query["door"]) == _door_notation(response["door"])
        and (neighborhood is None or neighborhood == response.get("neighborhood"))
    ):
        return VILLAGE_AS_PLACE
    if (
        not _SECTION.match(query["locality_road"])
        and _SECTION.match(response["locality_road"])
        and _text(_without_section(response["locality_road"])) == _text(query["locality_road"])
        and (query["county"], query["town"]) == (response["county"], response["town"])
        and _door_notation(query["door"]) == _door_notation(response["door"])
        and (query.get("village") is None or _same_village(query["county"], query["town"], query["village"], response.get("village") or ""))
        and (neighborhood is None or neighborhood == response.get("neighborhood"))
    ):
        return ROAD_SECTION
    return None


def _place_route(place: str, query: dict, response: dict) -> str | None:
    """Rules 4 and 5 for a query whose place name came from the village text (e09 rule 2).

    The response must name the same county, town and door. It may write the name as its village
    (rule 4) or as its locality_road (rule 5); both routes need the base group check.
    """
    if (query["county"], query["town"]) != (response["county"], response["town"]):
        return None
    if _door_notation(query["door"]) != _door_notation(response["door"]):
        return None
    if _text(response.get("village")) == _text(place):
        return VILLAGE_DOOR
    if _text(response["locality_road"]) == _text(place):
        return VILLAGE_AS_PLACE
    return None


def base_town(query_key: str) -> tuple[str, str]:
    value = json.loads(query_key[3:])
    return value["county"], value["town"]


class GroupVerdict:
    """What the base says about one group: not present, one location, or several."""

    __slots__ = ("present", "single", "neighborhoods")

    def __init__(self, present: bool, single: bool, neighborhoods: frozenset):
        self.present, self.single, self.neighborhoods = present, single, neighborhoods


def _verdict(units: dict) -> GroupVerdict:
    """``units`` maps a unit (neighbourhood, or road and neighbourhood) to its set of (lng, lat) points.

    A unit with several points is represented by its medoid; the unit points then decide within 30 m.
    """
    if not units:
        return GroupVerdict(False, False, frozenset())
    reps = set()
    for points in units.values():
        reps.add(next(iter(points)) if len(points) == 1 else adopt_coordinates(points, math.inf)["representative"])
    single = len(reps) == 1 or adopt_coordinates(reps, GROUP_TOLERANCE_M)["representative"] is not None
    return GroupVerdict(True, single, frozenset(units))


class BaseGroups:
    """The valid offline-index rows of the towns one import needs, grouped for rules 2, 4, 5 and 9.

    ``load`` reads ``offline_rows.parquet`` once for the towns not yet loaded; ``seconds`` is the
    total read time. Only rows with validity 'valid' and a building_key count.
    """

    def __init__(self, index: Path):
        self.path = Path(index) / "offline_rows.parquet"
        self.loaded: set = set()
        self.seconds = 0.0
        self._neighborhood: dict = {}
        self._village_door: dict = {}
        self._place_door: dict = {}
        self._section_door: dict = {}

    def load(self, towns) -> None:
        needed = sorted(set(towns) - self.loaded)
        if not needed:
            return
        started = time.perf_counter()
        with duckdb.connect() as db:
            db.execute("CREATE TEMP TABLE need(county VARCHAR, town VARCHAR)")
            db.executemany("INSERT INTO need VALUES (?, ?)", needed)
            found = db.execute(
                "SELECT building_key, lng, lat FROM read_parquet(?) "
                "WHERE validity = 'valid' AND building_key IS NOT NULL AND lng IS NOT NULL AND lat IS NOT NULL "
                "AND county_code IN (SELECT county FROM need) "
                "AND (json_extract_string(substr(building_key, 4), '$.county'), "
                "json_extract_string(substr(building_key, 4), '$.town')) IN (SELECT county, town FROM need)",
                [str(self.path)],
            ).fetchall()
        for key, lng, lat in found:
            value = json.loads(key[3:])
            door = _door_notation(value["door"])
            road, village, neighborhood = _text(value["locality_road"]), _text(value.get("village")), value.get("neighborhood")
            town = (value["county"], value["town"])
            point = (lng, lat)
            self._neighborhood.setdefault((*town, road, door, village), {}).setdefault(
                neighborhood, set()
            ).add(point)
            if road != "":
                self._place_door.setdefault((*town, road, door), {}).setdefault(
                    (village, neighborhood), set()
                ).add(point)
            self._section_door.setdefault((*town, _without_section(road), door), {}).setdefault(
                (road, village, neighborhood), set()
            ).add(point)
            if village is not None:
                self._village_door.setdefault((*town, village, door), {}).setdefault(
                    (road, neighborhood), set()
                ).add(point)
        self.loaded |= set(needed)
        self.seconds += time.perf_counter() - started

    def neighborhood_group(self, query_key: str) -> GroupVerdict:
        """Rule 2: the base group of the query's signature and village."""
        value = json.loads(query_key[3:])
        group = (value["county"], value["town"], _text(value["locality_road"]), _door_notation(value["door"]), _text(value.get("village")))
        return _verdict(self._neighborhood.get(group, {}))

    def _place_group(self, value: dict, place: str) -> GroupVerdict:
        """The base rows that write the place name as their village or as their locality_road, same door."""
        group = (value["county"], value["town"], _text(place), _door_notation(value["door"]))
        units = {("village", *unit): points for unit, points in self._village_door.get(group, {}).items()}
        units.update({("place", *unit): points for unit, points in self._place_door.get(group, {}).items()})
        return _verdict(units)

    def village_door_group(self, query_key: str) -> GroupVerdict:
        """Rule 4: the base group of the query's village and door in its county and town, any road."""
        value = json.loads(query_key[3:])
        place = _place_from_village(value)
        if place is not None:
            return self._place_group(value, place)
        group = (value["county"], value["town"], _text(value["village"]), _door_notation(value["door"]))
        return _verdict(self._village_door.get(group, {}))

    def place_door_group(self, query_key: str) -> GroupVerdict:
        """Rule 5: the base group whose locality_road is the query's village text, same door, any village."""
        value = json.loads(query_key[3:])
        place = _place_from_village(value)
        if place is not None:
            return self._place_group(value, place)
        group = (value["county"], value["town"], _text(value["village"]), _door_notation(value["door"]))
        return _verdict(self._place_door.get(group, {}))

    def section_group(self, query_key: str, response_key: str) -> GroupVerdict:
        """Rule 9: the base rows of the query's town and door whose road without its section is the query road,
        any section, village or neighbourhood; each unit is (road, village, neighbourhood). The group counts as
        not present when no row writes the response's road."""
        value = json.loads(query_key[3:])
        group = (value["county"], value["town"], _text(value["locality_road"]), _door_notation(value["door"]))
        units = self._section_door.get(group, {})
        if _text(json.loads(response_key[3:])["locality_road"]) not in {unit[0] for unit in units}:
            return _verdict({})
        return _verdict(units)
