"""R07-12a: the run-time address source check (clean worktree and data-file hashes, not HEAD).

Synthetic address repositories only. A HEAD other than the pinned commit passes and is recorded.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "acceptance"))

import lvr_pipeline.pipeline as pipeline  # noqa: E402
from lvr_pipeline.offline.index import check_pinned_source, load_pinned_source  # noqa: E402
from lvr_pipeline.pipeline import run_full  # noqa: E402
from test_p2_offline import source  # noqa: E402
from test_run_full import checkpoints, setup_inputs  # noqa: E402

STAGES = ["offline-index", "ingested", "normalized", "converted", "address-pool", "offline-state", "tgos-state",
          "review", "final-state", "output", "run-report"]


def git(root, *args):
    return subprocess.check_output(
        ["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "core.hooksPath=/dev/null", *args], text=True).strip()


def commit_all(root, message):
    git(root, "add", ".")
    git(root, "commit", "-m", message)
    return git(root, "rev-parse", "HEAD")


def test_docs_only_commit_passes_and_records_new_head(tmp_path):
    root, descriptor = source(tmp_path)
    first = check_pinned_source(root, descriptor)
    assert first["head_matches"] is True and first["actual_head"] == descriptor["commit"]
    (root / "README.md").write_text("docs only\n", encoding="utf-8")
    head = commit_all(root, "docs")
    checked = check_pinned_source(root, descriptor)
    assert head != descriptor["commit"]
    assert checked["head_matches"] is False and checked["actual_head"] == head
    assert checked["pinned_commit"] == descriptor["commit"] and checked["checked_at"].endswith("+00:00")
    load_pinned_source(root, descriptor)  # the loader goes through the same check


@pytest.mark.parametrize("entry", ["road", "char_fixes", "road_index", "administrative"])
def test_changed_data_file_is_rejected_dirty_and_committed(tmp_path, entry):
    root, descriptor = source(tmp_path)
    relative = {"road": descriptor["roads"][0]["path"], "char_fixes": descriptor["char_fixes"]["path"],
                "road_index": descriptor["road_index"]["path"],
                "administrative": descriptor["administrative_files"][0]["path"]}[entry]
    path = root / relative
    path.write_bytes(path.read_bytes() + b"\n# changed\n")
    with pytest.raises(ValueError, match="clean"):
        check_pinned_source(root, descriptor)
    commit_all(root, "change data")
    with pytest.raises(ValueError, match="differs from pin: " + relative):
        check_pinned_source(root, descriptor)


def test_same_size_change_and_missing_file_name_the_path(tmp_path):
    root, descriptor = source(tmp_path)
    relative = descriptor["roads"][0]["path"]
    path = root / relative
    data = path.read_bytes()
    path.write_bytes(data.replace(b"121.5", b"121.6", 1))  # same size, different hash
    assert len(path.read_bytes()) == len(data) and path.read_bytes() != data
    commit_all(root, "same size")
    with pytest.raises(ValueError, match="differs from pin: " + relative):
        check_pinned_source(root, descriptor)
    path.unlink()
    commit_all(root, "remove")
    with pytest.raises(ValueError, match="missing: " + relative):
        check_pinned_source(root, descriptor)


@pytest.mark.parametrize("kind", ["untracked", "modified"])
def test_dirty_worktree_is_rejected(tmp_path, kind):
    root, descriptor = source(tmp_path)
    if kind == "untracked":
        (root / "extra.txt").write_text("x", encoding="utf-8")
    else:
        (root / "README.md").write_text("tracked\n", encoding="utf-8")
        commit_all(root, "readme")
        (root / "README.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="clean"):
        check_pinned_source(root, descriptor)


def test_hashes_false_checks_only_the_worktree(tmp_path):
    root, descriptor = source(tmp_path)
    path = root / descriptor["roads"][0]["path"]
    path.write_bytes(path.read_bytes() + b"x")
    commit_all(root, "change")
    with pytest.raises(ValueError, match="differs from pin"):
        check_pinned_source(root, descriptor)
    assert check_pinned_source(root, descriptor, hashes=False)["head_matches"] is False


def test_run_full_with_docs_only_commit_passes_every_stage_and_records_head(tmp_path):
    inputs = setup_inputs(tmp_path)
    pinned = json.loads(inputs["address_source"].read_text(encoding="utf-8"))["commit"]
    (inputs["address_dir"] / "README.md").write_text("docs only\n", encoding="utf-8")
    head = commit_all(inputs["address_dir"], "docs")
    result = run_full(run_id="docs", **inputs)
    assert result["completed"] is True, result["failures"]
    report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
    entries = report["address_source_head"]
    assert [e["step"] for e in entries] == STAGES
    assert all(e["actual_head"] == head != pinned and e["head_matches"] is False for e in entries)
    # One full hash check at the first stage entry; later entries see the same HEAD and only check status.
    assert [e["full_check"] for e in entries] == [True] + [False] * 10
    assert all(e["checked_at"] for e in entries)
    assert result["address_source_head"] == entries

    # A resume after another docs-only commit passes; the changed HEAD triggers a new full check.
    (inputs["address_dir"] / "README.md").write_text("docs again\n", encoding="utf-8")
    newer = commit_all(inputs["address_dir"], "docs again")
    again = run_full(run_id="docs", **inputs)
    assert again["completed"] is True, again["failures"]
    assert [e["full_check"] for e in again["address_source_head"]] == [True] + [False] * 10
    assert {e["actual_head"] for e in again["address_source_head"]} == {newer}


def test_matching_head_is_recorded_as_matching(tmp_path):
    inputs = setup_inputs(tmp_path)
    result = run_full(run_id="same", **inputs)
    assert result["completed"] is True, result["failures"]
    assert all(e["head_matches"] is True for e in result["address_source_head"])


def test_road_change_after_a_stage_stops_the_next_stage(tmp_path, monkeypatch):
    """Mechanism: wrap the normalize build so that, once it has run, a road file is changed and
    committed in the address repository. The check at the entry of the next stage (converted) must
    then fail with address_source_changed, and converted builds nothing."""
    inputs = setup_inputs(tmp_path)
    descriptor = json.loads(inputs["address_source"].read_text(encoding="utf-8"))
    road = inputs["address_dir"] / descriptor["roads"][0]["path"]
    original = pipeline.normalize

    def normalize_then_change(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        road.write_bytes(road.read_bytes() + b"\n")
        commit_all(inputs["address_dir"], "road change")
        return snapshot

    monkeypatch.setattr(pipeline, "normalize", normalize_then_change)
    result = run_full(run_id="changed", **inputs)
    assert result["completed"] is False
    [failure] = result["failures"]
    assert failure["step"] == "converted" and failure["code"] == "address_source_changed"
    assert "differs from pin: " + descriptor["roads"][0]["path"] in failure["detail"]
    run_dir = inputs["runs_root"] / "changed"
    assert set(checkpoints(run_dir)) == {"offline-index", "ingested", "normalized"}
    assert not (run_dir / "converted").exists()  # no snapshot directory was created for the stopped stage
    assert not (run_dir / "run-report").exists()
    assert [e["step"] for e in result["address_source_head"]] == STAGES[:3]
    [attempt] = (run_dir / "attempts").glob("*.json")
    assert json.loads(attempt.read_text(encoding="utf-8"))["failures"] == result["failures"]
