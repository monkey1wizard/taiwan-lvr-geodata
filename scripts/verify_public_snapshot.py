"""Linux public handoff acceptance: fetch state and reproduce all month bytes without raw."""

import argparse
import json
from pathlib import Path

from lvr_pipeline.distribution import fetch_output
from lvr_pipeline.packaging import package_output, verify_output, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest-url", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument(
        "--work-dir", type=Path, default=Path("data/downloads/public-verification")
    )
    args = parser.parse_args()
    fetched = fetch_output(
        args.manifest_url,
        args.manifest_sha256,
        args.work_dir / "handoff",
        maintenance=True,
    )
    manifest = json.loads((fetched / "manifest.json").read_text(encoding="utf-8"))
    handoff_root = fetched / "maintenance"
    handoff = json.loads((handoff_root / "handoff.json").read_text(encoding="utf-8"))
    rebuilt = package_output(
        handoff_root / handoff["converted"],
        handoff_root / handoff["state"],
        args.work_dir / "rebuilt",
        notices=json.loads((handoff_root / "NOTICE.json").read_text(encoding="utf-8")),
        run_id="cloud-rebuilt",
    )
    rebuilt_manifest = verify_output(rebuilt)
    expected = {
        a["path"]: a["sha256"] for a in manifest["assets"] if a["kind"] == "monthly"
    }
    actual = {
        a["path"]: a["sha256"]
        for a in rebuilt_manifest["assets"]
        if a["kind"] == "monthly"
    }
    if expected != actual:
        raise ValueError("Cloud rebuilt monthly bytes differ from public snapshot")
    sample = next(
        m["tx_yyyymm"] for m in manifest["months"] if m["categories"]["sales"]["rows"]
    )
    fetch_output(
        args.manifest_url,
        args.manifest_sha256,
        args.work_dir / "month",
        month=sample,
        category="sales",
    )
    result = {
        "snapshot_id": manifest["snapshot_id"],
        "manifest_sha256": args.manifest_sha256,
        "raw_provided": False,
        "month_file_count": len(expected),
        "retained_rows": manifest["retained_rows"],
        "all_month_hashes_match": True,
        "unmatched_selection_verified": True,
        "tgos_started": False,
        "result": "pass",
    }
    write_json(args.work_dir / "public-verification.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
