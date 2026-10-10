"""Indexed maintenance parts: round trip, tamper detection, legacy single ZIP."""

import json
import shutil
import zipfile

import pytest

from lvr_pipeline.output.publish import fetch_output
from lvr_pipeline.output.package import package_output
from lvr_pipeline.output.publish import extract_handoff, write_maintenance
from lvr_pipeline.output.verify import verify_output
from lvr_pipeline.storage.runs import sha256_file
from test_p2_distribution import local_transport
from test_p2_offline import run

NOTICES = {
    "publication_authorized": True,
    "legacy_coordinates_authorized": True,
    "sources": ["synthetic"],
}
SMALL = 24000  # F-8: monthly GeoParquet grew ~2.8KB with the GIS attribute columns


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("parts")
    converted, *_, state = run(tmp)
    parts = package_output(
        converted, state, tmp / "parts", notices=NOTICES, max_asset_bytes=SMALL
    )
    legacy = package_output(converted, state, tmp / "legacy", notices=NOTICES)
    return converted, state, parts, legacy


def files_of(root):
    return {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file()}


def maintenance(root):
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    return [a for a in manifest["assets"] if a["kind"] == "maintenance"]


def copy_output(root, target):
    shutil.copytree(root, target)
    return target


def first_part(root):
    return next(a for a in maintenance(root) if a.get("role") == "part")


def index_of(root):
    return next(a for a in maintenance(root) if a.get("role") == "index")


def rewrite_index(root, edit):
    path = root / index_of(root)["path"]
    value = json.loads(path.read_text(encoding="utf-8"))
    edit(value)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_small_limit_forces_several_indexed_parts(built):
    _, _, parts, _ = built
    assets = maintenance(parts)
    index = [a for a in assets if a.get("role") == "index"]
    pieces = [a for a in assets if a.get("role") == "part"]
    assert len(index) == 1 and len(pieces) > 2
    assert all(a["bytes"] <= SMALL for a in assets)
    assert [a["asset_name"] for a in pieces] == sorted(a["asset_name"] for a in pieces)
    assert index[0]["asset_name"].endswith("_maintenance_index.json")
    assert not [a for a in assets if a["asset_name"].endswith("_maintenance.zip")]
    verify_output(parts)


def test_round_trip_matches_source_snapshots_file_by_file(built, tmp_path):
    converted, state, parts, _ = built
    index = index_of(parts)
    handoff = extract_handoff(parts / index["path"], tmp_path / "fresh")
    for prefix, snapshot in (("converted", converted), ("state", state)):
        source = files_of(snapshot)
        restored = files_of(tmp_path / "fresh" / prefix / "snapshots" / snapshot.name)
        assert source.keys() == restored.keys()
        for name, path in source.items():
            assert sha256_file(path) == sha256_file(restored[name])
    assert handoff["state"] == f"state/snapshots/{state.name}"
    declared = json.loads((parts / index["path"]).read_text(encoding="utf-8"))
    assert declared["member_count"] == len(declared["members"])
    assert declared["part_count"] == len(declared["parts"])


def test_corrupted_part_fails(built, tmp_path):
    root = copy_output(built[2], tmp_path / "bad")
    path = root / first_part(root)["path"]
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(ValueError, match="part hash"):
        extract_handoff(root / index_of(root)["path"], tmp_path / "x")


def test_missing_part_fails(built, tmp_path):
    root = copy_output(built[2], tmp_path / "bad")
    (root / first_part(root)["path"]).unlink()
    with pytest.raises(ValueError, match="part missing"):
        extract_handoff(root / index_of(root)["path"], tmp_path / "x")


def test_extra_file_in_part_fails(built, tmp_path):
    root = copy_output(built[2], tmp_path / "bad")
    part = root / first_part(root)["path"]
    with zipfile.ZipFile(part, "a") as archive:
        archive.writestr("extra.txt", "x")

    def fix(value):
        entry = next(p for p in value["parts"] if p["name"] == part.name)
        entry["bytes"] = part.stat().st_size
        entry["sha256"] = sha256_file(part)

    rewrite_index(root, fix)
    with pytest.raises(ValueError, match="member list differs"):
        extract_handoff(root / index_of(root)["path"], tmp_path / "x")


def test_member_hash_mismatch_in_index_fails(built, tmp_path):
    root = copy_output(built[2], tmp_path / "bad")

    def fix(value):
        value["members"][0]["sha256"] = "0" * 64

    rewrite_index(root, fix)
    with pytest.raises(ValueError, match="member hash"):
        extract_handoff(root / index_of(root)["path"], tmp_path / "x")


def test_member_removed_from_index_fails(built, tmp_path):
    root = copy_output(built[2], tmp_path / "bad")

    def fix(value):
        value["members"].pop()
        value["member_count"] -= 1
        value["member_bytes"] = sum(m["bytes"] for m in value["members"])

    rewrite_index(root, fix)
    with pytest.raises(ValueError, match="member list differs"):
        extract_handoff(root / index_of(root)["path"], tmp_path / "x")


def test_verify_output_rejects_index_part_mismatch(built, tmp_path):
    root = copy_output(built[2], tmp_path / "bad")

    def fix(value):
        value["parts"].pop()
        value["part_count"] -= 1

    rewrite_index(root, fix)
    with pytest.raises(ValueError):
        verify_output(root)


def test_oversize_single_file_raises(tmp_path):
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 10000)
    with pytest.raises(ValueError, match="member exceeds"):
        write_maintenance(tmp_path, "id", [("big.bin", big)], 8000)


def test_legacy_single_zip_still_extracts_and_verifies(built, tmp_path):
    _, _, _, legacy = built
    assets = maintenance(legacy)
    assert len(assets) == 1 and assets[0]["asset_name"].endswith("_maintenance.zip")
    assert "role" not in assets[0]
    verify_output(legacy)
    handoff = extract_handoff(legacy / assets[0]["path"], tmp_path / "fresh")
    assert (tmp_path / "fresh" / handoff["state"] / "manifest.json").exists()


def test_fetch_maintenance_with_parts_and_legacy(built, tmp_path):
    _, _, parts, legacy = built
    transport, calls = local_transport(parts)
    fetched = fetch_output(
        "https://example.invalid/manifest.json",
        sha256_file(parts / "manifest.json"),
        tmp_path / "parts",
        maintenance=True,
        transport=transport,
    )
    assert len(calls) == 1 + len(maintenance(parts))
    assert (fetched / "maintenance/handoff.json").exists()
    transport, calls = local_transport(legacy)
    fetched = fetch_output(
        "https://example.invalid/manifest.json",
        sha256_file(legacy / "manifest.json"),
        tmp_path / "legacy",
        maintenance=True,
        transport=transport,
    )
    assert len(calls) == 2 and (fetched / "maintenance/handoff.json").exists()


def test_full_fetch_verifies_parts(built, tmp_path):
    _, _, parts, _ = built
    transport, _ = local_transport(parts)
    fetch_output(
        "https://example.invalid/manifest.json",
        sha256_file(parts / "manifest.json"),
        tmp_path / "full",
        transport=transport,
    )
