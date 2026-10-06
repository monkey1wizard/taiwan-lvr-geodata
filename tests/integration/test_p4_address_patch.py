"""P4 address patch export acceptance."""

from lvr_pipeline.address_patch import _split_legacy, export_address_patch, verify_address_patch
from lvr_pipeline.storage.parquet import rows
from lvr_pipeline.tgos import import_tgos, prepare_tgos, transition_batch
from test_p3_tgos import fixture_state, response


def imported_state(tmp_path, response_address):
    _, state, address = fixture_state(tmp_path)
    prepared, _ = prepare_tgos(
        state,
        tmp_path / "work",
        tmp_path / "exchange",
        limit=1,
    )
    query = list(rows(prepared / "tgos_queries.parquet"))[0]
    submitted = transition_batch(
        prepared, tmp_path / "work", query["batch_id"], "submitted"
    )
    result = tmp_path / "response.csv"
    response(
        result,
        [
            {
                "Address": address,
                "Response_Address": response_address,
                "Response_X": "121.51",
                "Response_Y": "25.01",
            }
        ],
    )
    return import_tgos(
        submitted, tmp_path / "work", query["batch_id"], result
    )


def test_export_address_patch_keeps_legacy_columns_and_provenance(tmp_path):
    imported = imported_state(tmp_path, "臺北市中正區幸福里1鄰測試路11號")
    area = tmp_path / "area.csv"
    area.write_text(
        "name,dgbas_id\n臺北市中正區幸福里,6300100-001\n",
        encoding="utf-8",
    )
    patch = export_address_patch(
        imported, [area], tmp_path / "work", run_id="patch-ok"
    )
    report = verify_address_patch(patch)
    assert report["patch_rows"] == 1
    assert report["quarantine_rows"] == 0
    row = list(rows(patch / "address_patch.parquet"))[0]
    assert [
        row[key]
        for key in [
            "full_addr",
            "county",
            "town",
            "village",
            "neighborhood",
            "road",
            "section",
            "lane",
            "alley",
            "sub_alley",
            "tong",
            "number",
            "x",
            "y",
        ]
    ] == [
        "臺北市中正區幸福里1鄰測試路11號",
        "63",
        "6300100",
        "6300100-001",
        "1鄰",
        "測試路",
        "",
        "",
        "",
        "",
        "",
        "11號",
        121.51,
        25.01,
    ]
    provenance = list(rows(patch / "provenance.parquet"))
    assert len(provenance) == 1
    assert provenance[0]["patch_id"] == row["patch_id"]
    assert provenance[0]["source_kind"] == "tgos_result"


def test_export_address_patch_quarantines_missing_village_evidence(tmp_path):
    imported = imported_state(tmp_path, "臺北市中正區測試路11號")
    area = tmp_path / "area.csv"
    area.write_text("name,dgbas_id\n", encoding="utf-8")
    patch = export_address_patch(
        imported, [area], tmp_path / "work", run_id="patch-quarantine"
    )
    report = verify_address_patch(patch)
    assert report["patch_rows"] == 0
    assert report["quarantine_rows"] == 1
    assert list(rows(patch / "quarantine.parquet"))[0]["reason"] == (
        "response_address_missing_village"
    )


def test_split_legacy_normalizes_tgos_post_number_syntax():
    legacy, reason = _split_legacy(
        "臺南市佳里區海澄里1鄰萊芉寮5號之2",
        "6701200",
        {"臺南市佳里區海澄里": {"6701200-001"}},
    )
    assert reason is None
    assert legacy["full_addr"] == "臺南市佳里區海澄里1鄰萊芉寮5之2號"
    assert legacy["number"] == "5之2號"
