"""R09-2: full same-door check of TGOS responses (rules A, B and C)."""

import pytest

from lvr_pipeline.results.review import TGOS_REJECTION_CODES
from lvr_pipeline.storage.parquet import rows
from lvr_pipeline.tgos import import_tgos, prepare_tgos, transition_batch
from test_p1_conversion import ADDRESS
from test_p2_offline import run
from test_p3_tgos import response

GOOD = ("121.51", "25.01")


def _import(tmp_path, query, pool=(), answer=None, coordinate=GOOD, extra_rows=(), drop=()):
    """Import one response for ``query``; ``pool`` addresses share the pool and are queried too."""
    records = [{"address": ADDRESS, "date": "1141102"}]
    records += [{"address": value, "date": "1150102"} for value in [query, *pool]]
    *_, state = run(tmp_path, coordinates=[(ADDRESS, "63", "6300100", 121.5, 25.0)], records=records)
    prepared, _ = prepare_tgos(state, tmp_path / "work", tmp_path / "exchange", exchange_date="2026-10-17")
    queries = list(rows(prepared / "tgos_queries.parquet"))
    batch_id = queries[0]["batch_id"]
    submitted = transition_batch(prepared, tmp_path / "work", batch_id, "submitted")
    values = [
        {"Address": item["address"], "Response_Address": "", "Response_X": "", "Response_Y": ""}
        for item in queries
        if item["address"] not in drop
    ]
    for item in values:
        if item["Address"] == query:
            item.update(Response_Address=query if answer is None else answer,
                        Response_X=coordinate[0], Response_Y=coordinate[1])
    values += list(extra_rows)
    result = tmp_path / "result.csv"
    response(result, values)
    imported = import_tgos(submitted, tmp_path / "work", batch_id, result)
    return {r["address"]: r for r in rows(imported / "tgos_imports.parquet")}[query]


def _rejected(result, reason):
    assert result["status"] == "rejected" and result["reason"] == reason
    assert reason in TGOS_REJECTION_CODES
    return TGOS_REJECTION_CODES[reason]


ROAD = "臺北市中正區測試路"


@pytest.mark.parametrize(
    "query,answer",
    [
        (f"{ROAD}341號", f"{ROAD}201號"),
        (f"{ROAD}64號", f"{ROAD}62號"),
        (f"{ROAD}58之25號", f"{ROAD}58之22號"),
        (f"{ROAD}21號", "臺北市中正區別路21號"),
    ],
)
def test_response_for_another_door_is_isolated(tmp_path, query, answer):
    result = _import(tmp_path, query, answer=answer)
    code = _rejected(result, "TGOS response address differs from submitted address")
    assert code == "tgos_isolated"


@pytest.mark.parametrize("case", ["missing", "extra"])
def test_response_row_set_must_equal_submitted_set(tmp_path, case):
    query = f"{ROAD}21號"
    kwargs = (
        {"drop": {query}}
        if case == "missing"
        else {"extra_rows": [{"Address": f"{ROAD}99號", "Response_Address": "", "Response_X": "", "Response_Y": ""}]}
    )
    with pytest.raises(ValueError, match="Address set differs"):
        _import(tmp_path, query, **kwargs)


def test_two_villages_for_the_same_door_are_ambiguous(tmp_path):
    result = _import(
        tmp_path, f"{ROAD}16號", pool=[f"臺北市中正區甲里測試路16號", f"臺北市中正區乙里測試路16號"],
        answer="臺北市中正區甲里測試路16號",
    )
    reason = "TGOS response village or neighbourhood is ambiguous"
    assert _rejected(result, reason) == "tgos_village_ambiguous"


def test_one_village_for_the_door_is_adopted(tmp_path):
    result = _import(
        tmp_path, f"{ROAD}16號", pool=["臺北市中正區甲里測試路16號"], answer="臺北市中正區甲里測試路16號"
    )
    assert result["status"] == "succeeded" and result["reason"] is None
    assert result["response_address"] == "臺北市中正區甲里測試路16號"


def test_two_neighbourhoods_in_one_village_are_ambiguous(tmp_path):
    pool = ["臺北市中正區甲里3鄰測試路20號", "臺北市中正區甲里5鄰測試路20號"]
    result = _import(tmp_path, "臺北市中正區甲里測試路20號", pool=pool, answer=pool[0])
    reason = "TGOS response village or neighbourhood is ambiguous"
    assert _rejected(result, reason) == "tgos_village_ambiguous"


def test_one_neighbourhood_in_the_village_is_adopted(tmp_path):
    pool = ["臺北市中正區甲里3鄰測試路20號"]
    result = _import(tmp_path, "臺北市中正區甲里測試路20號", pool=pool, answer=pool[0])
    assert result["status"] == "succeeded"


def test_pool_keys_of_another_village_are_ignored(tmp_path):
    pool = ["臺北市中正區乙里1鄰測試路21號", "臺北市中正區乙里2鄰測試路21號", "臺北市中正區乙里測試路21號"]
    result = _import(tmp_path, "臺北市中正區甲里測試路21號", pool=pool, answer="臺北市中正區甲里測試路21號")
    assert result["status"] == "succeeded"


@pytest.mark.parametrize(
    "coordinate,reason",
    [
        (("25.03", "121.56"), "TGOS coordinate is outside declared WGS84 Taiwan bounds"),
        (("302000", "2770000"), "TGOS coordinate is outside declared WGS84 Taiwan bounds"),
        (("121.51", ""), "Partial TGOS coordinate pair"),
        (("", "25.01"), "Partial TGOS coordinate pair"),
        (("abc", "25.01"), "TGOS coordinate is not numeric"),
    ],
)
def test_bad_coordinates_map_to_coordinate_invalid(tmp_path, coordinate, reason):
    result = _import(tmp_path, f"{ROAD}21號", coordinate=coordinate)
    assert _rejected(result, reason) == "tgos_coordinate_invalid"


def test_success_row_without_response_address_is_incomplete(tmp_path):
    result = _import(tmp_path, f"{ROAD}21號", answer=" ")
    assert _rejected(result, "Successful TGOS row lacks Response_Address") == "tgos_response_incomplete"


def test_every_recorded_rejection_reason_has_a_review_code():
    import inspect
    import re

    from lvr_pipeline.tgos import validate

    # The per-row checks: the coordinate parser and the try block of import_tgos.
    text = inspect.getsource(validate)
    source = text[text.index("def _tgos_coordinate"):text.index("def _layer_is_ambiguous")]
    source += text[text.index("        reason = None"):text.index("except (TypeError, ValueError)")]
    raised = set(re.findall(r'raise ValueError\(\s*"([^"]+)"', source))
    assert raised and raised <= set(TGOS_REJECTION_CODES)
    assert set(TGOS_REJECTION_CODES.values()) <= {
        "tgos_isolated", "tgos_response_incomplete", "tgos_coordinate_invalid", "tgos_village_ambiguous",
    }
