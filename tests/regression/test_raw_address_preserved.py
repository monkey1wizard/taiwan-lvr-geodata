# -*- coding: utf-8 -*-
"""R04-9: normalize() keeps the original door text while the address component holds the corrected text.

Synthetic input only. The address contains a garbled PUA character (U+E001 -> 槺) and a variant (巿 -> 市), both
covered by config/rules/character-fixes.csv. The test runs the real ingest and normalize path.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))

from lvr_pipeline.transactions.read import ingest  # noqa: E402
from lvr_pipeline.transactions.normalize import normalize  # noqa: E402
from test_p1_conversion import CODE, dataset, make_source  # noqa: E402

RULES = Path(__file__).resolve().parents[2] / "config" / "rules" / "character-fixes.csv"
RAW = "高雄市榔七街10號"
RAW_VARIANT = "臺北巿中正區測試路10號"


def test_normalize_keeps_original_address_and_stores_corrected_text(tmp_path):
    raw, manifest = make_source(tmp_path, [{"address": RAW}, {"address": RAW_VARIANT}], categories=("sales",))
    work = tmp_path / "work"
    ingested = ingest(raw, manifest, ["115q1"], work, code_commit=CODE)
    normalized = normalize(ingested, work, cutoff=202610, rules_path=RULES, code_commit=CODE)
    observations = dataset(normalized, "observation")
    components = dataset(normalized, "address-component")
    assert [o["raw_address"] for o in observations] == [RAW, RAW_VARIANT]
    texts = [c["normalized_address"] for c in components]
    assert texts == ["高雄市槺榔七街10號", "臺北市中正區測試路10號"]
    for observation, text in zip(observations, texts):
        assert observation["raw_address"] != text
