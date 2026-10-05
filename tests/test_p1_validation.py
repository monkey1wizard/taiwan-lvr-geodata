"""Adversarial failures beyond the happy-path conversion fixture."""
import copy
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lvr_pipeline.ingest import ingest
from lvr_pipeline.normalize import normalize, normalize_record
from lvr_pipeline.parquet_io import BatchWriter, inspect_parquet, verify_relations
from lvr_pipeline.processing import load_snapshot
from lvr_pipeline.snapshots import SnapshotStore
from test_p1_conversion import CODE, make_source, pipeline, rules, dataset


def test_duplicate_header_is_rejected_as_invalid_input(tmp_path):
    raw,manifest=make_source(tmp_path,[{"extra":["extra"]}],extra_header=["new_field"])
    with pytest.raises(ValueError,match="Duplicate/empty"):
        ingest(raw,manifest,["115q1"],tmp_path/"work",code_commit=CODE)
    assert not (tmp_path/"work/ingested/current.json").exists()


@pytest.mark.parametrize("value",["1.234","1.00000000000000000000000000000001","NaN","Infinity","-1","1,0",str(2**63)])
def test_nonexact_or_unsupported_amount_is_not_rounded(tmp_path,value):
    raw,manifest=make_source(tmp_path,[{"amount":value}],categories=("sales",))
    ingested=ingest(raw,manifest,["115q1"],tmp_path/"work",code_commit=CODE)
    record=dataset(ingested,"ingest-record")[0]
    normalized=normalize_record(record,202610,{},[])
    assert normalized["observation"][0]["amount_minor"] is None
    assert normalized["diagnostic"][0]["code"]=="invalid_amount"


def test_conflicting_money_aliases_do_not_choose_a_column(tmp_path):
    raw,manifest=make_source(tmp_path,[{"extra":["999"]}],extra_header=["房地總價元"],categories=("presale",))
    ingested=ingest(raw,manifest,["115q1"],tmp_path/"work",code_commit=CODE)
    output=normalize_record(dataset(ingested,"ingest-record")[0],202610,{},[])
    assert output["observation"][0]["amount_minor"] is None
    assert output["diagnostic"][0]["code"]=="invalid_amount"


def test_schema_types_cannot_change_even_if_values_cast(tmp_path):
    raw,manifest=make_source(tmp_path,categories=("sales",))
    snapshot=ingest(raw,manifest,["115q1"],tmp_path/"work",code_commit=CODE)
    path=snapshot/"records/115q1/sales.parquet"
    table=pq.read_table(path)
    field=pa.field("source_row_number",pa.string(),nullable=False)
    pq.write_table(table.set_column(table.schema.get_field_index(field.name),field,pa.array(["3"])),path)
    with pytest.raises(ValueError,match="schema/metadata"):
        inspect_parquet(path,"ingest-record")


def test_duplicate_ids_across_partitions_rejected(tmp_path):
    ingested,_,_=pipeline(tmp_path,categories=("sales",))
    path=ingested/"records/115q1/sales.parquet"
    with pytest.raises(ValueError,match="Duplicate"):
        verify_relations({"ingest-record":[path,path]})


def test_dispositions_cannot_reclassify_retained_observations(tmp_path):
    _,_,converted=pipeline(tmp_path,categories=("sales",))
    manifest=json.loads((converted/"manifest.json").read_text(encoding="utf-8"))
    groups={}
    for item in manifest["artifacts"]:
        if item["schema"]:
            groups.setdefault(item["schema"],[]).append(converted/item["path"])
    path=groups["disposition"][0]
    table=pq.read_table(path)
    table=table.set_column(table.schema.get_field_index("outcome"),table.schema.field("outcome"),pa.array(["excluded"]))
    pq.write_table(table,path)
    with pytest.raises(ValueError,match="outcome"):
        verify_relations(groups)


def test_changed_cutoff_and_manual_rules_create_distinct_snapshots(tmp_path):
    raw,manifest=make_source(tmp_path)
    work=tmp_path/"work"
    ingested=ingest(raw,manifest,["115q1"],work,code_commit=CODE)
    path=rules(tmp_path)
    before=normalize(ingested,work,cutoff=202610,rules_path=path,code_commit=CODE)
    later=normalize(ingested,work,cutoff=202512,rules_path=path,code_commit=CODE)
    assert before!=later and load_snapshot(later,"normalize")[1]["retained_rows"]==0
    path.write_text(path.read_text(encoding="utf-8")+"台,臺,variant,\n",encoding="utf-8")
    patched=normalize(ingested,work,cutoff=202610,rules_path=path,code_commit=CODE)
    assert patched not in {before,later}


def test_missing_rules_and_bad_cutoff_do_not_create_completed_state(tmp_path):
    raw,manifest=make_source(tmp_path)
    work=tmp_path/"work"
    ingested=ingest(raw,manifest,["115q1"],work,code_commit=CODE)
    with pytest.raises(FileNotFoundError):
        normalize(ingested,work,cutoff=202610,rules_path=tmp_path/"missing.csv",code_commit=CODE)
    with pytest.raises(ValueError,match="cutoff"):
        normalize(ingested,work,cutoff=202613,rules_path=rules(tmp_path),code_commit=CODE)
    assert not (work/"normalized/current.json").exists()


def test_arrow_rows_and_bytes_both_bound_buffers(tmp_path):
    writer=BatchWriter(tmp_path/"diagnostics.parquet","diagnostic",1000,max_bytes=3000)
    for index in range(8):
        writer.add({"raw_record_id":"a"*64,"code":"test","detail":"x"*1200,"source_ref":"test"})
    writer.close()
    assert writer.max_buffer_bytes<=3000 and writer.max_buffer_rows<=2
    assert pq.ParquetFile(writer.path).metadata.num_rows==8
    too_large=BatchWriter(tmp_path/"large.parquet","diagnostic",max_bytes=1000)
    try:
        with pytest.raises(ValueError,match="byte budget"):
            too_large.add({"raw_record_id":"a"*64,"code":"test","detail":"x"*1200,"source_ref":"test"})
    finally:
        too_large.close()


def test_disk_preflight_refuses_to_publish_insufficient_scope(tmp_path,monkeypatch):
    import lvr_pipeline.ingest as module
    raw,manifest=make_source(tmp_path,categories=("sales",))
    real=module.shutil.disk_usage(tmp_path)
    monkeypatch.setattr(module.shutil,"disk_usage",lambda _:real._replace(free=1024))
    with pytest.raises(ValueError,match="20% headroom"):
        ingest(raw,manifest,["115q1"],tmp_path/"work",code_commit=CODE)
    assert not (tmp_path/"work/ingested/current.json").exists()


def test_arrow_contract_descriptor_matches_published_schema():
    from lvr_pipeline.parquet_io import SCHEMAS
    root=Path(__file__).resolve().parents[1]
    document=json.loads((root/"schemas/converted-parquet.json").read_text(encoding="utf-8"))
    for name,schema in SCHEMAS.items():
        assert document["datasets"][name]["fields"]==[
            {"name":field.name,"type":str(field.type),"nullable":field.nullable} for field in schema]


def test_declared_input_scope_and_cutoff_cannot_disagree_with_rows(tmp_path):
    _,_,converted=pipeline(tmp_path,categories=("sales",))
    manifest=json.loads((converted/"manifest.json").read_text(encoding="utf-8"))
    groups={}
    for item in manifest["artifacts"]:
        if item["schema"]:
            groups.setdefault(item["schema"],[]).append(converted/item["path"])
    with pytest.raises(ValueError,match="source scope"):
        verify_relations(groups,source_scope={"115q1":"f"*64})
    with pytest.raises(ValueError,match="cutoff"):
        verify_relations(groups,cutoff=202512)
