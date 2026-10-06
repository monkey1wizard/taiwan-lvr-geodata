import copy
import json
import os
from pathlib import Path

import pytest

from lvr_pipeline.storage.runs import SnapshotStore
from test_p0_contracts import observation, component

BINDINGS={"code_commit":"9fec324"+"0"*33,"schema_version":"1.0","config_sha256":"a"*64,"input_sha256":["b"*64]}


def prepare(store, tmp_path, name, parent=None):
    store.begin(name,expected_parent=parent,bindings=BINDINGS)
    record=observation()
    for filename,schema,rows in [("observations.jsonl","observation",[record]),("components.jsonl","address-component",[component(record)])]:
        source=tmp_path/(name+filename)
        source.write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows),encoding="utf-8")
        store.add(name,filename,source,format_name="jsonl",row_count=len(rows),schema=schema)


def test_publish_verify_reuse_and_immutable_names(tmp_path):
    store=SnapshotStore(tmp_path/"store")
    prepare(store,tmp_path,"first")
    assert store.current() is None
    pointer=store.publish("first",expected_parent=None)
    assert store.current() == pointer
    assert store.reusable("first",BINDINGS)
    changed=copy.deepcopy(BINDINGS)
    changed["config_sha256"]="c"*64
    assert not store.reusable("first",changed)
    with pytest.raises(FileExistsError):
        store.begin("first",expected_parent=None,bindings=BINDINGS)


def test_incomplete_stage_and_stale_writers_keep_old_pointer(tmp_path):
    store=SnapshotStore(tmp_path/"store")
    prepare(store,tmp_path,"first")
    store.publish("first",expected_parent=None)
    prepare(store,tmp_path,"next","first")
    prepare(store,tmp_path,"stale","first")
    assert store.current()["snapshot_id"] == "first"
    store.publish("next",expected_parent="first")
    with pytest.raises(ValueError,match="Stale"):
        store.publish("stale",expected_parent="first")
    assert store.current()["snapshot_id"] == "next"
    assert (store.root/"staging/stale").exists()


def test_failure_before_pointer_switch_can_resume(tmp_path,monkeypatch):
    store=SnapshotStore(tmp_path/"store")
    prepare(store,tmp_path,"first")
    store.publish("first",expected_parent=None)
    prepare(store,tmp_path,"next","first")
    original=os.replace
    def interrupt(source,target):
        if Path(target).name == "current.json":
            raise OSError("simulated interruption")
        return original(source,target)
    monkeypatch.setattr(os,"replace",interrupt)
    with pytest.raises(OSError):
        store.publish("next",expected_parent="first")
    assert store.current()["snapshot_id"] == "first"
    assert (store.root/"snapshots/next").exists()
    monkeypatch.setattr(os,"replace",original)
    store.publish("next",expected_parent="first")
    assert store.current()["snapshot_id"] == "next"


def test_incorrect_hash_and_row_count_refuse_publication(tmp_path):
    store=SnapshotStore(tmp_path/"store")
    prepare(store,tmp_path,"first")
    path=store.root/"staging/first/observations.jsonl"
    path.write_text(path.read_text(encoding="utf-8")+'{}\n',encoding="utf-8")
    with pytest.raises(ValueError,match="hash"):
        store.publish("first",expected_parent=None)
    assert store.current() is None
    store.begin("count",expected_parent=None,bindings=BINDINGS)
    src=tmp_path/"count.jsonl"
    src.write_text('{}\n',encoding="utf-8")
    with pytest.raises(ValueError,match="count"):
        store.add("count","rows.jsonl",src,format_name="jsonl",row_count=2)
    with pytest.raises(ValueError):
        store.publish("count",expected_parent=None)


def test_corrupt_committed_artifact_detected(tmp_path):
    store=SnapshotStore(tmp_path/"store")
    prepare(store,tmp_path,"first")
    store.publish("first",expected_parent=None)
    (store.root/"snapshots/first/components.jsonl").write_bytes(b'{}\n')
    with pytest.raises(ValueError,match="hash"):
        store.current()


def test_orphan_relation_refuses_complete_snapshot(tmp_path):
    store=SnapshotStore(tmp_path/"store")
    store.begin("first",expected_parent=None,bindings=BINDINGS)
    src=tmp_path/"orphan.jsonl"
    src.write_text(json.dumps(component(observation()))+'\n',encoding="utf-8")
    store.add("first","components.jsonl",src,format_name="jsonl",row_count=1,schema="address-component")
    with pytest.raises(ValueError,match="require observations"):
        store.publish("first",expected_parent=None)


@pytest.mark.parametrize("name",["../escape","C:/escape","a\\escape","/absolute","."])
def test_unsafe_ids(tmp_path,name):
    store=SnapshotStore(tmp_path/"store")
    with pytest.raises(ValueError):
        store.begin(name,expected_parent=None,bindings=BINDINGS)


@pytest.mark.parametrize("name",["../escape","C:/escape","a\\escape","/absolute","manifest.json"])
def test_unsafe_artifacts(tmp_path,name):
    store=SnapshotStore(tmp_path/"store")
    store.begin("first",expected_parent=None,bindings=BINDINGS)
    src=tmp_path/"src"
    src.write_bytes(b'data')
    with pytest.raises(ValueError):
        store.add("first",name,src,format_name="binary",row_count=None)


def test_lock_conflict_fails_closed(tmp_path):
    store=SnapshotStore(tmp_path/"store")
    prepare(store,tmp_path,"first")
    (store.root/".publish-lock").mkdir()
    with pytest.raises(RuntimeError,match="lock"):
        store.publish("first",expected_parent=None)
    assert store.current() is None


def test_manifest_row_count_rechecked(tmp_path):
    store=SnapshotStore(tmp_path/"store")
    prepare(store,tmp_path,"first")
    path=store.root/"staging/first/draft.json"
    data=json.loads(path.read_text(encoding="utf-8"))
    data["artifacts"][0]["row_count"]=2
    path.write_text(json.dumps(data),encoding="utf-8")
    with pytest.raises(ValueError,match="count"):
        store.publish("first",expected_parent=None)


def test_replace_waits_out_short_windows_lock(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from lvr_pipeline.storage import runs as snapshots

    calls = []

    def flaky(source, target):
        calls.append(source)
        if len(calls) < 3:
            raise PermissionError("locked")

    monkeypatch.setattr(snapshots, "os", SimpleNamespace(name="nt", replace=flaky))
    monkeypatch.setattr(snapshots.time, "sleep", lambda _: None)
    snapshots._replace(tmp_path / "a", tmp_path / "b")
    assert len(calls) == 3

    monkeypatch.setattr(snapshots, "os", SimpleNamespace(name="posix", replace=flaky))
    calls.clear()
    with pytest.raises(PermissionError):
        snapshots._replace(tmp_path / "a", tmp_path / "b")
    assert len(calls) == 1
