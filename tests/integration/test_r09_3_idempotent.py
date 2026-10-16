"""R09-3: idempotent TGOS import, refusal of a different response, and submission_unknown."""

import pytest

from lvr_pipeline.storage.parquet import rows
from lvr_pipeline.storage.runs import digest
from lvr_pipeline.tgos import import_tgos, prepare_tgos, transition_batch
from test_p3_tgos import fixture_two_unmatched, response
from test_p2_offline import index_of

ONE = ("121.51", "25.01")
OTHER = ("121.52", "25.02")


def _reply(path, addresses, coordinate=ONE):
    response(
        path,
        [
            {"Address": a, "Response_Address": a, "Response_X": coordinate[0], "Response_Y": coordinate[1]}
            for a in addresses
        ],
    )
    return path


def _snapshot_count(work):
    return len(list((work / "tgos-state" / "snapshots").iterdir()))


def _counts(work, snapshot):
    return (
        _snapshot_count(work),
        len(list(rows(snapshot / "tgos_imports.parquet"))),
        len(list(rows(snapshot / "address_observations.parquet"))),
    )


def _batch(snapshot, batch_id):
    return next(row for row in rows(snapshot / "tgos_batches.parquet") if row["batch_id"] == batch_id)


def _queries(snapshot, batch_id):
    return [row for row in rows(snapshot / "tgos_queries.parquet") if row["batch_id"] == batch_id]


def _prepare(tmp_path, limit=10_000):
    _, state, addresses = fixture_two_unmatched(tmp_path)
    work = tmp_path / "work"
    prepared, _ = prepare_tgos(state, work, tmp_path / "exchange", limit=limit, index=index_of(state))
    batch_id = list(rows(prepared / "tgos_batches.parquet"))[0]["batch_id"]
    return state, work, prepared, batch_id, addresses


def test_same_response_again_returns_same_snapshot(tmp_path):
    _, work, prepared, batch_id, addresses = _prepare(tmp_path)
    submitted = transition_batch(prepared, work, batch_id, "submitted")
    reply = _reply(tmp_path / "reply.csv", addresses)
    imported = import_tgos(submitted, work, batch_id, reply)
    before = _counts(work, imported)
    assert before[1] == 2
    assert import_tgos(imported, work, batch_id, reply) == imported
    assert _counts(work, imported) == before


def test_different_response_for_completed_batch_is_refused(tmp_path):
    _, work, prepared, batch_id, addresses = _prepare(tmp_path)
    submitted = transition_batch(prepared, work, batch_id, "submitted")
    imported = import_tgos(submitted, work, batch_id, _reply(tmp_path / "reply.csv", addresses))
    before = _counts(work, imported)
    other = _reply(tmp_path / "other.csv", addresses, coordinate=OTHER)
    with pytest.raises(ValueError, match="already imported"):
        import_tgos(imported, work, batch_id, other)
    assert _counts(work, imported) == before


def test_submission_unknown_batch_addresses_are_not_selected_again(tmp_path):
    _, work, prepared, batch_id, _ = _prepare(tmp_path, limit=1)
    sent = _queries(prepared, batch_id)[0]["address"]
    unknown = transition_batch(prepared, work, batch_id, "submission_unknown")
    second, _ = prepare_tgos(unknown, work, tmp_path / "exchange", index=index_of(unknown))
    new_batch = [row for row in rows(second / "tgos_batches.parquet") if row["batch_id"] != batch_id][0]
    new_addresses = [row["address"] for row in _queries(second, new_batch["batch_id"])]
    assert sent not in new_addresses
    assert len(new_addresses) == 1


def test_submission_unknown_with_no_other_candidates_produces_no_batch(tmp_path):
    _, work, prepared, batch_id, _ = _prepare(tmp_path)
    unknown = transition_batch(prepared, work, batch_id, "submission_unknown")
    with pytest.raises(ValueError, match="No eligible TGOS candidates"):
        prepare_tgos(unknown, work, tmp_path / "exchange", index=index_of(unknown))
    assert len(list(rows(unknown / "tgos_batches.parquet"))) == 1


def test_retry_of_submission_unknown_query_is_refused(tmp_path):
    _, work, prepared, batch_id, _ = _prepare(tmp_path, limit=1)
    query = _queries(prepared, batch_id)[0]
    unknown = transition_batch(prepared, work, batch_id, "submission_unknown")
    with pytest.raises(ValueError, match="TGOS retry is not in a retryable state"):
        prepare_tgos(
            unknown,
            work,
            tmp_path / "exchange",
            retry_fingerprints=[query["query_fingerprint"]],
            retry_reason="x", index=index_of(unknown))


def test_import_of_submission_unknown_batch_completes_it(tmp_path):
    _, work, prepared, batch_id, addresses = _prepare(tmp_path)
    unknown = transition_batch(prepared, work, batch_id, "submission_unknown")
    imported = import_tgos(unknown, work, batch_id, _reply(tmp_path / "reply.csv", addresses))
    assert _batch(imported, batch_id)["status"] == "completed"
    assert {row["status"] for row in rows(imported / "tgos_imports.parquet")} == {"succeeded"}


def test_cancelled_unknown_batch_is_selected_again_with_approved_retry(tmp_path):
    state, work, prepared, batch_id, _ = _prepare(tmp_path, limit=1)
    query = _queries(prepared, batch_id)[0]
    unknown = transition_batch(prepared, work, batch_id, "submission_unknown")
    cancelled = transition_batch(unknown, work, batch_id, "cancelled", reason="x")
    seeded, _ = prepare_tgos(
        state,
        tmp_path / "new",
        tmp_path / "exchange",
        ledger=cancelled,
        retry_fingerprints=[query["query_fingerprint"]],
        retry_reason="regenerated after cancellation", index=index_of(state))
    new_batch = [row for row in rows(seeded / "tgos_batches.parquet") if row["batch_id"] != batch_id][-1]
    selected = [row["address"] for row in _queries(seeded, new_batch["batch_id"])]
    assert query["address"] in selected
