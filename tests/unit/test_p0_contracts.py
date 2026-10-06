import copy
import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from lvr_pipeline.address import building_key, building_key_v2
from lvr_pipeline.contracts import component_id, observation_id, validate_relations, validate_rows
from lvr_pipeline.tx_date import validated_roc_to_tx_yyyymm


@pytest.mark.parametrize("address", ["臺北市中正區測試路10之1號", "臺北市中正區測試路10號之1", "臺北市中正區測試路10號之1二樓"])
def test_equivalent_full_subdoor(address):
    assert building_key_v2(address) == building_key_v2("臺北市中正區測試路10號之1")


def test_distinct_subdoors_and_villages():
    assert building_key_v2("臺北市中正區測試路10號之1") != building_key_v2("臺北市中正區測試路10號之2")
    a = building_key_v2("金門縣金城鎮甲里10號")
    b = building_key_v2("金門縣金城鎮乙里10號")
    assert a and b and a != b
    assert json.loads(a.removeprefix("v2:"))["county"] == "09020"
    assert json.loads(building_key_v2("連江縣南竿鄉甲村10號").removeprefix("v2:"))["county"] == "09007"


@pytest.mark.parametrize("address", ["臺北市中正區測試路10-12號", "臺北市中正區測試路10-1號", "臺北市中正區測試路10一1號", "臺北市中正區測試路10號及12號", "臺北市中正區測試?路10號", "測試路10號", "臺北市中正區測試路10號對面", "臺北市中正區測試路10之1號之2"])
def test_ambiguous_or_unscoped_address(address):
    assert building_key_v2(address) is None


def test_legacy_stays_compatible():
    assert building_key("臺北市中正區測試路10號之1") == building_key("臺北市中正區測試路10號之2")
    assert building_key_v2("台北市中正區測試路１０號之１") == building_key_v2("臺北市中正區測試路10之1號")


@pytest.mark.parametrize("value,expected", [("1130229",202402),("1140229",None),("1140230",None),("1140900",None),("1151101",None),("1140901",202509),("００１０１０１",None),("0000101",None)])
def test_strict_dates(value, expected):
    assert validated_roc_to_tx_yyyymm(value, run_cutoff_yyyymm=202610) == expected


def test_bad_cutoff():
    with pytest.raises(ValueError):
        validated_roc_to_tx_yyyymm("1140901", run_cutoff_yyyymm=202613)


def observation(serial="same", sha="a"*64, member="a_lvr_land_a.csv", row=3):
    return {"schema_version":"1.0", "category":"sales", "src_batch":"115q1", "source_serial":serial,
        "input_sha256":sha, "member_path":member, "source_row_number":row,
        "raw_record_id":observation_id(sha,member,row), "record_grain":"source_observation", "transaction_key":None,
        "raw_address":"臺北市中正區測試路10號之1", "tx_date_raw":"1150102", "tx_yyyymm":202601,
        "run_cutoff_yyyymm":202610, "currency":"TWD", "amount_scale":100, "amount_minor":100000, "area_m2_decimal":"10.25", "props_json":{"unknown":"preserve"}, "parse_status":"retained"}


def component(record, ordinal=0):
    address = record["raw_address"]
    return {"raw_record_id":record["raw_record_id"],"component_id":component_id(record["raw_record_id"],ordinal),"ordinal":ordinal,
        "normalized_address":address,"key_version":"v2","building_key":building_key_v2(address),"legacy_building_key":building_key(address),"expansion_status":"confirmed"}


def test_unproven_serial_is_not_transaction_identity():
    a,b,c = observation(),observation(member="b_lvr_land_a.csv"),observation(sha="b"*64)
    validate_rows([a,b,c], "observation")
    assert len({r["raw_record_id"] for r in [a,b,c]}) == 3
    guessed = copy.deepcopy(a)
    guessed["transaction_key"] = "same"
    with pytest.raises(ValidationError):
        validate_rows([guessed], "observation")


def test_amounts_do_not_expand_and_unknown_fields_survive():
    record = observation()
    expanded = [component(record,0),component(record,1)]
    validate_relations([record],expanded,[])
    assert record["amount_minor"] == 100000 and record["props_json"]["unknown"] == "preserve"
    expanded[0]["amount_minor"] = 100000
    with pytest.raises(ValidationError):
        validate_relations([record],expanded,[])


def test_lineage_and_relation_guards():
    record = observation()
    with pytest.raises(ValueError, match="Duplicate"):
        validate_rows([record,record],"observation")
    with pytest.raises(ValueError,match="Orphan"):
        validate_relations([record],[component(observation(sha="b"*64))],[])
    with pytest.raises(ValueError):
        validate_relations([record],[component(record),component(record)],[])
    with pytest.raises(ValueError):
        validate_relations([record],[],[{"raw_record_id":record["raw_record_id"],"reason":"land","source_ref":"row3"}])
    bad=copy.deepcopy(record)
    bad["raw_record_id"]="f"*64
    with pytest.raises(ValueError,match="lineage"):
        validate_rows([bad],"observation")
    bad=copy.deepcopy(record)
    bad["tx_yyyymm"]=202513
    with pytest.raises(ValueError,match="month"):
        validate_rows([bad],"observation")
