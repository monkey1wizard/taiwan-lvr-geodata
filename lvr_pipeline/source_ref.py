"""Short traceable source reference used by yearly point files.

Format: <src_batch>-<county letter>-<category letter>-<source_row_number>, for example
114q2-e-a-6076 is row 6076 of e_lvr_land_a.csv in the 114q2 batch ZIP. The reference
carries no hash; raw_record_id is recovered with observation_id and the batch's ZIP hash.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .contracts.validate import observation_id

MEMBER = re.compile(r"^([a-z])_lvr_land_([abc])\.csv$", re.IGNORECASE)
REF = re.compile(r"^([0-9a-z]+)-([a-z])-([abc])-([1-9][0-9]*)$")
RAW_MANIFEST = Path(__file__).resolve().parent.parent / "config" / "sources" / "raw_manifest.json"


def make_source_ref(src_batch, member_path, source_row_number):
    match = MEMBER.match(member_path or "")
    if not match:
        raise ValueError(f"member_path does not match <county>_lvr_land_<a|b|c>.csv: {member_path!r}")
    if not src_batch or "-" in src_batch:
        raise ValueError(f"Unusable src_batch: {src_batch!r}")
    if isinstance(source_row_number, bool) or not isinstance(source_row_number, int) or source_row_number < 1:
        raise ValueError("Expected a positive source row number")
    return f"{src_batch}-{match.group(1).lower()}-{match.group(2).lower()}-{source_row_number}"


def parse_source_ref(ref):
    """Return (src_batch, member_path, source_row_number)."""
    match = REF.match(ref or "")
    if not match:
        raise ValueError(f"Malformed source_ref: {ref!r}")
    batch, county, category, row = match.groups()
    return batch, f"{county}_lvr_land_{category}.csv", int(row)


def load_batch_hashes(path=RAW_MANIFEST):
    inputs = json.loads(Path(path).read_text(encoding="utf-8"))["inputs"]
    return {item["batch"]: item["sha256"] for item in inputs}


def resolve_source_ref(ref, batch_hashes=None):
    """Return {src_batch, member_path, source_row_number, input_sha256, raw_record_id}."""
    batch, member, row = parse_source_ref(ref)
    hashes = load_batch_hashes() if batch_hashes is None else batch_hashes
    if batch not in hashes:
        raise ValueError(f"Unknown source batch: {batch}")
    return {"src_batch": batch, "member_path": member, "source_row_number": row,
            "input_sha256": hashes[batch], "raw_record_id": observation_id(hashes[batch], member, row)}


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Resolve a yearly-point source_ref")
    parser.add_argument("source_ref", nargs="+")
    parser.add_argument("--raw-manifest", type=Path, default=RAW_MANIFEST)
    args = parser.parse_args(argv)
    hashes = load_batch_hashes(args.raw_manifest)
    for ref in args.source_ref:
        print(json.dumps(resolve_source_ref(ref, hashes), ensure_ascii=False))


if __name__ == "__main__":
    main()
