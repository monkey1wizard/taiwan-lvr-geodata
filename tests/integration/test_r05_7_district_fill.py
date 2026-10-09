"""R05-7: offline district fill for addresses without a town, and road-only addresses.

Owner decisions of 2026-10-07 and 2026-10-09. A county with no town is looked up in the
offline index of that county by street or place, lanes and door; a road-only address in its
source county (ZIP member letter), the whole country only when that county is unknown. One
candidate town fills the town in as evidence; several towns or none are not filled. Located,
conflict and cross-check outcomes come from the R04-12 door rule. Synthetic data only.
"""

import csv
import json
import subprocess

import pytest

from lvr_pipeline.address_pool import build_pool, district_query
from lvr_pipeline.addresses.identity import building_key_v2, drop_repeated_county
from lvr_pipeline.contracts.validate import validate_dataset_rows
from lvr_pipeline.offline.index import build_index
from lvr_pipeline.offline.match import AdministrativeNames
from lvr_pipeline.results.reconcile import DOOR_CROSS_CHECK, load_p2, resolve_offline
from lvr_pipeline.results.review import R05_7_CODES, build_review
from lvr_pipeline.storage.parquet import rows
from lvr_pipeline.storage.runs import sha256_file
from test_p1_conversion import pipeline

P1, P2, P3, P4, P5 = (120.97, 24.80), (120.98, 24.81), (120.99, 24.82), (121.51, 25.03), (120.93, 24.78)
TOWNS = {
    "新竹市東區": "1001801", "新竹市北區": "1001802", "新竹市香山區": "1001803",
    "臺北市中正區": "6300100", "臺北市大安區": "6300300", "嘉義市東區": "1002001",
}
# (full address, town, road, coordinate)
SOURCE = [
    ("新竹市東區明湖路100號", "新竹市東區", "明湖路", P1),
    ("新竹市東區光復路1號", "新竹市東區", "光復路", P1),
    ("新竹市北區光復路1號", "新竹市北區", "光復路", P2),
    ("新竹市東區同點路1號", "新竹市東區", "同點路", P3),
    ("新竹市北區同點路1號", "新竹市北區", "同點路", P3),
    ("新竹市東區甲里1鄰和平路5號", "新竹市東區", "和平路", P1),
    ("新竹市北區乙里2鄰和平路5號", "新竹市北區", "和平路", P2),
    ("新竹市香山區甲里1鄰香路7號", "新竹市香山區", "香路", P1),
    ("新竹市香山區乙里1鄰香路7號", "新竹市香山區", "香路", P5),
    ("新竹市東區共同路3號", "新竹市東區", "共同路", P1),
    ("臺北市中正區共同路3號", "臺北市中正區", "共同路", P4),
]
# county code of a member letter: o 新竹市, a 臺北市; y is not a county letter.
LETTERS = {"sales": "o", "presale": "a", "rent": "y"}
FULL = {
    "full_unique": "新竹市東區明湖路100號5樓",
    "full_taipei": "臺北市中正區共同路3號",
}
FILL = {
    "unique": "新竹市明湖路100號",
    "repeated_unique": "新竹市新竹市明湖路100號",
    "repeated_full": "新竹市新竹市東區光復路1號",
    "ambiguous": "新竹市光復路1號",
    "ambiguous_same_point": "新竹市同點路1號",
    "missing": "新竹市無此路9號",
    "village": "新竹市甲里和平路5號",
    "one_town_two_doors": "新竹市香路7號",
    "typo_city": "嘉義市嘉義市市東區民族路72號",
    "typo_letter": "臺北市v新興區民族二路72號",
    "old_county": "桃園縣中壢市中山路1號",
    "road_hsinchu_only": "明湖路100號",
    "road_both_counties": "共同路3號",
    "road_two_towns": "光復路1號",
    "road_missing": "無此路9號",
}


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def address_source(tmp_path):
    root = tmp_path / "address"
    (root / "roads").mkdir(parents=True)
    area = root / "area_2014.csv"
    area.write_text("name,dgbas_id\n" + "".join(f"{n},{c}\n" for n, c in TOWNS.items()), encoding="utf-8")
    custom = root / "area_custom.csv"
    custom.write_text("name,dgbas_id\n", encoding="utf-8")
    files = {}
    for address, town, road, (lng, lat) in SOURCE:
        code = TOWNS[town]
        county = code[:2] if code.startswith("63") else code[:5]
        files.setdefault(f"{county}-{road}.csv", []).append([address, county, code, road, lng, lat])
    for name, values in files.items():
        with (root / "roads" / name).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["FULL_ADDR", "COUNTY", "TOWN", "ROAD", "X", "Y"])
            writer.writerows(values)
    _git(root, "init")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "core.hooksPath=/dev/null", "commit", "-m", "fixture")
    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    return root, {
        "commit": commit,
        "administrative_files": [{"path": p.name, "sha256": sha256_file(p)} for p in [area, custom]],
        "roads": [{"path": "roads/" + name, "sha256": sha256_file(root / "roads" / name)} for name in sorted(files)],
    }


def build(tmp_path, addresses):
    records = [{"address": value} for value in addresses]
    _, _, converted = pipeline(tmp_path, records, county_letter=LETTERS)
    root, descriptor = address_source(tmp_path)
    work = tmp_path / "work"
    index = build_index(root, descriptor, work, counties=["10018", "63"])
    pool = build_pool(converted, root, descriptor, work, index_path=index)
    state = resolve_offline(pool, index, work)
    review = build_review(converted, state, tmp_path / "review", descriptor=descriptor, run_id="r05-7")
    return {"converted": converted, "root": root, "descriptor": descriptor, "pool": pool, "state": state,
            "review": review}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return build(tmp_path_factory.mktemp("r05-7"), [*FULL.values(), *FILL.values()])


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    return build(tmp_path_factory.mktemp("r05-7-base"), list(FULL.values()))


def occurrences(built):
    """{(address in the source, category letter): occurrence row}."""
    observations = {}
    for path in (built["converted"] / "observations").rglob("*.parquet"):
        for row in rows(path):
            observations[row["raw_record_id"]] = (row["raw_address"], row["member_path"][0])
    found = {}
    for row in rows(built["pool"] / "address_occurrences.parquet"):
        found[observations[row["raw_record_id"]]] = row
    return found


def occurrence(built, name, letter="o"):
    return occurrences(built)[({**FULL, **FILL}[name], letter)]


def status(built, key):
    return {row["building_key"]: row for row in rows(built["state"] / "address_index.parquet")}[key]


def candidates(built):
    return {row["component_id"]: row for row in rows(built["pool"] / "district_candidates.parquet")}


def review_rows(built, name="exception_addresses.parquet"):
    return list(rows(built["review"] / name))


# ── shared repeated-county rule ─────────────────────────────────────────────────


def test_repeated_county_rule_is_shared(built):
    assert drop_repeated_county("新竹市新竹市東區明湖路100號") == "新竹市東區明湖路100號"
    assert drop_repeated_county("嘉義市嘉義市嘉義市東區") == "嘉義市東區"
    assert drop_repeated_county("台北市臺北市中正區") == "台北市臺北市中正區"  # not an exact repeat
    names = AdministrativeNames(built["root"], built["descriptor"]["administrative_files"])
    assert names.canonicalize("新竹市新竹市東區明湖路100號") == ("新竹市東區明湖路100號", "10018", "1001801")
    assert names.canonicalize("新竹市新竹市明湖路100號") == ("新竹市明湖路100號", "", "")
    row = occurrence(built, "repeated_full")
    assert row["reason"] == "valid" and row["building_key"] == building_key_v2("新竹市東區光復路1號")


def test_district_query_splits_the_door_without_a_town():
    assert district_query("新竹市明湖路100號5樓") == ("10018", "明湖路100號5樓", "明湖路", "100", None, None)
    assert district_query("新竹市甲里1鄰和平路5號") == ("10018", "甲里1鄰和平路5號", "和平路", "5", "甲里", "1")
    assert district_query("慈濟路165號") == (None, "慈濟路165號", "慈濟路", "165", None, None)
    assert district_query("新竹市明湖路") is None


# ── county written, town missing ───────────────────────────────────────────────


@pytest.mark.parametrize("name", ["unique", "repeated_unique"])
def test_unique_town_is_filled_and_located(built, name):
    row = occurrence(built, name)
    assert row["reason"] == "road_unique_in_county"
    assert (row["county_code"], row["town_code"]) == ("10018", "1001801")
    assert row["normalized_address"] == "新竹市東區明湖路100號"
    assert row["building_key"] == building_key_v2("新竹市東區明湖路100號")
    result = status(built, row["building_key"])
    assert result["status"] == "located" and (result["lng"], result["lat"]) == P1
    detail = candidates(built)[row["component_id"]]
    assert (detail["search_scope"], detail["search_county_code"], detail["town_count"]) == ("county", "10018", 1)
    # The source text is never changed.
    for path in (built["converted"] / "observations").rglob("*.parquet"):
        assert all(r["raw_address"] in {**FULL, **FILL}.values() for r in rows(path))


def test_written_village_narrows_the_towns(built):
    row = occurrence(built, "village")
    assert row["reason"] == "road_unique_in_county" and row["town_code"] == "1001801"
    assert status(built, row["building_key"])["lng"] == P1[0]


def test_several_towns_are_ambiguous_and_go_to_cross_check(built):
    row = occurrence(built, "ambiguous")
    assert row["reason"] == "district_ambiguous" and row["building_key"] is None
    assert (row["county_code"], row["town_code"]) == ("10018", "")
    detail = candidates(built)[row["component_id"]]
    assert (detail["town_count"], detail["coordinate_count"]) == (2, 2)
    found = [json.loads(r["related_json"]) for r in review_rows(built) if r["reason_code"] == "district_ambiguous"
             and r["raw_address"] == FILL["ambiguous"]]
    assert len(found) == 3 and {c["town_code"] for c in found[0]["candidates"]} == {"1001801", "1001802"}
    cross = [r for r in review_rows(built, "cross_check_addresses.parquet") if r["raw_address"] == FILL["ambiguous"]]
    assert len(cross) == 3 and {r["reason_code"] for r in cross} == {DOOR_CROSS_CHECK}


def test_several_towns_with_one_coordinate_are_ambiguous_without_cross_check(built):
    row = occurrence(built, "ambiguous_same_point")
    assert row["reason"] == "district_ambiguous" and row["building_key"] is None
    assert candidates(built)[row["component_id"]]["coordinate_count"] == 1
    cross = review_rows(built, "cross_check_addresses.parquet")
    assert FILL["ambiguous_same_point"] not in {r["raw_address"] for r in cross}


def test_one_town_with_two_doors_is_filled_then_cross_checked(built):
    row = occurrence(built, "one_town_two_doors")
    assert row["reason"] == "road_unique_in_county" and row["town_code"] == "1001803"
    detail = {r["building_key"]: r for r in rows(built["state"] / "coordinate_resolutions.parquet")}
    assert status(built, row["building_key"])["status"] == "conflict"
    assert detail[row["building_key"]]["resolution_basis"] == DOOR_CROSS_CHECK


def test_no_candidate_is_district_missing(built):
    row = occurrence(built, "missing")
    assert row["reason"] == "district_missing" and row["building_key"] is None
    assert row["county_code"] == "10018"
    found = [r for r in review_rows(built) if r["reason_code"] == "district_missing"]
    assert {r["raw_address"] for r in found} == {FILL["missing"]}
    assert all(r["county_code"] == "10018" for r in found)


@pytest.mark.parametrize("name", ["typo_city", "typo_letter", "old_county"])
def test_source_typos_and_old_counties_stay_invalid_admin(built, name):
    row = occurrence(built, name)
    assert row["reason"] == "invalid_admin" and row["building_key"] is None


# ── road only ───────────────────────────────────────────────────────────────────


def test_road_only_unique_in_source_county(built):
    row = occurrence(built, "road_hsinchu_only", "o")
    assert row["reason"] == "road_only_unique" and row["town_code"] == "1001801"
    assert row["normalized_address"] == "新竹市東區明湖路100號"
    detail = candidates(built)[row["component_id"]]
    assert (detail["search_scope"], detail["source_county_code"]) == ("source_county", "10018")
    assert status(built, row["building_key"])["status"] == "located"
    # Source county 臺北市 has no such door; the search does not leave the source county.
    other = occurrence(built, "road_hsinchu_only", "a")
    assert other["reason"] == "road_only_not_unique" and other["building_key"] is None
    assert candidates(built)[other["component_id"]]["town_count"] == 0


def test_source_county_decides_between_counties(built):
    hsinchu = occurrence(built, "road_both_counties", "o")
    taipei = occurrence(built, "road_both_counties", "a")
    assert (hsinchu["reason"], hsinchu["town_code"]) == ("road_only_unique", "1001801")
    assert (taipei["reason"], taipei["town_code"]) == ("road_only_unique", "6300100")
    assert (status(built, taipei["building_key"])["lng"], status(built, taipei["building_key"])["lat"]) == P4


def test_unknown_source_county_searches_the_whole_country(built):
    unique = occurrence(built, "road_hsinchu_only", "y")
    assert unique["reason"] == "road_only_unique" and unique["town_code"] == "1001801"
    detail = candidates(built)[unique["component_id"]]
    assert (detail["search_scope"], detail["search_county_code"], detail["source_county_code"]) == (
        "nationwide", None, None)
    both = occurrence(built, "road_both_counties", "y")
    assert both["reason"] == "road_only_not_unique" and both["building_key"] is None
    assert candidates(built)[both["component_id"]]["town_count"] == 2
    cross = review_rows(built, "cross_check_addresses.parquet")
    assert any(r["raw_address"] == FILL["road_both_counties"] and r["category"] == "rent" for r in cross)


def test_road_only_not_unique_in_source_county(built):
    row = occurrence(built, "road_two_towns", "o")
    assert row["reason"] == "road_only_not_unique" and row["county_code"] == ""
    missing = occurrence(built, "road_missing", "o")
    assert missing["reason"] == "road_only_not_unique"
    found = {(r["raw_address"], r["category"]) for r in review_rows(built) if r["reason_code"] == "road_only_not_unique"}
    assert (FILL["road_two_towns"], "sales") in found and (FILL["road_missing"], "sales") in found
    related = next(json.loads(r["related_json"]) for r in review_rows(built)
                   if r["reason_code"] == "road_only_not_unique" and r["raw_address"] == FILL["road_two_towns"]
                   and r["category"] == "sales")
    assert related["source_county_code"] == "10018" and related["town_count"] == 2


# ── existing keys, contracts and reports ───────────────────────────────────────


def test_existing_keys_keep_their_results(built, baseline):
    before = {row["building_key"]: row for row in rows(baseline["state"] / "address_index.parquet")}
    after = {row["building_key"]: row for row in rows(built["state"] / "address_index.parquet")}
    assert before
    for key, row in before.items():
        for field in ["status", "lng", "lat", "evidence_id", "coordinate_count", "evidence_count",
                      "canonical_address", "county_code", "town_code"]:
            assert after[key][field] == row[field], (key, field)
    # The filled 新竹市東區明湖路100號 joins the key of the full address; the full address stays canonical.
    assert after[building_key_v2(FULL["full_unique"])]["canonical_address"] == FULL["full_unique"]
    full = occurrences(baseline)
    for (address, letter), row in occurrences(built).items():
        if (address, letter) in full:
            assert (row["building_key"], row["reason"]) == (full[(address, letter)]["building_key"],
                                                            full[(address, letter)]["reason"])


def test_snapshots_verify_and_report_the_fill(built):
    _, pool_report = load_p2(built["pool"], "address-pool")
    _, state_report = load_p2(built["state"], "offline-state")
    fill = pool_report["district_fill"]
    assert fill["fill_key_mismatch"] == 0
    assert fill["lookups"] == pool_report["district_candidate_rows"] == state_report["dataset_counts"][
        "district-candidate"]
    assert sum(fill[r] for r in ["road_unique_in_county", "road_only_unique", "district_ambiguous",
                                 "district_missing", "road_only_not_unique", "invalid_admin"]) == fill["lookups"]
    review = json.loads((built["review"] / "quality.json").read_text(encoding="utf-8"))
    assert all(review["reason_counts"][code] > 0 for code in R05_7_CODES)
    load_p2(built["review"], "review")


def test_district_candidate_contract(built):
    row = next(iter(candidates(built).values()))
    validate_dataset_rows([row], "district-candidate")
    with pytest.raises(ValueError, match="reason"):
        validate_dataset_rows([{**row, "reason": "guessed"}], "district-candidate")
    filled = next(r for r in candidates(built).values() if r["reason"] == "road_unique_in_county")
    with pytest.raises(ValueError, match="candidate"):
        validate_dataset_rows([{**filled, "town_count": 2}], "district-candidate")
