"""R06-2: verify-sources and inventory-sources on synthetic ZIPs only."""
import json
import shutil
from pathlib import Path

from lvr_pipeline.cli import main
from lvr_pipeline.transactions.inventory import (
    describe_zip, verify_inventory, write_raw_manifest,
)

FIXTURES = Path(__file__).parents[1] / "fixtures/p0"


def _setup(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    for name in ["115q1_lvr_landcsv.zip", "101q1_lvr_landcsv.zip"]:
        shutil.copyfile(FIXTURES / name, raw / name)
    manifest = write_raw_manifest(raw, tmp_path / "manifest.json")
    return raw, manifest, tmp_path / "manifest.json"


def _codes(report, batch):
    row = next(b for b in report["batches"] if b["batch"] == batch)
    return {f["code"] for f in row["failures"]}


def test_all_pass_and_known_empty_counted(tmp_path):
    raw, manifest, _ = _setup(tmp_path)
    report = verify_inventory(raw, manifest)
    assert report["verified"] is True
    assert report["batch_count"] == 2 and report["passed_count"] == 2
    assert report["known_empty_count"] == 1 and report["failed_count"] == 0


def test_missing_file_fails_even_for_known_empty_batch(tmp_path):
    raw, manifest, _ = _setup(tmp_path)
    (raw / "101q1_lvr_landcsv.zip").unlink()
    report = verify_inventory(raw, manifest)
    assert report["verified"] is False
    assert _codes(report, "101q1") == {"missing_file"}
    assert report["known_empty_count"] == 0


def test_hash_mismatch_and_all_failures_listed(tmp_path):
    raw, manifest, _ = _setup(tmp_path)
    (raw / "115q1_lvr_landcsv.zip").write_bytes((raw / "115q1_lvr_landcsv.zip").read_bytes() + b"x")
    (raw / "101q1_lvr_landcsv.zip").unlink()
    report = verify_inventory(raw, manifest)
    assert "sha256_mismatch" in _codes(report, "115q1")
    assert "size_mismatch" in _codes(report, "115q1")
    assert report["failed_count"] == 2  # does not stop at the first failure


def test_member_missing_and_changed(tmp_path):
    raw, manifest, _ = _setup(tmp_path)
    entry = next(e for e in manifest["inputs"] if e["batch"] == "115q1")
    entry["members"].append({"path": "z_lvr_land_a.csv", "size_bytes": 1, "crc32": "00000000", "category": "sales"})
    entry["members"][0]["crc32"] = "deadbeef"
    report = verify_inventory(raw, manifest)
    codes = _codes(report, "115q1")
    assert "member_missing" in codes and "member_crc32_mismatch" in codes


def test_member_unlisted(tmp_path):
    raw, manifest, _ = _setup(tmp_path)
    entry = next(e for e in manifest["inputs"] if e["batch"] == "115q1")
    entry["members"].pop(0)
    assert "member_unlisted" in _codes(verify_inventory(raw, manifest), "115q1")


def test_known_empty_needs_explicit_field_and_reason(tmp_path):
    raw, manifest, _ = _setup(tmp_path)
    entry = next(e for e in manifest["inputs"] if e["batch"] == "101q1")
    entry["known_empty"] = False
    assert "empty_without_known_empty" in _codes(verify_inventory(raw, manifest), "101q1")
    entry["known_empty"] = True
    entry["known_empty_reason"] = None
    assert "known_empty_reason_missing" in _codes(verify_inventory(raw, manifest), "101q1")
    entry["known_empty_reason"] = "inspected"
    entry2 = next(e for e in manifest["inputs"] if e["batch"] == "115q1")
    entry2["known_empty"] = True
    entry2["known_empty_reason"] = "wrong"
    assert "known_empty_has_transactions" in _codes(verify_inventory(raw, manifest), "115q1")


def test_not_a_zip(tmp_path):
    raw, manifest, _ = _setup(tmp_path)
    (raw / "115q1_lvr_landcsv.zip").write_bytes(b"not zip")
    assert "not_a_zip" in _codes(verify_inventory(raw, manifest), "115q1")


def test_cli_exit_codes_and_output(tmp_path, capsys):
    raw, _, manifest_path = _setup(tmp_path)
    assert main(["verify-sources", "--raw-dir", str(raw), "--manifest", str(manifest_path)]) == 0
    assert json.loads(capsys.readouterr().out)["verified"] is True
    (raw / "115q1_lvr_landcsv.zip").unlink()
    assert main(["verify-sources", "--raw-dir", str(raw), "--manifest", str(manifest_path)]) == 1
    assert json.loads(capsys.readouterr().out)["failed_count"] == 1


def test_inventory_sources_cli_matches_describe_zip(tmp_path, capsys):
    raw, _, _ = _setup(tmp_path)
    out = tmp_path / "out" / "raw_manifest.json"
    assert main(["inventory-sources", "--raw-dir", str(raw), "--output", str(out)]) == 0
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["raw_count"] == 2
    assert document["inputs"][0] == describe_zip(raw / "101q1_lvr_landcsv.zip")
    assert main(["inventory-sources", "--raw-dir", str(tmp_path / "empty"), "--output", str(out)]) == 1
