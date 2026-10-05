"""Synthetic end-to-end foundation smoke; not a P1 ingest implementation."""
import csv
import io
import json
from pathlib import Path
import zipfile

from lvr_pipeline.address import building_key, building_key_v2, norm
from lvr_pipeline.contracts import component_id, observation_id, validate_relations
from lvr_pipeline.snapshots import SnapshotStore
from lvr_pipeline.sources import CATEGORIES, TRANSACTION_MEMBER, describe_zip, verify_raw
from lvr_pipeline.tx_date import validated_roc_to_tx_yyyymm


def test_synthetic_source_to_committed_typed_rows(tmp_path):
    path=Path(__file__).parent/"fixtures/p0/115q1_lvr_landcsv.zip"
    manifest=describe_zip(path)
    verify_raw(path,manifest)
    observations,components=[],[]
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            match=TRANSACTION_MEMBER.fullmatch(name)
            if not match:
                continue
            with archive.open(name) as binary:
                rows=list(csv.reader(io.TextIOWrapper(binary,encoding="utf-8-sig",newline="")))
            # This fixture explicitly defines physical row 2 as an English description.
            address, date_raw, amount, unknown=rows[2]
            raw_id=observation_id(manifest["sha256"],name,3)
            observations.append({"schema_version":"1.0","category":CATEGORIES[match[1]],"src_batch":"115q1","source_serial":None,
                "input_sha256":manifest["sha256"],"member_path":name,"source_row_number":3,"raw_record_id":raw_id,
                "record_grain":"source_observation","transaction_key":None,"raw_address":address,"tx_date_raw":date_raw,
                "tx_yyyymm":validated_roc_to_tx_yyyymm(date_raw,run_cutoff_yyyymm=202610),"run_cutoff_yyyymm":202610,
                "currency":"TWD","amount_scale":100,"amount_minor":int(amount)*100,"area_m2_decimal":None,"props_json":{"synthetic_unknown":unknown},"parse_status":"retained"})
            normalized=norm(address)
            components.append({"raw_record_id":raw_id,"component_id":component_id(raw_id,0),"ordinal":0,
                "normalized_address":normalized,"key_version":"v2","building_key":building_key_v2(normalized),
                "legacy_building_key":building_key(address),"expansion_status":"confirmed"})
    validate_relations(observations,components,[])
    assert len(observations)==3 and sum(row["amount_minor"] for row in observations)==30000000
    assert len({row["building_key"] for row in components})==1
    store=SnapshotStore(tmp_path/"store")
    bindings={"code_commit":"0"*40,"schema_version":"1.0","config_sha256":"a"*64,"input_sha256":[manifest["sha256"]]}
    store.begin("synthetic",expected_parent=None,bindings=bindings)
    for name, rows in [("observation",observations),("address-component",components)]:
        source=tmp_path/(name+".jsonl")
        source.write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows),encoding="utf-8")
        store.add("synthetic",source.name,source,format_name="jsonl",row_count=3,schema=name)
    store.publish("synthetic",expected_parent=None)
    assert store.current()["snapshot_id"]=="synthetic"


def test_address_fixture_retains_code_strings():
    with (Path(__file__).parent/"fixtures/p0/addresses.csv").open(encoding="utf-8",newline="") as stream:
        row=next(csv.DictReader(stream))
    assert len(row)==14 and row["COUNTY"]=="09020" and row["TOWN"]=="09020010"
