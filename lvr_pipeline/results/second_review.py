"""Second manual-review area (R10-1a): keys that still have no usable coordinate after TGOS.

One row per review key: the ``building_key``, or for a ``district_missing`` address the query
fingerprint of its door-level text (R09-6 formula; an address with no parseable door uses
``digest(["district_missing", county, normalized_address])``). Rows are rebuilt on every review
stage and only ever start as ``pending``; the owner's decision files set the other states.

A key enters when it has no waiting TGOS query (prepared, submitted, submission_unknown) and its
final address status is not ``located``. The latest imported TGOS result of the key decides the
entry reason; the order of ``tgos-result`` rows is the import order, so the last row of a key is the
latest import and the earlier ones are listed in ``prior_result_ids``. A key never sent to TGOS
enters only when its query text does not round-trip (``tgos_key_text_not_round_trip``), or, for a
district address, when it has no candidate town (``district_no_candidate_town``, needs the offline
index). A ``conflict`` key enters as ``conflict`` unless its county is suspended.

Decision files (``YYYYMMDD-NN-<sha8>.csv``) are owner evidence; this module only reads them.
Nothing here changes a ``building_key`` or merges keys.
"""

from __future__ import annotations

import csv
import datetime
import hashlib
import io
import json
from pathlib import Path
import re

from ..addresses.identity import building_key
from ..addresses.pool import district_query
from ..contracts.schemas import dataset_schema
from ..contracts.validate import within_taiwan_bounds
from ..output.rows import make_transaction_id
from ..storage.parquet import BatchWriter
from ..storage.runs import digest
from ..tgos import batches as tgos_batches

# Entry reasons that exist only here (EXCEPTION_REASON_CODES holds the codes of the review table too).
ENTRY_REASONS = (
    "tgos_not_found",
    "tgos_isolated",
    "tgos_response_incomplete",
    "tgos_coordinate_invalid",
    "tgos_village_ambiguous",
    "tgos_key_text_not_round_trip",
    "district_no_candidate_town",
    "conflict",
)
STATUSES = ("pending", "adopted", "rejected", "deferred")
WAITING_QUERY = {"prepared", "submitted", "submission_unknown"}
_COLUMNS = [field.name for field in dataset_schema("second-review")]
DECISION_NAME = re.compile(r"^\d{8}-\d{2}-[0-9a-f]{8}\.csv$")
DECISION_COLUMNS = ["review_key", "decision", "mode", "tgos_result_id", "lng", "lat", "address", "basis",
                    "decided_by", "decided_at"]
DECISION_MODES = ("accept_tgos_response", "manual_coordinate")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_decisions(directory: Path) -> list[dict]:
    """Every decision file of the directory in file-name order, checked for name, hash and fields.

    A bad file name, name hash, header, row or a repeated review key fails the whole call.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError(f"Review decisions directory does not exist: {directory}")
    files = []
    for path in sorted(p for p in directory.iterdir() if p.is_file() and p.name.endswith(".csv")):
        if not DECISION_NAME.fullmatch(path.name):
            raise ValueError(f"Decision file name is not YYYYMMDD-NN-<sha8>.csv: {path.name}")
        data = path.read_bytes()
        sha256 = hashlib.sha256(data).hexdigest()
        if sha256[:8] != path.name[-12:-4]:
            raise ValueError(f"Decision file {path.name} content hash {sha256[:8]} differs from its name")
        files.append({"name": path.name, "sha256": sha256, "rows": _parse_decisions(path.name, data)})
    return files


def decision_binding(files: list[dict]) -> list[list[str]]:
    return [[item["name"], item["sha256"]] for item in files]


def _parse_decisions(name: str, data: bytes) -> list[dict]:
    if data.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"Decision file {name} has a BOM")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"Decision file {name} is not UTF-8") from None
    lines = list(csv.reader(io.StringIO(text, newline="")))
    if not lines or lines[0] != DECISION_COLUMNS:
        raise ValueError(f"Decision file {name} header is not {','.join(DECISION_COLUMNS)}")
    decisions, seen = [], set()
    for number, values in enumerate(lines[1:], 1):
        if len(values) != len(DECISION_COLUMNS):
            raise ValueError(f"Decision file {name} row {number} has {len(values)} fields")
        row = {column: value.strip() for column, value in zip(DECISION_COLUMNS, values)}
        _check_decision(name, number, row)
        if row["review_key"] in seen:
            raise ValueError(f"Decision file {name} row {number} repeats review_key {row['review_key']}")
        seen.add(row["review_key"])
        decisions.append({**row, "row": number})
    return decisions


def _check_decision(name: str, number: int, row: dict) -> None:
    def fail(message):
        raise ValueError(f"Decision file {name} row {number}: {message}")

    if not row["review_key"]:
        fail("review_key is empty")
    if row["decision"] not in STATUSES[1:]:
        fail(f"decision {row['decision']!r} is not adopted, rejected or deferred")
    if row["decision"] == "adopted":
        if row["mode"] not in DECISION_MODES:
            fail(f"adopted needs mode {' or '.join(DECISION_MODES)}")
        if row["mode"] == "accept_tgos_response" and not row["tgos_result_id"]:
            fail("accept_tgos_response needs tgos_result_id")
        if row["mode"] == "manual_coordinate" and not (row["lng"] and row["lat"] and row["address"]):
            fail("manual_coordinate needs lng, lat and address")
    for column in ("basis", "decided_by", "decided_at"):
        if not row[column]:
            fail(f"{column} is empty")
    try:
        if not _DATE.fullmatch(row["decided_at"]):
            raise ValueError
        datetime.date.fromisoformat(row["decided_at"])
    except ValueError:
        fail(f"decided_at {row['decided_at']!r} is not YYYY-MM-DD")


def _response_key(address: str | None) -> tuple[str | None, str | None]:
    """The key of the response address under the current rules, or why there is none."""
    if not address:
        return None, "no response address"
    key = building_key(address)
    return (key, None) if key is not None else (None, "building_key returned None")


def _mismatch_fields(query_key: str, response_key: str | None) -> str | None:
    if response_key is None:
        return None
    asked, answered = json.loads(query_key[3:]), json.loads(response_key[3:])
    return json.dumps(sorted(f for f in asked.keys() | answered.keys() if asked.get(f) != answered.get(f)),
                      ensure_ascii=False, separators=(",", ":"))


def _entry_reason(result: dict, rejection_codes: dict[str, str]) -> str:
    if result["status"] == "failed" and not result["reason"]:
        return "tgos_not_found"
    if result["status"] == "rejected" and result["reason"] in rejection_codes:
        return rejection_codes[result["reason"]]
    raise ValueError(
        f"TGOS result {result['result_id']} (status {result['status']}, reason {result['reason']!r}) "
        "has no second review entry reason"
    )


def _blank(review_key: str, key_kind: str, entry_reason: str, **fields) -> dict:
    row = {name: None for name in _COLUMNS}
    row.update(
        review_id=digest(["second_review", review_key]), review_key=review_key, key_kind=key_kind,
        entry_reason=entry_reason, status="pending", observation_count=0, **fields,
    )
    return row


def _with_result(row: dict, results: list[dict]) -> dict:
    """Attach the latest result as evidence and the earlier ones by id."""
    latest = results[-1]
    key, why = _response_key(latest["response_address"])
    row.update(
        query_fingerprint=latest["query_fingerprint"], tgos_batch_id=latest["batch_id"],
        tgos_result_id=latest["result_id"], prior_result_ids=json.dumps([r["result_id"] for r in results[:-1]]),
        response_address=latest["response_address"], response_building_key=key,
        response_building_key_reason=why, response_lng=latest["lng"], response_lat=latest["lat"],
        response_sha256=latest["response_sha256"],
    )
    return row


def _ledger(tgos: dict) -> tuple[dict, dict]:
    """Per review key: waiting flag and results in import order; and every result by id."""
    by_query = {(q["batch_id"], q["query_fingerprint"]): q for q in tgos["queries"]}
    keys: dict[str, dict] = {}

    def review_key(query):
        return query["building_key"] if query["query_kind"] == "building_key" else query["query_fingerprint"]

    for query in tgos["queries"]:
        entry = keys.setdefault(review_key(query), {"kind": query["query_kind"], "waiting": False,
                                                    "results": [], "query": query})
        entry["waiting"] |= query["status"] in WAITING_QUERY
    for result in tgos["results"]:
        query = by_query.get((result["batch_id"], result["query_fingerprint"]))
        if query is None:
            raise ValueError(f"TGOS result {result['result_id']} has no query")
        keys[review_key(query)]["results"].append(result)
    return keys, {r["result_id"]: r for r in tgos["results"]}


def _district_texts(db) -> dict:
    """Door-level query text and fingerprint of every district_missing address (R09-6 formula)."""
    found = {}
    for county, address in db.execute(
        "SELECT DISTINCT county_code, normalized_address FROM occurrence "
        "WHERE reason = 'district_missing' AND county_code <> '' ORDER BY 1, 2"
    ).fetchall():
        parsed = district_query(address)
        if parsed is None:
            found[(county, address)] = (address, digest(["district_missing", county, address]))
        else:
            text = tgos_batches.district_query_text(county, parsed)
            found[(county, address)] = (text, digest(["district_missing", county, text]))
    return found


def build_second_review(
    db, build_dir: Path, *, suspended_codes: set[str], tgos: dict, rejection_codes: dict[str, str],
    occurrences: Path, index_rows: Path | None, decisions: list[dict],
) -> tuple[list[tuple[str, Path, str, int]], tuple[Path, int], dict]:
    """Write second_review.parquet/.csv and second_review_observations.parquet into build_dir.

    ``db`` holds the views occurrence, result (final address status), observations and components.
    ``tgos`` has the lists batches, queries and results of the TGOS state (empty without one).
    Returns the parquet artifacts for ``Stage.finish``, the CSV path with its row count (the caller
    adds it to the store) and the report block.
    """
    ledger, results_by_id = _ledger(tgos)
    rows: dict[str, dict] = {}

    # Building keys that are not located.
    cursor = db.execute(
        "SELECT building_key, canonical_address, county_code, status FROM result WHERE status <> 'located'"
    )
    while batch := cursor.fetchmany(4096):
        for key, address, county, status in batch:
            known = ledger.get(key)
            if known is not None and known["waiting"]:
                continue
            base = {"building_key": key, "county_code": county, "town": json.loads(key[3:])["town"],
                    "canonical_address": address, "offline_status": status}
            if status == "conflict":
                if county not in suspended_codes:
                    rows[key] = _blank(key, "building_key", "conflict", **base)
            elif known is not None and known["results"]:
                results = known["results"]
                if results[-1]["status"] == "succeeded" and county in suspended_codes:
                    continue
                row = _blank(key, "building_key", _entry_reason(results[-1], rejection_codes), **base)
                _with_result(row, results)
                if row["entry_reason"] == "tgos_isolated":
                    row["mismatch_fields"] = _mismatch_fields(key, row["response_building_key"])
                rows[key] = row
            elif status in {"unmatched", "outside_scope"}:
                try:
                    tgos_batches.key_query_text(key)
                except ValueError:
                    rows[key] = _blank(key, "building_key", "tgos_key_text_not_round_trip", **base)

    # District queries in the ledger, and district addresses never sent.
    queried_fingerprints = {k for k, v in ledger.items() if v["kind"] == "district_missing"}
    texts = _district_texts(db) if index_rows is not None or queried_fingerprints else {}
    for fingerprint in queried_fingerprints:
        known = ledger[fingerprint]
        if known["waiting"] or not known["results"] or known["results"][-1]["status"] == "succeeded":
            continue
        query = known["query"]
        row = _blank(fingerprint, "district_query", _entry_reason(known["results"][-1], rejection_codes),
                     county_code=query["county_code"], canonical_address=query["address"])
        rows[fingerprint] = _with_result(row, known["results"])
    if index_rows is not None:
        candidates, _ = tgos_batches.select_district_candidates(occurrences, index_rows)
        with_candidates = {c["query_fingerprint"] for c in candidates}
        for (county, address), (text, fingerprint) in texts.items():
            if fingerprint in queried_fingerprints or fingerprint in with_candidates:
                continue
            rows.setdefault(fingerprint, _blank(
                fingerprint, "district_query", "district_no_candidate_town", query_fingerprint=fingerprint,
                county_code=county, canonical_address=text))

    # Observations of every entered key, from this run's address members.
    db.execute("CREATE OR REPLACE TEMP TABLE entry_map (review_key VARCHAR, building_key VARCHAR, "
               "county_code VARCHAR, normalized_address VARCHAR)")
    mapped = [(k, k, None, None) for k, r in rows.items() if r["key_kind"] == "building_key"]
    mapped += [(fingerprint, None, county, address)
               for (county, address), (_, fingerprint) in texts.items() if fingerprint in rows]
    if mapped:
        db.executemany("INSERT INTO entry_map VALUES (?, ?, ?, ?)", mapped)
    observation_sql = """
        SELECT k.review_key, ob.src_batch, ob.member_path, ob.source_row_number, o.raw_record_id, o.component_id,
               ob.category, ob.tx_yyyymm, c.ordinal
        FROM occurrence o JOIN entry_map k ON o.building_key = k.building_key
        JOIN observations ob USING (raw_record_id) JOIN components c USING (component_id)
        UNION ALL
        SELECT k.review_key, ob.src_batch, ob.member_path, ob.source_row_number, o.raw_record_id, o.component_id,
               ob.category, ob.tx_yyyymm, c.ordinal
        FROM occurrence o JOIN entry_map k ON k.building_key IS NULL AND o.reason = 'district_missing'
         AND o.county_code = k.county_code AND o.normalized_address = k.normalized_address
        JOIN observations ob USING (raw_record_id) JOIN components c USING (component_id)
        ORDER BY 1, 2, 3, 4, 6"""
    observations = []
    for key, batch, member, number, raw_id, component_id, category, month, ordinal in db.execute(observation_sql).fetchall():
        row = rows[key]
        observations.append({
            "review_id": row["review_id"], "transaction_id": make_transaction_id(batch, member, number),
            "door_index": ordinal + 1, "raw_record_id": raw_id, "component_id": component_id,
            "category": category, "tx_yyyymm": month,
        })
        row["observation_count"] += 1
        if month is not None:
            row["tx_yyyymm_min"] = month if row["tx_yyyymm_min"] is None else min(row["tx_yyyymm_min"], month)
            row["tx_yyyymm_max"] = month if row["tx_yyyymm_max"] is None else max(row["tx_yyyymm_max"], month)

    files = _apply_decisions(rows, decisions, results_by_id)

    ordered = sorted(rows.values(), key=lambda r: (r["entry_reason"], r["review_key"]))
    writer = BatchWriter(Path(build_dir) / "second_review.parquet", "second-review")
    csv_path = Path(build_dir) / "second_review.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        output = csv.writer(stream)
        output.writerow(_COLUMNS)
        for row in ordered:
            writer.add(row)
            output.writerow(["" if row[name] is None else row[name] for name in _COLUMNS])
    writer.close()
    observation_writer = BatchWriter(Path(build_dir) / "second_review_observations.parquet",
                                     "second-review-observation")
    for row in observations:
        observation_writer.add(row)
    observation_writer.close()
    report = {
        "district_candidates_checked": index_rows is not None,
        "rows": writer.row_count,
        "entry_reason_counts": {code: sum(r["entry_reason"] == code for r in ordered) for code in ENTRY_REASONS},
        "status_counts": {code: sum(r["status"] == code for r in ordered) for code in STATUSES},
        "observation_rows": observation_writer.row_count,
        "csv_bytes": csv_path.stat().st_size,
        "decision_files": files,
        "decision_totals": {
            name: sum(f[name] for f in files) for name in ("rows", "applied", "superseded", "orphaned", "stale")
        },
    }
    return (
        [("second_review.parquet", writer.path, "second-review", writer.row_count),
         ("second_review_observations.parquet", observation_writer.path, "second-review-observation",
          observation_writer.row_count)],
        (csv_path, writer.row_count),
        report,
    )


def _apply_decisions(rows: dict[str, dict], decisions: list[dict], results_by_id: dict) -> list[dict]:
    """Apply decision files in name order; a later valid decision replaces an earlier one."""
    final: dict[str, tuple[dict, dict, dict, dict]] = {}
    summary = []
    for file in decisions:
        counts = {"file": file["name"], "sha256": file["sha256"], "rows": len(file["rows"]),
                  "applied": 0, "superseded": 0, "orphaned": 0, "stale": 0}
        summary.append(counts)
        for decision in file["rows"]:
            row = rows.get(decision["review_key"])
            if row is None:
                counts["orphaned"] += 1
                continue
            outcome = _validate_against_run(row, decision, results_by_id)
            if outcome is None:
                counts["stale"] += 1
                continue
            if decision["review_key"] in final:
                earlier = final[decision["review_key"]][0]
                earlier["applied"] -= 1
                earlier["superseded"] += 1
            final[decision["review_key"]] = (counts, file, decision, outcome)
            counts["applied"] += 1
    for key, (_, file, decision, outcome) in final.items():
        rows[key].update(
            status=decision["decision"], decision_file=file["name"], decision_file_sha256=file["sha256"],
            decision_row=decision["row"], decided_at=decision["decided_at"], **outcome,
        )
    return summary


def _validate_against_run(row: dict, decision: dict, results_by_id: dict) -> dict | None:
    """The decision_* values to store, or None when the decision is stale for this run."""
    empty = {"decision_mode": None, "decision_lng": None, "decision_lat": None, "decision_address": None}
    if decision["decision"] != "adopted":
        return empty
    if decision["mode"] == "manual_coordinate":
        try:
            lng, lat = float(decision["lng"]), float(decision["lat"])
        except ValueError:
            return None
        if not within_taiwan_bounds(lng, lat):
            return None
        return {"decision_mode": decision["mode"], "decision_lng": lng, "decision_lat": lat,
                "decision_address": decision["address"]}
    allowed = {row["tgos_result_id"], *json.loads(row["prior_result_ids"] or "[]")} - {None}
    result = results_by_id.get(decision["tgos_result_id"])
    if (decision["tgos_result_id"] not in allowed or result is None or result["lng"] is None
            or result["lat"] is None or not within_taiwan_bounds(result["lng"], result["lat"])):
        return None
    return {"decision_mode": decision["mode"], "decision_lng": result["lng"], "decision_lat": result["lat"],
            "decision_address": result["response_address"]}

