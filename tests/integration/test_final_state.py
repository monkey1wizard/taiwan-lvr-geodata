"""R10-1b: final-state stage, door addresses of updated doors, affected months and the catalog proof."""

import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lvr_pipeline.output.backfill import compare_catalogs  # noqa: E402
from lvr_pipeline.output.build import write_output  # noqa: E402
from lvr_pipeline.output.catalog import write_catalog  # noqa: E402
from lvr_pipeline.output.rows import iter_door_rows  # noqa: E402
from lvr_pipeline.results.final_state import build_final_state  # noqa: E402
from lvr_pipeline.results.reconcile import haversine_m, load_p2  # noqa: E402
from lvr_pipeline.results.review import build_review  # noqa: E402
from lvr_pipeline.storage.parquet import rows  # noqa: E402
from lvr_pipeline.storage.runs import load_snapshot, sha256_file  # noqa: E402
from test_second_review import (  # noqa: E402
    ADDRESS, FOUND, NOT_FOUND, PLAIN, _decision_file, _key, build_base, by_address, second,
)

MANUAL_ADDRESS = "臺北市中正區測試路11號(人工判定)"
# NOT_FOUND also trades in March 2025, ADDRESS also in June 2025 (a month nothing changes in).
EXTRA = [{"address": NOT_FOUND, "date": "1140315"}, {"address": ADDRESS, "date": "1140601"}]
CATEGORIES = ("sales", "presale", "rent")
_count = {"n": 0}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return build_base(tmp_path_factory.mktemp("final_state"), EXTRA)


def fresh(world, name):
    _count["n"] += 1
    return world["tmp"] / f"{name}-{_count['n']}"


def review_of(world, *, tgos, decisions=None):
    return build_review(
        world["converted"], world["state"], fresh(world, "review"), descriptor=world["descriptor"],
        tgos_state=world["tgos"] if tgos else None, index=world["index"], review_decisions=decisions)


def final_of(world, review, *, tgos, baseline=None, work=None):
    return build_final_state(
        world["converted"], world["state"], work or fresh(world, "final"), review=review,
        tgos_state=world["tgos"] if tgos else None, baseline=baseline)


def decisions_dir(world, items):
    directory = fresh(world, "decisions")
    _decision_file(directory, "20261018", 1, items)
    return directory


def manual_decision(world, address, lng, lat, new_address):
    return [{"review_key": _key(address), "decision": "adopted", "mode": "manual_coordinate",
             "lng": str(lng), "lat": str(lat), "address": new_address}]


def output_of(world, name, final=None):
    root = fresh(world, f"out-{name}")
    summary = write_output(world["converted"], world["state"], root / "public", root / "release",
                           spill_dir=root / "spill", final_state=final)
    _, report = load_snapshot(world["converted"], "converted")
    catalog = write_catalog(root / "public", root / "release", summary, converted_report=report, cutoff_yyyymm="202610")
    return {"root": root, "catalog": catalog}


def affected_of(final):
    return json.loads((final / "affected_months.json").read_text(encoding="utf-8"))


def sources_of(final):
    return list(rows(final / "coordinate_sources.parquet"))


def csv_rows(output, path):
    with (output["root"] / "public" / path).open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def changed_files(old, new):
    old_files = {e["path"]: e["sha256"] for e in old["catalog"]["files"]}
    return sorted(e["path"] for e in new["catalog"]["files"] if old_files.get(e["path"]) != e["sha256"])


def test_no_decisions_and_no_tgos_equal_the_offline_state(world):
    review = review_of(world, tgos=False)
    final = final_of(world, review, tgos=False)
    assert list(rows(final / "address_index.parquet")) == list(rows(world["state"] / "address_index.parquet"))
    assert sources_of(final) == []
    affected = affected_of(final)
    assert affected["months"] == [] and affected["files"] == [] and affected["removed_total"] == 0
    assert affected["baseline_kind"] == "offline-state"
    assert affected["baseline_manifest_sha256"] == sha256_file(world["state"] / "manifest.json")
    offline, with_final = output_of(world, "offline"), output_of(world, "final", final)
    assert changed_files(offline, with_final) == []
    result = compare_catalogs(offline["catalog"], with_final["catalog"], affected)
    assert result["ok"] and result["errors"] == [] and result["warnings"] == [] and result["checked"] > 0
    assert load_p2(final, "final-state")[1]["dataset_counts"] == {
        "final-address-result": len(list(rows(world["state"] / "address_index.parquet"))), "coordinate-source": 0}


def test_manual_decision_updates_only_its_months_and_categories(world):
    # Baseline: the TGOS adoption alone; the decision then changes only the doors of NOT_FOUND.
    tgos_only = final_of(world, review_of(world, tgos=True), tgos=True)
    directory = decisions_dir(world, manual_decision(world, NOT_FOUND, 121.5, 25.0, MANUAL_ADDRESS))
    review = review_of(world, tgos=True, decisions=directory)
    final = final_of(world, review, tgos=True, baseline=tgos_only)
    row = by_address(second(review), NOT_FOUND)
    assert row["status"] == "adopted" and row["observation_count"] == 6
    # The coordinate sources are relative to the offline state: the 3 TGOS doors of FOUND stay in them.
    assert [r["source"] for r in sources_of(final)].count("tgos") == 3
    sources = [r for r in sources_of(final) if r["source"] == "manual"]
    assert len(sources) == row["observation_count"]
    assert {r["source"] for r in sources} == {"manual"} and {r["building_key"] for r in sources} == {_key(NOT_FOUND)}
    assert {(r["after_lng"], r["after_lat"]) for r in sources} == {(121.5, 25.0)}
    assert {r["after_address"] for r in sources} == {MANUAL_ADDRESS} and {r["before_address"] for r in sources} == {NOT_FOUND}
    assert all(r["before_lng"] is None and r["decision_file"] and r["decision_row"] == 1 for r in sources)
    assert {(r["period_year"], r["period_month"], r["category"]) for r in sources} == {
        (y, m, c) for y, m in ((2025, 3), (2026, 1)) for c in sorted(CATEGORIES)}
    affected = affected_of(final)
    assert [(m["year"], m["month"], m["category"], m["added"], m["updated"], m["removed"]) for m in affected["months"]] == [
        (y, m, c, 1, 0, 0) for y, m in ((2025, 3), (2026, 1)) for c in sorted(CATEGORIES)]
    assert affected["baseline_kind"] == "final-state"
    assert affected["baseline_manifest_sha256"] == sha256_file(tgos_only / "manifest.json")
    expected_csv = sorted(f"csv/{y}/taiwanlvr_{y}{m}_{c}.csv" for y, m in ((2025, "03"), (2026, "01")) for c in CATEGORIES)
    assert affected["files"] == expected_csv + ["taiwanlvr.parquet", "catalog.json"]
    before, after = output_of(world, "tgos-only", tgos_only), output_of(world, "manual", final)
    assert changed_files(before, after) == expected_csv + ["taiwanlvr.parquet"]
    proof = compare_catalogs(before["catalog"], after["catalog"], affected)
    assert proof["ok"] and proof["warnings"] == []
    # Unaffected months are byte-identical.
    for path in ("csv/2025/taiwanlvr_202506_sales.csv", "csv/2025/taiwanlvr_202506_rent.csv"):
        assert (before["root"] / "public" / path).read_bytes() == (after["root"] / "public" / path).read_bytes()
    door = [r for r in csv_rows(after, "csv/2026/taiwanlvr_202601_sales.csv") if r["address"] == MANUAL_ADDRESS]
    assert len(door) == 1 and (door[0]["longitude"], door[0]["latitude"]) == ("121.5", "25.0")
    # Against the offline state the TGOS adoption is listed too, and the proof still holds.
    offline = output_of(world, "offline")
    from_offline = affected_of(final_of(world, review, tgos=True))
    assert compare_catalogs(offline["catalog"], after["catalog"], from_offline)["ok"]
    assert from_offline["baseline_kind"] == "offline-state"
    assert from_offline["baseline_manifest_sha256"] == sha256_file(world["state"] / "manifest.json")
    assert {m["csv_path"] for m in from_offline["months"]} >= set(expected_csv)
    # Against this run's TGOS state (explicit baseline) only the decision is listed.
    from_tgos = affected_of(final_of(world, review, tgos=True, baseline=world["tgos"]))
    assert from_tgos["baseline_kind"] == "tgos-state"
    assert from_tgos["baseline_manifest_sha256"] == sha256_file(world["tgos"] / "manifest.json")
    assert {m["csv_path"] for m in from_tgos["months"]} == set(expected_csv)


def test_new_tgos_adoption_is_a_source_tgos_update(world):
    review = review_of(world, tgos=True)
    final = final_of(world, review, tgos=True)
    sources = sources_of(final)
    found = world["results"][FOUND]
    assert len(sources) == 3 and {r["source"] for r in sources} == {"tgos"}
    assert {r["building_key"] for r in sources} == {_key(FOUND)}
    assert {r["tgos_result_id"] for r in sources} == {found["result_id"]}
    assert {(r["after_lng"], r["after_lat"]) for r in sources} == {(121.54, 25.04)}
    assert {r["after_address"] for r in sources} == {found["response_address"]} and {r["before_lng"] for r in sources} == {None}
    assert all(r["review_key"] is None and r["decision_file"] is None for r in sources)
    # Without a baseline the offline state is the baseline: the TGOS adoption is new. Against the TGOS state it is not.
    affected = affected_of(final)
    assert affected["baseline_kind"] == "offline-state"
    own = affected_of(final_of(world, review, tgos=True, baseline=world["tgos"]))
    assert own["baseline_kind"] == "tgos-state" and own["months"] == [] and own["files"] == []
    assert [(m["category"], m["added"]) for m in affected["months"]] == [(c, 1) for c in sorted(CATEGORIES)]
    offline, with_tgos = output_of(world, "offline"), output_of(world, "tgos", final)
    expected = [m["csv_path"] for m in affected["months"]] + ["taiwanlvr.parquet"]
    assert changed_files(offline, with_tgos) == sorted(expected)
    assert compare_catalogs(offline["catalog"], with_tgos["catalog"], affected)["ok"]
    rows_written = [r for r in csv_rows(with_tgos, "csv/2026/taiwanlvr_202601_sales.csv") if r["address"] == found["response_address"]]
    assert len(rows_written) == 1
    report = json.loads((final / "quality.json").read_text(encoding="utf-8"))
    assert report["source_doors"] == {"tgos": 3, "manual": 0} and report["tgos_response_address_missing"] == {"keys": 0, "doors": 0}


@pytest.mark.parametrize("lng,kept", [(121.5401, True), (121.55, False)])
def test_tgos_manual_conflict_keeps_tgos_within_tolerance_and_drops_both_beyond(world, lng, kept):
    # FOUND is located by TGOS at (121.54, 25.04); the owner adopted another coordinate.
    distance = haversine_m((121.54, 25.04), (lng, 25.04))
    assert (distance <= 30) is kept
    directory = decisions_dir(world, manual_decision(world, FOUND, lng, 25.04, "臺北市中正區測試路15號(人工)"))
    review = review_of(world, tgos=True, decisions=directory)
    entry = by_address(second(review), FOUND)
    assert entry["entry_reason"] == "tgos_manual_conflict" and entry["status"] == "adopted"
    # Against the offline state a dropped TGOS coordinate is no removal; against this run's own TGOS state it would be.
    final = final_of(world, review, tgos=True)
    result = {r["building_key"]: r for r in rows(final / "address_index.parquet")}[_key(FOUND)]
    report = json.loads((final / "quality.json").read_text(encoding="utf-8"))["tgos_manual_conflict"]
    output = output_of(world, "conflict", final)
    written = [r["address"] for r in csv_rows(output, "csv/2026/taiwanlvr_202601_sales.csv")]
    if kept:
        assert (result["status"], result["lng"], result["lat"]) == ("located", 121.54, 25.04)
        sources = sources_of(final)
        assert {r["source"] for r in sources} == {"tgos"} and {r["review_key"] for r in sources} == {_key(FOUND)}
        assert {r["after_address"] for r in sources} == {world["results"][FOUND]["response_address"]}
        assert report["kept_tgos"] == 1 and report["conflict"] == 0 and report["kept_tgos_doors"] == 3
        assert world["results"][FOUND]["response_address"] in written
    else:
        assert (result["status"], result["lng"], result["lat"], result["evidence_id"]) == ("conflict", None, None, None)
        assert sources_of(final) == [] and affected_of(final)["months"] == []
        with pytest.raises(ValueError, match="3 published doors would disappear"):
            final_of(world, review, tgos=True, baseline=world["tgos"])
        assert report["kept_tgos"] == 0 and report["conflict"] == 1 and report["conflict_doors"] == 3
        assert not [a for a in written if "15號" in a]
        assert not [r for r in iter_door_rows(world["converted"], world["state"], spill_dir=fresh(world, "s"),
                                              final_state=final) if "15號" in r["address"]]


def test_a_removed_door_stops_the_stage_and_publishes_nothing(world):
    with_tgos = final_of(world, review_of(world, tgos=True), tgos=True)
    review = review_of(world, tgos=False)
    work = fresh(world, "removed")
    with pytest.raises(ValueError, match="3 published doors would disappear"):
        final_of(world, review, tgos=False, baseline=with_tgos, work=work)
    assert list((work / "final-state").glob("snapshots/*")) == []
    # The same inputs without the baseline are fine.
    assert final_of(world, review, tgos=False, work=work).is_dir()


def test_rerun_with_identical_inputs_gives_identical_files_and_reuses_the_stage(world):
    directory = decisions_dir(world, manual_decision(world, NOT_FOUND, 121.5, 25.0, MANUAL_ADDRESS))
    review = review_of(world, tgos=True, decisions=directory)
    work = fresh(world, "stable")
    first = final_of(world, review, tgos=True, work=work)
    assert final_of(world, review, tgos=True, work=work) == first
    other = final_of(world, review, tgos=True)
    assert other != first
    names = ("address_index.parquet", "coordinate_sources.parquet", "affected_months.json")
    assert {n: sha256_file(first / n) for n in names} == {n: sha256_file(other / n) for n in names}
    # The output of a final state needs the converted and status state it was built from: the TGOS
    # state of this final state is accepted; a TGOS state it did not read is not.
    assert list(iter_door_rows(world["converted"], world["tgos"], spill_dir=fresh(world, "s"), final_state=first))
    without_tgos = final_of(world, review_of(world, tgos=False), tgos=False)
    with pytest.raises(ValueError, match="not built from"):
        list(iter_door_rows(world["converted"], world["tgos"], spill_dir=fresh(world, "s"), final_state=without_tgos))


def test_compare_catalogs_reports_every_kind_of_difference():
    def entry(path, rows=1, sha="a", size=10):
        return {"path": path, "rows": rows, "sha256": sha, "size_bytes": size}

    old = {"files": [entry("csv/a.csv"), entry("csv/b.csv"), entry("taiwanlvr.parquet", 2)], "total_rows": 2, "cutoff_yyyymm": "202610"}
    new = {"files": [entry("csv/a.csv", sha="b"), entry("csv/b.csv", rows=2, sha="b"), entry("csv/c.csv"),
                     entry("taiwanlvr.parquet", 3, sha="c")], "total_rows": 3, "cutoff_yyyymm": "202611"}
    month = {"year": 2026, "month": 1, "category": "sales", "added": 1, "updated": 0, "removed": 0}
    affected = {"months": [{**month, "csv_path": "csv/b.csv"}], "files": ["csv/b.csv", "taiwanlvr.parquet", "catalog.json"]}
    result = compare_catalogs(old, new, affected)
    assert not result["ok"]
    assert any("csv/a.csv: sha256 changed" in e for e in result["errors"])
    assert any("csv/c.csv: added but not in the affected list" in e for e in result["errors"])
    assert result["info"] == ["cutoff_yyyymm: '202610' to '202611'"] and result["warnings"] == []
    wrong = compare_catalogs(old, {**new, "files": [entry("csv/a.csv"), entry("csv/b.csv", rows=5, sha="b"),
                                                    entry("taiwanlvr.parquet", 3, sha="c")]}, affected)
    assert any("csv/b.csv: rows grew by 4, the list records 1" in e for e in wrong["errors"])
    same = compare_catalogs(old, {"files": [entry("csv/a.csv"), entry("csv/b.csv", rows=2), entry("taiwanlvr.parquet", 3, sha="c")]}, affected)
    assert same["ok"] and same["warnings"] == ["csv/b.csv: listed as affected but its sha256 did not change"]


def test_district_query_decision_places_its_doors_through_the_coordinate_sources(world):
    plain = by_address(second(review_of(world, tgos=True)), PLAIN)
    assert plain["key_kind"] == "district_query"
    directory = decisions_dir(world, [{"review_key": plain["review_key"], "decision": "adopted", "mode": "manual_coordinate",
                                       "lng": "121.5", "lat": "25.0", "address": "臺北市中正區測試路99號"}])
    review = review_of(world, tgos=True, decisions=directory)
    tgos_only = final_of(world, review_of(world, tgos=True), tgos=True)
    final = final_of(world, review, tgos=True, baseline=tgos_only)
    sources = [r for r in sources_of(final) if r["building_key"] is None]
    assert len(sources) == 3 and {r["source"] for r in sources} == {"manual"}
    assert {r["review_key"] for r in sources} == {plain["review_key"]}
    assert {(r["before_lng"], r["after_lng"], r["after_lat"]) for r in sources} == {(None, 121.5, 25.0)}
    assert {r["after_address"] for r in sources} == {"臺北市中正區測試路99號"} and {r["before_address"] for r in sources} == {PLAIN}
    affected = affected_of(final)
    assert [(m["category"], m["added"]) for m in affected["months"]] == [(c, 1) for c in sorted(CATEGORIES)]
    before, after = output_of(world, "district-before", tgos_only), output_of(world, "district-after", final)
    assert compare_catalogs(before["catalog"], after["catalog"], affected)["ok"]
    assert [r["address"] for r in csv_rows(after, "csv/2026/taiwanlvr_202601_sales.csv")].count("臺北市中正區測試路99號") >= 1
    report = json.loads((final / "quality.json").read_text(encoding="utf-8"))["decisions"]
    assert report["district_keys_adopted"] == 1 and report["district_doors"] == 3 and report["district_adoption_not_applied"] == 0


@pytest.fixture(scope="module")
def other_run(tmp_path_factory):
    """The same synthetic data built again in another directory, as an earlier run would be."""
    return build_base(tmp_path_factory.mktemp("final_state_other"), EXTRA)


def test_baseline_from_another_run_is_accepted_by_door_identity(world, other_run):
    first, second_run = (sha256_file(w["converted"] / "manifest.json") for w in (world, other_run))
    print("converted manifest sha256:", first, second_run)
    old = build_final_state(other_run["converted"], other_run["state"], fresh(other_run, "final"),
                            review=review_of(other_run, tgos=True), tgos_state=other_run["tgos"])
    directory = decisions_dir(world, manual_decision(world, NOT_FOUND, 121.5, 25.0, MANUAL_ADDRESS))
    final = final_of(world, review_of(world, tgos=True, decisions=directory), tgos=True, baseline=old)
    affected = affected_of(final)
    assert [(m["category"], m["added"], m["removed"]) for m in affected["months"]] == [
        (c, 1, 0) for _ in range(2) for c in sorted(CATEGORIES)]
    assert affected["baseline_kind"] == "final-state" and affected["baseline_manifest_sha256"] == sha256_file(old / "manifest.json")
    record = json.loads((old / "quality.json").read_text(encoding="utf-8"))
    from lvr_pipeline.storage.runs import digest
    assert affected["baseline_config_fingerprint"] == digest(record["producer_config"]["producer_files"])
    assert affected["baseline_run_id"] == record["run_id"]


def test_baseline_of_another_program_version_is_rejected(world, other_run, monkeypatch):
    import lvr_pipeline.results.final_state as module
    real = module.bindings

    def other_program(*args, **kwargs):
        binding = real(*args, **kwargs)
        config = json.loads(json.dumps(module.PRODUCER_CONFIGS[binding["config_sha256"]]))
        name = sorted(config["producer_files"])[0]
        config["producer_files"][name] = "0" * 64
        fingerprint = module.digest(config)
        module.PRODUCER_CONFIGS[fingerprint] = config
        return {**binding, "config_sha256": fingerprint}

    with monkeypatch.context() as patch:
        patch.setattr(module, "bindings", other_program)
        old = build_final_state(other_run["converted"], other_run["state"], fresh(other_run, "final"),
                                review=review_of(other_run, tgos=True), tgos_state=other_run["tgos"])
    with pytest.raises(ValueError, match="different program version"):
        final_of(world, review_of(world, tgos=True), tgos=True, baseline=old)


def test_baseline_may_be_a_tgos_state_or_an_offline_state_and_other_kinds_are_refused(world):
    review = review_of(world, tgos=True)
    default = affected_of(final_of(world, review, tgos=True))
    explicit = affected_of(final_of(world, review, tgos=True, baseline=world["tgos"]))
    assert default["baseline_kind"] == "offline-state" and default["months"]
    assert explicit["months"] == [] and explicit["baseline_kind"] == "tgos-state"
    assert explicit["baseline_manifest_sha256"] == sha256_file(world["tgos"] / "manifest.json")
    with pytest.raises(ValueError, match="Baseline must be"):
        final_of(world, review, tgos=True, baseline=review)
