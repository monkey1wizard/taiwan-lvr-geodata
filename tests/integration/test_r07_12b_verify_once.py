"""R07-12b: rows are validated once, when a snapshot is built; later reads check hashes only.

Synthetic inputs only. One counted run-full is shared by the tests; each test copies it.
"""
import json
import shutil
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "acceptance"))

from lvr_pipeline.addresses import admin  # noqa: E402
from lvr_pipeline.cli import main  # noqa: E402
from lvr_pipeline.pipeline import run_full  # noqa: E402
from lvr_pipeline.storage import runs  # noqa: E402
from lvr_pipeline.storage.runs import RunStore, SnapshotStore, load_snapshot  # noqa: E402
from test_run_full import checkpoints, setup_inputs  # noqa: E402


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("verify_once")
    # setup_inputs points the town table at a synthetic one; the per-test restore in conftest
    # runs after this module fixture, so put the committed file back here.
    original_towns = admin.ADMIN_TOWNS_PATH
    inputs = setup_inputs(tmp)
    calls = []
    original = runs._inspect

    def counting(path, format_name, schema):
        calls.append((Path(path), format_name, schema))
        return original(path, format_name, schema)

    patch = pytest.MonkeyPatch()
    patch.setattr(runs, "_inspect", counting)
    try:
        result = run_full(run_id="once", **inputs)
    finally:
        patch.undo()
        admin.ADMIN_TOWNS_PATH = original_towns
        admin.load_admin_towns.cache_clear()
    assert result["completed"] is True
    run_dir = inputs["runs_root"] / "once"
    return {"run_dir": run_dir, "calls": calls, "tmp": tmp}


@pytest.fixture
def copy_of_run(built, tmp_path):
    target = tmp_path / "once"
    shutil.copytree(built["run_dir"], target)
    return target


def snapshot_dir(run_dir, stage):
    return run_dir / stage / "snapshots" / checkpoints(run_dir)[stage]["snapshot_id"]


def parquet_files(directory, *, schema_only=True):
    """Relative paths of the Parquet artifacts that the manifest lists with a row schema."""
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    return sorted(item["path"] for item in manifest["artifacts"]
                  if item["format"] == "parquet" and (item["schema"] or not schema_only))


def test_each_parquet_artifact_is_inspected_exactly_once(built):
    run_dir = built["run_dir"]
    seen = {}
    for path, format_name, schema in built["calls"]:
        if format_name != "parquet" or not schema:
            continue
        relative = path.relative_to(run_dir)
        key = (relative.parts[0], "/".join(relative.parts[3:]))  # the stage and the artifact path
        seen[key] = seen.get(key, 0) + 1
    expected = {(stage, file) for stage in checkpoints(run_dir) if (run_dir / stage / "snapshots").is_dir()
                for file in parquet_files(snapshot_dir(run_dir, stage))}
    assert expected and set(seen) == expected
    assert {key: count for key, count in seen.items() if count != 1} == {}


def test_tampered_parquet_is_not_loaded_or_reused(copy_of_run):
    bindings = json.loads((copy_of_run / "manifest.json").read_text(encoding="utf-8"))["bindings"]
    run = RunStore(copy_of_run.parent, copy_of_run.name, bindings)
    directory = snapshot_dir(copy_of_run, "converted")
    assert run.reusable("converted", bindings) is True
    load_snapshot(directory, "converted")
    target = directory / parquet_files(directory)[0]
    data = bytearray(target.read_bytes())
    data[len(data) // 2] ^= 0xFF
    target.write_bytes(bytes(data))
    with pytest.raises(ValueError, match="hash"):
        load_snapshot(directory, "converted")
    assert run.reusable("converted", bindings) is False


def tamper_with_valid_parquet(run_dir):
    """Replace an ingest-record artifact by a valid Parquet file that breaks the row contract,
    and rewrite the manifest size and SHA-256 so only row validation can notice."""
    directory = snapshot_dir(run_dir, "ingested")
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    item = next(entry for entry in manifest["artifacts"] if entry["schema"] == "ingest-record" and entry["row_count"])
    target = directory / item["path"]
    table = pq.read_table(target)
    index = table.schema.get_field_index("row_status")
    bogus = pa.array(["bogus"] * table.num_rows, type=table.schema.field(index).type)
    table = table.set_column(index, table.schema.field(index), bogus)
    pq.write_table(table, target)
    item["size_bytes"] = target.stat().st_size
    item["sha256"] = runs.sha256_file(target)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return directory


def test_verify_snapshot_command_passes_a_good_snapshot(copy_of_run, capsys):
    directory = snapshot_dir(copy_of_run, "ingested")
    assert main(["verify-snapshot", "--path", str(directory)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"] is True and summary["snapshot_id"] == directory.name
    assert summary["artifacts"] >= 1 and summary["row_counts"]["ingest-record"] > 0


def test_verify_snapshot_command_catches_what_integrity_cannot(copy_of_run, capsys):
    directory = tamper_with_valid_parquet(copy_of_run)
    store = SnapshotStore(directory.parent.parent)
    assert store.verify_integrity(directory)["snapshot_id"] == directory.name  # hashes were rewritten to match
    with pytest.raises(ValueError, match="lineage/status"):
        store.verify_full(directory)
    assert main(["verify-snapshot", "--path", str(directory)]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["completed"] is False and "lineage/status" in output["error"]
