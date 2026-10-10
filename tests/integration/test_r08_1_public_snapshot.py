"""R08-1: verify_public_snapshot reproduces monthly bytes from a synthetic public handoff.

Everything is synthetic; the fetch transport serves local files, never the network.
"""

import json
import shutil

import pytest

from lvr_pipeline.output import package, publish
from lvr_pipeline.output.package import package_output, verify_public_snapshot
from lvr_pipeline.storage.runs import sha256_file
from test_p2_distribution import local_transport
from test_p2_offline import run

NOTICES = {
    "publication_authorized": True,
    "legacy_coordinates_authorized": True,
    "sources": ["synthetic"],
}
REAL_FETCH = publish.fetch_output


@pytest.fixture(scope="module")
def published(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("r08_1")
    converted, *_, state = run(tmp)
    return package_output(converted, state, tmp / "public", notices=NOTICES, run_id="public-snapshot")


def serve_with(monkeypatch, served):
    """Route the module's fetch_output through a local transport serving `served`."""
    transport, calls = local_transport(served)

    def fetch(url, sha, target, **kwargs):
        return REAL_FETCH(url, sha, target, transport=transport, **kwargs)

    monkeypatch.setattr(package, "fetch_output", fetch)
    return calls


def test_public_snapshot_reproduces_all_month_bytes(published, tmp_path, monkeypatch):
    serve_with(monkeypatch, published)
    manifest_sha = sha256_file(published / "manifest.json")
    result = verify_public_snapshot(
        "https://example.invalid/manifest.json", manifest_sha, tmp_path / "work"
    )
    assert result["result"] == "pass"
    assert result["month_file_count"] > 0
    assert result["raw_provided"] is False
    assert result["manifest_sha256"] == manifest_sha
    written = json.loads((tmp_path / "work" / "public-verification.json").read_text(encoding="utf-8"))
    assert written == result


def test_tampered_monthly_hash_in_public_manifest_fails(published, tmp_path, monkeypatch):
    served = tmp_path / "tampered"
    shutil.copytree(published, served)
    manifest_path = served / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    monthly = next(a for a in manifest["assets"] if a["kind"] == "monthly")
    monthly["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    serve_with(monkeypatch, served)
    with pytest.raises(ValueError, match="Cloud rebuilt monthly bytes differ from public snapshot"):
        verify_public_snapshot(
            "https://example.invalid/manifest.json",
            sha256_file(manifest_path),
            tmp_path / "work",
        )
