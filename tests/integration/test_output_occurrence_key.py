"""Doors are joined to address results by the occurrence key, not by the key of the component.

The address pool may remap the key of a component (here the old district 木柵區 becomes 文山區); the
address results and the owner decisions are keyed by the occurrence key.
"""

import sys
from pathlib import Path

import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lvr_pipeline.output.rows import iter_door_rows  # noqa: E402
from lvr_pipeline.storage.parquet import rows  # noqa: E402
from test_final_state import (  # noqa: E402
    csv_rows, decisions_dir, final_of, output_of, review_of, second, sources_of,
)
from test_second_review import _key, build_base, by_address  # noqa: E402

OLD_LOCATED = "臺北市木柵區測試路11號"  # the source locates 臺北市文山區測試路11號
OLD_MISSING = "臺北市木柵區測試路12號"  # no source row: unmatched, an owner decision locates it
CURRENT_LOCATED = "臺北市文山區測試路11號"
CURRENT_MISSING = "臺北市文山區測試路12號"
MANUAL_ADDRESS = "臺北市文山區測試路12號(人工判定)"
EXTRA = [{"address": OLD_LOCATED, "date": "1140420"}, {"address": OLD_MISSING, "date": "1140421"},
         {"address": OLD_MISSING, "date": "1140422"}]


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return build_base(tmp_path_factory.mktemp("occurrence_key"), EXTRA)


def remapped(world):
    """component_id -> occurrence, for the components whose occurrence key differs from the component key."""
    components = {}
    for item in world["converted"].glob("**/*.parquet"):
        if "address_component" in item.as_posix():
            components.update({r["component_id"]: r for r in rows(item)})
    occurrences = rows(world["state"] / "address_occurrences.parquet")
    return {o["component_id"]: (components[o["component_id"]], o) for o in occurrences
            if o["building_key"] != components[o["component_id"]]["building_key"]}


def test_fixture_remaps_the_key_of_the_old_district(world):
    found = remapped(world)
    assert {c["normalized_address"] for c, _ in found.values()} == {OLD_LOCATED, OLD_MISSING}
    assert {o["building_key"] for _, o in found.values()} == {_key(CURRENT_LOCATED), _key(CURRENT_MISSING)}
    assert all(o["original_key"] == c["building_key"] != o["building_key"] for c, o in found.values())


def test_a_door_whose_key_was_remapped_is_in_the_output(world):
    out = output_of(world, "remapped")
    located = [r for r in iter_door_rows(world["converted"], world["state"], spill_dir=world["tmp"] / "spill")
               if r["transaction_addresses"] == OLD_LOCATED]
    assert len(located) == 3  # sales, presale and rent
    assert {(r["longitude"], r["latitude"]) for r in located} == {(121.55, 25.02)}
    month = "csv/2025/taiwanlvr_202504_{}.csv"
    written = [r for c in ("sales", "presale", "rent") for r in csv_rows(out, month.format(c))
               if r["transaction_addresses"] == OLD_LOCATED]
    assert len(written) == 3 and {(r["longitude"], r["latitude"]) for r in written} == {("121.55", "25.02")}
    table = pq.read_table(out["root"] / "release" / "taiwanlvr.parquet").to_pylist()
    assert len([r for r in table if r["transaction_addresses"] == OLD_LOCATED]) == 3
    assert not [r for r in table if r["transaction_addresses"] == OLD_MISSING]


def test_a_manual_decision_on_a_remapped_key_reaches_the_output(world):
    decision = [{"review_key": _key(CURRENT_MISSING), "decision": "adopted", "mode": "manual_coordinate",
                 "lng": "121.5", "lat": "25.0", "address": MANUAL_ADDRESS}]
    review = review_of(world, tgos=True, decisions=decisions_dir(world, decision))
    entry = by_address(second(review), CURRENT_MISSING)
    assert entry["status"] == "adopted" and entry["observation_count"] == 6
    final = final_of(world, review, tgos=True)
    sources = [r for r in sources_of(final) if r["source"] == "manual"]
    assert len(sources) == entry["observation_count"]
    assert {r["building_key"] for r in sources} == {_key(CURRENT_MISSING)}
    assert {(r["after_lng"], r["after_lat"]) for r in sources} == {(121.5, 25.0)}
    out = output_of(world, "decided", final)
    doors = [r for r in iter_door_rows(world["converted"], world["tgos"], spill_dir=world["tmp"] / "spill2",
                                       final_state=final) if r["transaction_addresses"] == OLD_MISSING]
    assert len(doors) == 6 and {r["address"] for r in doors} == {MANUAL_ADDRESS}
    written = [r for c in ("sales", "presale", "rent") for m in ("202504",)
               for r in csv_rows(out, f"csv/2025/taiwanlvr_{m}_{c}.csv") if r["address"] == MANUAL_ADDRESS]
    assert len(written) == 6
