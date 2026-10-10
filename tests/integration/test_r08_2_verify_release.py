"""R08-2: verify_public_snapshot recomputes the unmatched TGOS candidate set from the handoff."""

import json

import pytest

from lvr_pipeline.output import package
from lvr_pipeline.output.package import verify_public_snapshot, verify_unmatched_candidates
from lvr_pipeline.storage.parquet import rows
from lvr_pipeline.storage.runs import sha256_file
from test_r08_1_public_snapshot import published, serve_with  # noqa: F401
from test_p2_offline import run


@pytest.fixture(scope="module")
def synthetic_state(tmp_path_factory):
    *_, state = run(tmp_path_factory.mktemp("r08_2"))
    return state


def independent_count(state):
    """Unmatched/outside_scope rows read straight from the parquet, no pipeline helper."""
    table = state / "address_index.parquet"
    return sum(1 for r in rows(table) if r["status"] in {"unmatched", "outside_scope"})


def test_public_snapshot_reports_unmatched_candidates(published, tmp_path, monkeypatch):
    serve_with(monkeypatch, published)
    result = verify_public_snapshot(
        "https://example.invalid/manifest.json",
        sha256_file(published / "manifest.json"),
        tmp_path / "work",
    )
    handoff = tmp_path / "work" / "handoff" / "maintenance"
    meta = json.loads((handoff / "handoff.json").read_text(encoding="utf-8"))
    expected = independent_count(handoff / meta["state"])
    assert result["unmatched_selection_verified"] is True
    assert result["unmatched_candidates"] == expected


def test_state_candidate_count_matches_independent_count(synthetic_state):
    assert verify_unmatched_candidates(synthetic_state) == independent_count(synthetic_state)


def test_zero_eligible_candidates_still_passes(synthetic_state, monkeypatch):
    real = package.load_state

    def empty(path):
        manifest, report, stage = real(path)
        return manifest, {**report, "status_counts": {"located": 1}}, stage

    monkeypatch.setattr(package, "load_state", empty)
    monkeypatch.setattr(package, "rows", lambda path: iter([]))
    assert verify_unmatched_candidates(synthetic_state) == 0


def test_status_counts_disagreeing_with_unmatched_table_fails(synthetic_state, monkeypatch):
    real = package.load_state

    def tampered(path):
        manifest, report, stage = real(path)
        counts = {**report["status_counts"], "unmatched": report["status_counts"].get("unmatched", 0) + 5}
        return manifest, {**report, "status_counts": counts}, stage

    monkeypatch.setattr(package, "load_state", tampered)
    with pytest.raises(ValueError, match="differs from state status_counts"):
        verify_unmatched_candidates(synthetic_state)


def fake_rows(statuses, county="A"):
    return [
        {
            "status": status,
            "building_key": f"k{n}",
            "canonical_address": f"addr{n}",
            "county_code": county,
        }
        for n, status in enumerate(statuses)
    ]


def patch_state(monkeypatch, status_counts, table):
    real = package.load_state

    def load(path):
        manifest, report, stage = real(path)
        return manifest, {**report, "status_counts": status_counts}, stage

    monkeypatch.setattr(package, "load_state", load)
    monkeypatch.setattr(package, "rows", lambda path: iter(table))


def test_nonzero_candidates_are_counted(synthetic_state, monkeypatch):
    table = fake_rows(["unmatched", "outside_scope", "located"])
    patch_state(monkeypatch, {"unmatched": 1, "outside_scope": 1, "located": 1}, table)
    assert verify_unmatched_candidates(synthetic_state) == 2


def test_emptied_unmatched_table_fails(synthetic_state, monkeypatch):
    patch_state(monkeypatch, {"unmatched": 2, "located": 1}, fake_rows(["located"]))
    with pytest.raises(ValueError, match="differs from state status_counts"):
        verify_unmatched_candidates(synthetic_state)
