"""E-00: a district_missing address is filled in the address pool from an adopted TGOS district-fill answer.

The answer is read from the TGOS ledger and its response file, checked again with the rules in force
(``adoption_response_key`` and ``_check_district_fill``), and only then used. Synthetic data only.
"""

import json
import shutil

import pytest

from lvr_pipeline.addresses.identity import building_key
from lvr_pipeline.addresses.pool import FILLED_REASONS, build_pool
from lvr_pipeline.contracts.validate import validate_dataset_rows
from lvr_pipeline.results.reconcile import load_p2, resolve_offline
from lvr_pipeline.storage.parquet import rows
from lvr_pipeline.storage.runs import PRODUCER_CONFIGS, digest
from lvr_pipeline.tgos import import_tgos, prepare_tgos, transition_batch
from lvr_pipeline.tgos.validate import build_tgos_state, plan_tgos_state
from test_p1_conversion import ADDRESS
from test_p2_offline import index_of, run
from test_p3_tgos import response

PLAIN = "臺北市測試路99號"
VILLAGE = "臺北市大安里測試路98號"
NO_ROAD = "臺北市無此路7號"
KEY = "臺北市中正區測試路21號"
EXPLICIT = "臺北市文山區測試路99號3樓"
GOOD, OTHER = ("121.51", "25.01"), ("121.52", "25.02")
FILLED_PLAIN = "臺北市文山區測試路99號"
FILLED_VILLAGE = "臺北市中正區大安里測試路98號"


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e00")
    converted, root, descriptor, index, pool0, state0 = run(
        tmp,
        coordinates=[
            (ADDRESS, "63", "6300100", 121.5, 25.0),
            ("臺北市文山區測試路11號", "63", "6300800", 121.55, 25.02),
        ],
        records=[
            {"address": ADDRESS, "date": "1141102"},
            {"address": KEY, "date": "1150102"},
            {"address": PLAIN, "date": "1150103"},
            {"address": VILLAGE, "date": "1150104"},
            {"address": NO_ROAD, "date": "1150105"},
            {"address": EXPLICIT, "date": "1150106"},
        ],
    )
    work = tmp / "work"
    exchange = tmp / "exchange"
    prepared, folder = prepare_tgos(state0, work, exchange, index=index_of(state0), exchange_date="2026-10-18")
    batch_id = next(iter(rows(prepared / "tgos_batches.parquet")))["batch_id"]
    return {
        "tmp": tmp, "converted": converted, "root": root, "descriptor": descriptor, "index": index,
        "pool0": pool0, "state0": state0, "exchange": exchange, "folder": folder, "batch_id": batch_id,
        "submitted": transition_batch(prepared, work, batch_id, "submitted"),
        "queries": {r["address"]: r for r in rows(prepared / "tgos_queries.parquet")},
    }


def make_ledger(world, name, answers):
    """Import a response into its own copy of the exchange directory; ``answers`` maps a submitted
    address to (Response_Address, (x, y)). Returns (ledger snapshot, exchange directory)."""
    exchange = world["tmp"] / f"exchange-{name}"
    shutil.copytree(world["exchange"], exchange)
    finish = exchange / world["folder"].relative_to(world["exchange"]) / "Address_Finish.csv"
    blank = ("", ("", ""))
    response(finish, [
        {"Address": a, "Response_Address": answers.get(a, blank)[0],
         "Response_X": answers.get(a, blank)[1][0], "Response_Y": answers.get(a, blank)[1][1]}
        for a in world["queries"]
    ])
    ledger = import_tgos(world["submitted"], world["tmp"] / f"work-{name}", world["batch_id"], finish,
                         index=index_of(world["submitted"]))
    return ledger, exchange


def make_pool(world, name, answers, *, with_ledger=True):
    ledger, exchange = make_ledger(world, name, answers)
    work = world["tmp"] / f"pool-{name}"
    kwargs = {"tgos_ledger": ledger, "exchange_dir": exchange} if with_ledger else {}
    pool = build_pool(world["converted"], world["root"], world["descriptor"], work, index_path=world["index"], **kwargs)
    return {"pool": pool, "work": work, "ledger": ledger, "exchange": exchange}


def occurrences(world, pool):
    """{raw address as written: occurrence row} and {raw address: the address kept in the observation}."""
    raw = {}
    for path in (world["converted"] / "observations").rglob("*.parquet"):
        for row in rows(path):
            raw[row["raw_record_id"]] = row["raw_address"]
    found = {}
    for row in rows(pool / "address_occurrences.parquet"):
        found.setdefault(raw[row["raw_record_id"]], row)
    return found


@pytest.fixture(scope="module")
def adopted(world):
    answers = {PLAIN: (FILLED_PLAIN, GOOD), VILLAGE: (FILLED_VILLAGE, OTHER), KEY: (KEY, GOOD)}
    return make_pool(world, "good", answers)


def test_adopted_answer_fills_the_occurrence_from_the_transaction_text(world, adopted):
    found = occurrences(world, adopted["pool"])
    plain, village = found[PLAIN], found[VILLAGE]
    assert plain["reason"] == "tgos_district_fill" and "tgos_district_fill" in FILLED_REASONS
    assert plain["building_key"] == building_key(FILLED_PLAIN)
    assert (plain["county_code"], plain["town_code"], plain["normalized_address"]) == ("63", "6300800", FILLED_PLAIN)
    # The village is written by the transaction and kept; it never comes from the answer.
    assert village["reason"] == "tgos_district_fill" and village["building_key"] == building_key(FILLED_VILLAGE)
    assert village["normalized_address"] == FILLED_VILLAGE and village["town_code"] == "6300100"
    # The transaction text itself is untouched; only the occurrence row holds the filled address.
    assert {PLAIN, VILLAGE} <= set(found)
    # The offline fill rows are unchanged: an address without a candidate road stays district_missing.
    assert found[NO_ROAD]["reason"] == "district_missing" and found[NO_ROAD]["building_key"] is None
    assert found[KEY]["reason"] == "valid"
    candidates = {r["component_id"]: r for r in rows(adopted["pool"] / "district_candidates.parquet")}
    assert candidates[plain["component_id"]]["reason"] == "tgos_district_fill"
    for row in candidates.values():
        validate_dataset_rows([row], "district-candidate")


def test_representative_address_prefers_the_unfilled_member(world, adopted):
    # EXPLICIT writes the same door with a floor; the filled text sorts first but must not represent the key.
    pool_rows = {r["building_key"]: r for r in rows(adopted["pool"] / "unique_addresses.parquet")}
    shared = pool_rows[building_key(FILLED_PLAIN)]
    assert shared["record_count"] == 6 and shared["canonical_address"] == EXPLICIT
    only_filled = pool_rows[building_key(FILLED_VILLAGE)]
    assert only_filled["canonical_address"] == FILLED_VILLAGE and only_filled["record_count"] == 3


def test_report_counts_binding_and_reuse(world, adopted):
    _, report = load_p2(adopted["pool"], "address-pool")
    fill = report["tgos_district_fill"]
    assert (fill["queries"], fill["adopted"], fill["rejected"], fill["not_found"], fill["no_response"],
            fill["conflicting"], fill["adopted_fills"], fill["filled_occurrences"]) == (2, 2, 0, 0, 0, 0, 2, 6)  # three categories per record
    assert report["district_fill"]["tgos_district_fill"] == 6
    manifest = json.loads((adopted["pool"] / "manifest.json").read_text(encoding="utf-8"))
    parameters = PRODUCER_CONFIGS[manifest["bindings"]["config_sha256"]]["parameters"]
    ledger_fills = sorted([digest(["district_missing", "63", q["address"]]), town, building_key(address)]
                          for q, town, address in [(world["queries"][PLAIN], "文山區", FILLED_PLAIN),
                                                   (world["queries"][VILLAGE], "中正區", FILLED_VILLAGE)])
    assert parameters["tgos_district_fills"] == digest(ledger_fills)
    again = build_pool(world["converted"], world["root"], world["descriptor"], adopted["work"],
                       index_path=world["index"], tgos_ledger=adopted["ledger"], exchange_dir=adopted["exchange"])
    assert again == adopted["pool"]


def test_tgos_state_gives_the_filled_key_the_answered_coordinate(world, adopted):
    work = adopted["work"]
    offline = resolve_offline(adopted["pool"], world["index"], work)
    before = {r["building_key"]: r for r in rows(offline / "address_index.parquet")}
    for address in (FILLED_PLAIN, FILLED_VILLAGE):
        assert before[building_key(address)]["status"] == "unmatched"
    plan = plan_tgos_state(offline, adopted["ledger"], adopted["exchange"], work.parent)
    state = build_tgos_state(offline, work, plan, index=world["index"])
    after = {r["building_key"]: r for r in rows(state / "address_index.parquet")}
    plain, village = after[building_key(FILLED_PLAIN)], after[building_key(FILLED_VILLAGE)]
    assert (plain["status"], plain["lng"], plain["lat"]) == ("located", 121.51, 25.01)
    assert (village["status"], village["lng"], village["lat"]) == ("located", 121.52, 25.02)
    evidence = {r["evidence_id"]: r for r in rows(state / "address_observations.parquet")}
    assert evidence[plain["evidence_id"]]["source_kind"] == "tgos_result"
    assert evidence[plain["evidence_id"]]["normalized_address"] == FILLED_PLAIN
    _, report = load_p2(state, "tgos-state")
    assert report["tgos_district_fill_evidence_rows"] == 2
    assert report["status_counts"]["located"] == sum(r["status"] == "located" for r in after.values())


def test_answered_village_never_enters_the_key(world):
    built = make_pool(world, "village", {PLAIN: ("臺北市文山區復興里測試路99號", GOOD)})
    plain = occurrences(world, built["pool"])[PLAIN]
    assert plain["reason"] == "tgos_district_fill" and plain["building_key"] == building_key(FILLED_PLAIN)
    assert "village" not in json.loads(plain["building_key"][3:])
    offline = resolve_offline(built["pool"], world["index"], built["work"])
    plan = plan_tgos_state(offline, built["ledger"], built["exchange"], built["work"].parent)
    state = build_tgos_state(offline, built["work"], plan, index=world["index"])
    located = {r["building_key"]: r for r in rows(state / "address_index.parquet")}[plain["building_key"]]
    assert (located["status"], located["lng"], located["lat"]) == ("located", 121.51, 25.01)


@pytest.mark.parametrize(
    "name,answer",
    [
        ("town", ("臺北市大同區測試路99號", GOOD)),
        ("door", ("臺北市文山區測試路100號", GOOD)),
        ("road", ("臺北市文山區別條路99號", GOOD)),
        ("no_coordinate", ("", ("", ""))),
        ("outside", ("臺北市文山區測試路99號", ("0", "0"))),
    ],
)
def test_rejected_answer_leaves_the_address_district_missing(world, name, answer):
    built = make_pool(world, "bad-" + name, {PLAIN: answer, VILLAGE: answer})
    found = occurrences(world, built["pool"])
    assert found[PLAIN]["reason"] == found[VILLAGE]["reason"] == "district_missing"
    assert found[PLAIN]["building_key"] is None and found[PLAIN]["normalized_address"] == PLAIN
    _, report = load_p2(built["pool"], "address-pool")
    fill = report["tgos_district_fill"]
    assert fill["adopted"] == 0 and fill["adopted_fills"] == 0 and fill["filled_occurrences"] == 0
    assert fill["rejected"] + fill["not_found"] == 2
    manifest = json.loads((built["pool"] / "manifest.json").read_text(encoding="utf-8"))
    assert "tgos_district_fills" not in PRODUCER_CONFIGS[manifest["bindings"]["config_sha256"]]["parameters"]
    offline = resolve_offline(built["pool"], world["index"], built["work"])
    plan = plan_tgos_state(offline, built["ledger"], built["exchange"], built["work"].parent)
    state = build_tgos_state(offline, built["work"], plan, index=world["index"])
    assert not [r for r in rows(state / "address_index.parquet") if r["status"] == "located" and r["building_key"] and
                json.loads(r["building_key"][3:])["door"] in {"99號", "98號"}]
    _, state_report = load_p2(state, "tgos-state")
    assert "tgos_district_fill_evidence_rows" not in state_report


def test_without_a_ledger_or_without_adopted_fills_the_pool_is_unchanged(world):
    plain = make_pool(world, "none", {}, with_ledger=False)
    rejected = make_pool(world, "none-ledger", {PLAIN: ("臺北市大同區測試路99號", GOOD)})
    assert plain["pool"].name == rejected["pool"].name == world["pool0"].name
    for name in ("address_occurrences.parquet", "unique_addresses.parquet", "district_candidates.parquet"):
        assert list(rows(plain["pool"] / name)) == list(rows(rejected["pool"] / name)) == list(rows(world["pool0"] / name))
    _, report = load_p2(plain["pool"], "address-pool")
    assert "tgos_district_fill" not in report
    manifest = json.loads((plain["pool"] / "manifest.json").read_text(encoding="utf-8"))
    parameters = PRODUCER_CONFIGS[manifest["bindings"]["config_sha256"]]["parameters"]
    assert set(parameters) == {"address_commit", "coordinate_confirmed_candidates", "district_fill_rule",
                               "normalization_version", "admin_towns_sha256", "repeated_county_rule"}


def test_changed_response_file_is_refused(world):
    ledger, exchange = make_ledger(world, "tamper", {PLAIN: (FILLED_PLAIN, GOOD)})
    finish = exchange / world["folder"].relative_to(world["exchange"]) / "Address_Finish.csv"
    finish.write_text(finish.read_text(encoding="utf-8-sig").replace("121.51", "121.53"), encoding="utf-8-sig")
    with pytest.raises(ValueError, match="sha256"):
        build_pool(world["converted"], world["root"], world["descriptor"], world["tmp"] / "pool-tamper",
                   index_path=world["index"], tgos_ledger=ledger, exchange_dir=exchange)


def test_ledger_needs_the_index_and_the_exchange_directory(world):
    with pytest.raises(ValueError, match="needs the offline index"):
        build_pool(world["converted"], world["root"], world["descriptor"], world["tmp"] / "pool-bad-args",
                   tgos_ledger=world["submitted"])
