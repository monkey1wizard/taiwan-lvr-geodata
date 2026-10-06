"""P1 acceptance cases use generated synthetic data only."""
from __future__ import annotations

import copy
import csv
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lvr_pipeline.converted import export_converted
from lvr_pipeline.contracts import component_id
from lvr_pipeline.ingest import ingest
from lvr_pipeline.normalize import normalize
from lvr_pipeline.parquet_io import BatchWriter, SCHEMAS, rows
from lvr_pipeline.processing import load_snapshot
from lvr_pipeline.snapshots import SnapshotStore
from lvr_pipeline.sources import describe_zip, sha256_file

CODE="1"*40
ADDRESS="臺北市中正區測試路10號之1"


def make_source(tmp_path, records=None, *, count=None, batch="115q1", categories=("sales", "presale", "rent"), english=True, extra_header=None):
    raw=tmp_path/"raw"
    raw.mkdir(exist_ok=True)
    path=raw/f"{batch}_lvr_landcsv.zip"
    with zipfile.ZipFile(path,"w",compression=zipfile.ZIP_DEFLATED) as archive:
        for category,suffix in [("sales","a"),("presale","b"),("rent","c")]:
            if category not in categories:
                continue
            date="租賃年月日" if category=="rent" else "交易年月日"
            price="總額元" if category=="rent" else "總價元"
            area="建物總面積平方公尺" if category=="rent" else "建物移轉總面積平方公尺"
            header=["土地位置建物門牌","交易標的",date,price,area,"編號","new_field"]
            if extra_header:
                header+=extra_header
            # Write directly to the ZIP to keep the scaling fixture itself bounded.
            with archive.open(f"a_lvr_land_{suffix}.csv","w") as binary:
                text=io.TextIOWrapper(binary,encoding="utf-8-sig",newline="")
                writer=csv.writer(text)
                writer.writerow(header)
                if english:
                    writer.writerow(["Address","Target","TransactionDate","Amount","Area","ID","New"]+["New"]*len(extra_header or []))
                iterable=(records if records is not None else ({} for _ in range(count or 1)))
                for record in iterable:
                    value={"address":ADDRESS,"target":"房地(土地+建物)","date":"1150102","amount":"1234.56","area":"10.25","serial":"same","unknown":"keep",**record}
                    row=[value[key] for key in ["address","target","date","amount","area","serial","unknown"]]
                    writer.writerow(row+record.get("extra",[]))
                text.flush()
    return raw,{"inputs":[describe_zip(path)]}


def rules(tmp_path):
    path=tmp_path/"rules.csv"
    path.write_text("garbled,correct,kind,scope\nU+E000,試,char,\n",encoding="utf-8")
    return path


def pipeline(tmp_path,records=None,**kwargs):
    raw,manifest=make_source(tmp_path,records,**kwargs)
    work=tmp_path/"work"
    ingested=ingest(raw,manifest,["115q1"],work,batch_rows=2,code_commit=CODE)
    normalized=normalize(ingested,work,cutoff=202610,rules_path=rules(tmp_path),batch_rows=2,code_commit=CODE)
    converted=export_converted(normalized,work,code_commit=CODE)
    return ingested,normalized,converted


def dataset(snapshot,name):
    manifest=json.loads((snapshot/"manifest.json").read_text(encoding="utf-8"))
    return [row for artifact in manifest["artifacts"] if artifact["schema"]==name for row in rows(snapshot/artifact["path"])]


def test_three_categories_originals_money_identity_and_complete_contracts(tmp_path):
    ingested,normalized,converted=pipeline(tmp_path)
    observations=dataset(converted,"observation")
    assert {row["category"] for row in observations}=={"sales","presale","rent"}
    assert len({row["raw_record_id"] for row in observations})==3
    assert all(row["transaction_key"] is None and row["source_serial"]=="same" for row in observations)
    assert all(row["source_row_number"]==3 and row["amount_minor"]==123456 and row["area_m2_decimal"]=="10.25" for row in observations)
    assert all(json.loads(row["props_json"])["new_field"]=="keep" for row in observations)
    assert all(row["raw_address"]==ADDRESS for row in observations)
    _,report=load_snapshot(converted,"converted")
    assert report["input_rows"]==report["retained_rows"]==3
    assert report["amount_minor_sum"]==370368 and report["internal_only"]
    assert not report["consumer_output"] and not report["geocoded"] and not report["tgos_started"]
    assert report["max_buffer_rows"]<=2
    assert load_snapshot(ingested,"ingest")[1]["english_rows"]==3
    assert any("new_field" in row["unmapped_columns"] for row in load_snapshot(ingested,"ingest")[1]["members"])


def test_first_actual_transaction_without_english_row_is_not_dropped(tmp_path):
    _,_,converted=pipeline(tmp_path,english=False)
    assert len(dataset(converted,"observation"))==3
    assert all(row["source_row_number"]==2 for row in dataset(converted,"observation"))


def test_address_marker_alone_does_not_drop_a_transaction(tmp_path):
    _,_,converted=pipeline(tmp_path,[{"address":"Address"}],categories=("sales",))
    report=load_snapshot(converted,"converted")[1]
    assert report["input_rows"]==1 and report["excluded_rows"]==1
    assert dataset(converted,"disposition")[0]["reason"]=="no_doorplate"


def test_source_dispositions_and_diagnostics_are_not_double_counted(tmp_path):
    records=[{}, {"target":"土地"},{"target":"車位"},{"address":"臺北市中正區測試段123地號"},
             {"address":"沒有門牌"},{"date":"1140230"},{"date":"1151101"},
             {"address":"臺北市中正區測試?路10號"},{"amount":"bad"},{"area":"bad"},
             {"amount":"0","area":"0"},{"amount":"","area":"","serial":""}]
    _,_,converted=pipeline(tmp_path,records,categories=("sales",))
    report=load_snapshot(converted,"converted")[1]
    assert report["input_rows"]==12 and report["retained_rows"]==6 and report["excluded_rows"]==6 and report["failed_rows"]==0
    assert len(dataset(converted,"disposition"))==12
    obs=dataset(converted,"observation")
    assert obs[-2]["amount_minor"]==0 and obs[-2]["area_m2_decimal"]=="0"
    assert obs[-1]["amount_minor"] is None and obs[-1]["source_serial"] is None
    assert len({row["raw_record_id"] for row in obs})==6
    components=dataset(converted,"address-component")
    assert components[1]["building_key"] is None
    assert {row["code"] for row in dataset(converted,"diagnostic")} >= {"invalid_date","invalid_amount","invalid_area","unresolved_garbled_address"}


def test_explicit_lists_subdoors_patch_and_ambiguous_ranges(tmp_path):
    records=[{"address":"臺北市中正區測試路10、12號"},
             {"address":"臺北市中正區測試路10號之1、10號之2"},
             {"address":"臺北市中正區測試路10-12號"},
             {"address":"臺北市中正區測\ue000路10號"},
             {"address":"金門縣金城鎮甲里10號"},
             {"address":"金門縣金城鎮乙里10號"},
             {"address":"臺北市中正區測試路 10 號之 1 二樓"}]
    _,_,converted=pipeline(tmp_path,records,categories=("sales",))
    obs=dataset(converted,"observation")
    components=dataset(converted,"address-component")
    assert len(obs)==7 and len(components)==9
    assert sum(row["amount_minor"] for row in obs)==123456*7
    assert components[2]["building_key"] != components[3]["building_key"]
    assert components[4]["building_key"] is None and components[4]["expansion_status"]=="review"
    assert components[5]["normalized_address"]=="臺北市中正區測試路10號"
    assert components[6]["building_key"] != components[7]["building_key"]
    assert components[8]["building_key"]==components[2]["building_key"]


def test_review_component_keeps_no_key_even_if_whole_text_has_one(tmp_path):
    address="臺北市中正區測試路80十樓，測試三街232號"
    from lvr_pipeline.address import building_key_v2
    from lvr_pipeline.normalize import normalize_address
    assert building_key_v2(normalize_address(address)) is not None
    _,_,converted=pipeline(tmp_path,[{"address":address}],categories=("sales",))
    component=dataset(converted,"address-component")[0]
    assert component["expansion_status"]=="review" and component["building_key"] is None


def test_malformed_column_count_is_retained_as_failed_source_line(tmp_path):
    _,_,converted=pipeline(tmp_path,[{"extra":["too many"]},{}],categories=("sales",))
    report=load_snapshot(converted,"converted")[1]
    assert report["input_rows"]==2 and report["failed_rows"]==1 and report["retained_rows"]==1
    failed=dataset(converted,"disposition")[0]
    assert json.loads(failed["raw_values_json"])[-1]=="too many"
    assert failed["outcome"]=="failed" and failed["row_status"]=="failed"
    assert dataset(converted,"diagnostic")[0]["code"]=="csv_column_count"


def test_multiline_csv_keeps_physical_line_provenance(tmp_path):
    _,_,converted=pipeline(tmp_path,[{"unknown":"first\nsecond"},{}],categories=("sales",))
    obs=dataset(converted,"observation")
    assert [row["source_row_number"] for row in obs]==[3,5]
    assert json.loads(obs[0]["props_json"])["new_field"]=="first\nsecond"
    assert [row["source_line_end"] for row in dataset(converted,"disposition")]==[4,5]


def test_unclosed_quote_fails_only_its_line_and_keeps_later_rows(tmp_path):
    raw,_=make_source(tmp_path,[{},{"unknown":"MARK"},{},{}],categories=("sales",))
    path=raw/"115q1_lvr_landcsv.zip"
    with zipfile.ZipFile(path) as archive:
        text=archive.read("a_lvr_land_a.csv").decode("utf-8-sig").replace("MARK",'"6號')
    with zipfile.ZipFile(path,"w") as archive:
        archive.writestr("a_lvr_land_a.csv",codecs_bom(text))
    manifest={"inputs":[describe_zip(path)]}
    work=tmp_path/"work"
    ingested=ingest(raw,manifest,["115q1"],work,code_commit=CODE)
    report=load_snapshot(ingested,"ingest")[1]
    assert report["line_mode_members"]==[{"batch":"115q1","path":"a_lvr_land_a.csv"}]
    assert report["input_rows"]==4 and report["parse_failed_rows"]==1
    records=dataset(ingested,"ingest-record")
    failed=[row for row in records if row["row_status"]=="failed"]
    assert [row["source_row_number"] for row in failed]==[4]
    assert json.loads(failed[0]["raw_values_json"])[0].endswith('"6號')
    assert [row["source_row_number"] for row in records if row["row_status"]=="parsed"]==[3,5,6]


def codecs_bom(text):
    return "﻿".encode("utf-8")+text.encode("utf-8")


def test_known_empty_batch_is_a_zero_scope_not_missing_input(tmp_path):
    raw=tmp_path/"raw"
    raw.mkdir()
    path=raw/"101q1_lvr_landcsv.zip"
    with zipfile.ZipFile(path,"w") as archive:
        archive.writestr("manifest.csv","fixture\n")
        archive.writestr("build.ttt","fixture")
    manifest={"inputs":[describe_zip(path)]}
    work=tmp_path/"work"
    ingested=ingest(raw,manifest,["101q1"],work,code_commit=CODE)
    normalized=normalize(ingested,work,cutoff=202610,rules_path=rules(tmp_path),code_commit=CODE)
    converted=export_converted(normalized,work,code_commit=CODE)
    report=load_snapshot(converted,"converted")[1]
    assert report["known_empty_batches"]==["101q1"] and report["input_rows"]==0
    assert all(value==0 for value in report["dataset_counts"].values())
    with pytest.raises(ValueError,match="Missing manifest"):
        ingest(raw,manifest,["102q1"],work,code_commit=CODE)


def test_inputs_changed_or_duplicate_manifest_cannot_complete(tmp_path):
    raw,manifest=make_source(tmp_path)
    entry=manifest["inputs"][0]
    (raw/entry["filename"]).write_bytes(b"bad")
    with pytest.raises(ValueError,match="hash"):
        ingest(raw,manifest,["115q1"],tmp_path/"work",code_commit=CODE)
    assert not (tmp_path/"work/ingested/current.json").exists()
    duplicated={"inputs":[entry,copy.deepcopy(entry)]}
    with pytest.raises(ValueError,match="Duplicate manifest"):
        ingest(raw,duplicated,["115q1"],tmp_path/"work",code_commit=CODE)


def test_interruption_keeps_previous_pointer_and_rerun_uses_new_id(tmp_path,monkeypatch):
    raw,manifest=make_source(tmp_path)
    work=tmp_path/"work"
    first=ingest(raw,manifest,["115q1"],work,run_id="before",code_commit=CODE)
    original=BatchWriter.add
    def fail(self,row):
        raise OSError("simulated input interruption")
    monkeypatch.setattr(BatchWriter,"add",fail)
    with pytest.raises(OSError):
        ingest(raw,manifest,["115q1"],work,run_id="broken",code_commit=CODE)
    assert SnapshotStore(work/"ingested").current()["snapshot_id"]=="before"
    assert not (work/"ingested/snapshots/broken").exists()
    monkeypatch.setattr(BatchWriter,"add",original)
    second=ingest(raw,manifest,["115q1"],work,run_id="after",code_commit=CODE)
    assert SnapshotStore(work/"ingested").current()["snapshot_id"]=="after"
    assert first.exists() and second.exists()


def test_verified_reuse_is_bound_to_code_and_settings(tmp_path):
    raw,manifest=make_source(tmp_path)
    work=tmp_path/"work"
    first=ingest(raw,manifest,["115q1"],work,code_commit=CODE)
    assert ingest(raw,manifest,["115q1"],work,code_commit=CODE)==first
    changed=ingest(raw,manifest,["115q1"],work,batch_rows=2,code_commit=CODE)
    assert changed!=first
    with pytest.raises(ValueError,match="bindings"):
        ingest(raw,manifest,["115q1"],work,run_id=first.name,batch_rows=2,code_commit=CODE)


def test_arrow_schema_and_cross_partition_relations_are_checked(tmp_path):
    _,_,converted=pipeline(tmp_path)
    manifest=json.loads((converted/"manifest.json").read_text(encoding="utf-8"))
    item=next(a for a in manifest["artifacts"] if a["schema"]=="address-component")
    path=converted/item["path"]
    table=pq.read_table(path)
    changed=table.set_column(0,table.schema.field(0),pa.array(["f"*64]))
    changed=changed.set_column(changed.schema.get_field_index("component_id"),changed.schema.field("component_id"),pa.array([component_id("f"*64,0)]))
    # Rewrite a valid typed file and update its hash: semantics must still reject it.
    pq.write_table(changed,path)
    item["sha256"]=sha256_file(path)
    item["size_bytes"]=path.stat().st_size
    (converted/"manifest.json").write_text(json.dumps(manifest),encoding="utf-8")
    with pytest.raises(ValueError):
        SnapshotStore(converted.parent.parent).verify(converted)


def test_cli_runs_and_verifies_only_internal_output(tmp_path):
    raw,manifest=make_source(tmp_path)
    manifest_path=tmp_path/"manifest.json"
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False),encoding="utf-8")
    command=[sys.executable,"-m","lvr_pipeline","export-converted","--manifest",str(manifest_path),"--raw-dir",str(raw),"--batch","115q1","--cutoff","202610","--garbled-rules",str(rules(tmp_path)),"--work-dir",str(tmp_path/"work"),"--batch-rows","2"]
    completed=subprocess.run(command,capture_output=True,text=True,check=True)
    payload=json.loads(completed.stdout)
    assert payload["internal_only"] and payload["process_peak_rss_bytes"]>0
    check=subprocess.run([sys.executable,"-m","lvr_pipeline","verify-converted","--input",payload["snapshot"]],capture_output=True,text=True,check=True)
    assert json.loads(check.stdout)["verified"]


def test_exporting_existing_normalized_snapshot_cannot_ignore_cutoff(tmp_path):
    _,normalized,_=pipeline(tmp_path,categories=("sales",))
    completed=subprocess.run([sys.executable,"-m","lvr_pipeline","export-converted","--input",str(normalized),
        "--cutoff","202512","--work-dir",str(tmp_path/"fresh")],capture_output=True,text=True)
    assert completed.returncode==1 and "Cutoff differs" in json.loads(completed.stdout)["error"]
    assert not (tmp_path/"fresh/converted/current.json").exists()
