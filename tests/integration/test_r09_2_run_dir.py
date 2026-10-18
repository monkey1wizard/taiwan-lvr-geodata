"""R09-2c: TGOS and build-review commands write into data/runs/<run-id>, never into a run-full run."""

import json
import shutil
from pathlib import Path

import pytest

from lvr_pipeline.cli import build_parser, main
from lvr_pipeline.storage.runs import SnapshotStore
from test_p2_offline import index_of
from test_p3_tgos import fixture_state, fixture_two_unmatched

COMMANDS = ["prepare-tgos", "repair-tgos-exchange", "set-tgos-status", "import-tgos", "build-review"]


def _date_file(tmp_path):
    path = tmp_path / "date.json"
    path.write_text('{"date":"2026-10-17"}', encoding="utf-8")
    return path


def _in_runs(state, tmp_path):
    """The offline state beside the TGOS run under the runs root, where its ledger records it (contract rule 3)."""
    run = state.parent.parent.parent
    target = tmp_path / "runs" / run.name
    if not target.exists():
        for stage in ("offline-state", "address-pool"):  # the pool tells the state's address rule version
            shutil.copytree(run / stage, target / stage)
    return target / "offline-state" / "snapshots" / state.name


def _prepare_argv(state, tmp_path, run_id, *extra):
    state = state if "tgos-state" in state.parts else _in_runs(state, tmp_path)
    return ["prepare-tgos", "--state", str(state), "--index", str(index_of(state)), "--date-file", str(_date_file(tmp_path)),
            "--exchange-dir", str(tmp_path / "exchange"), "--runs-root", str(tmp_path / "runs"),
            "--run-id", run_id, *extra]


def _argv(command, tmp_path, converted, state, run_id):
    common = ["--runs-root", str(tmp_path / "runs"), "--run-id", run_id]
    date = ["--date-file", str(_date_file(tmp_path)), "--exchange-dir", str(tmp_path / "exchange")]
    return {
        "prepare-tgos": ["prepare-tgos", "--state", str(state), "--index", str(tmp_path / "index"), *date, *common],
        "repair-tgos-exchange": ["repair-tgos-exchange", "--state", str(state), "--batch", "batch", *date, *common],
        "set-tgos-status": ["set-tgos-status", "--state", str(state), "--batch", "batch",
                            "--status", "cancelled", *common],
        "import-tgos": ["import-tgos", "--state", str(state), "--batch", "batch",
                        "--response", str(tmp_path / "response.csv"), "--index", str(tmp_path / "index"), *common],
        "build-review": ["build-review", "--input", str(converted), "--state", str(state), *common],
    }[command]


def test_prepare_writes_snapshot_under_run_directory(tmp_path, capsys):
    _, state, _ = fixture_state(tmp_path)
    assert main(_prepare_argv(state, tmp_path, "tgos-a")) == 0
    path = Path(json.loads(capsys.readouterr().out)["path"])
    run_dir = tmp_path / "runs" / "tgos-a"
    assert path.parent == run_dir / "tgos-state" / "snapshots"
    assert SnapshotStore(run_dir / "tgos-state").current()["snapshot_id"] == path.name


def test_chained_prepare_under_one_run_id_makes_distinct_snapshots(tmp_path, capsys):
    _, state, _ = fixture_two_unmatched(tmp_path)
    assert main(_prepare_argv(state, tmp_path, "tgos-b", "--limit", "1")) == 0
    first = Path(json.loads(capsys.readouterr().out)["path"])
    assert main(_prepare_argv(first, tmp_path, "tgos-b", "--limit", "1")) == 0
    second = Path(json.loads(capsys.readouterr().out)["path"])
    assert first.name != second.name
    store = SnapshotStore(tmp_path / "runs" / "tgos-b" / "tgos-state")
    assert store.current()["snapshot_id"] == second.name


@pytest.mark.parametrize("command", COMMANDS)
def test_run_full_directory_is_refused_and_untouched(tmp_path, capsys, command):
    converted, state, _ = fixture_state(tmp_path)
    run_dir = tmp_path / "runs" / "full-owned"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({"run_id": "full-owned"}), encoding="utf-8")
    assert main(_argv(command, tmp_path, converted, state, "full-owned")) == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error.startswith("Run directory belongs to a run-full run: ")
    assert sorted(item.name for item in run_dir.iterdir()) == ["manifest.json"]
    assert not (tmp_path / "exchange").exists()


@pytest.mark.parametrize("command", COMMANDS)
def test_work_dir_option_is_rejected(tmp_path, capsys, command):
    argv = [*_argv(command, tmp_path, tmp_path, tmp_path, "x"), "--work-dir", str(tmp_path / "work")]
    with pytest.raises(SystemExit):
        main(argv)
    assert "unrecognized arguments: --work-dir" in capsys.readouterr().err


@pytest.mark.parametrize("command", COMMANDS)
def test_run_id_is_required(tmp_path, capsys, command):
    argv = _argv(command, tmp_path, tmp_path, tmp_path, "x")
    index = argv.index("--run-id")
    del argv[index:index + 2]
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)
    assert "the following arguments are required: --run-id" in capsys.readouterr().err
