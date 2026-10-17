"""Final state (R10-1b): the located doors the output reads, with TGOS and owner decisions applied.

The stage sits after review and before output. It starts from the address status of the offline
state, or of the TGOS state when one is given, and overlays the ``adopted`` rows of the second
manual-review area (owner decisions of 2026-10-18):

- A review key that is not located and has an ``adopted`` decision becomes ``located`` at the decision
  coordinate (source ``manual``). The evidence id of that coordinate is
  ``digest(["manual", review_key, decision_file, decision_row])``. The evidence counts stay those of
  the status the stage started from; they count offline evidence rows, not owner decisions.
- A ``tgos_manual_conflict`` key (located, with an ``adopted`` decision) compares the two coordinates
  by the haversine distance of ``results.reconcile.haversine_m`` (sphere of radius 6371008.8 m).
  Within ``coordinate_tolerance_m`` the located coordinate is kept (source ``tgos``). Beyond it the key
  gets status ``conflict`` with no coordinate: neither side is adopted and its doors are not output.
- A ``district_query`` key has no address-result row. Its doors (``second_review_observations``) get
  the decision coordinate and address through ``coordinate_sources.parquet`` (source ``manual``).

Outputs: ``address_index.parquet`` (dataset final-address-result: the columns of address-result),
``coordinate_sources.parquet`` (dataset coordinate-source: one row per door whose coordinate or address differs from the offline state; its
``after_*`` columns are what the output uses for those doors) and ``affected_months.json``. Only doors
listed in the coordinate sources get a new ``address``: the TGOS response address of the adopted
result, or the decision address. The period of a door is the year and month of the output
``trade_date`` (``output.rows.roc_date`` of ``tx_date_raw``) with the category.

``affected_months.json`` compares the located doors with the baseline (a previous final state, tgos state or
offline state; this run's offline state when none is given):
``added`` is located now and not in the baseline, ``updated`` is located in both with another coordinate
or address, ``removed`` is located in the baseline and not now.
Any removed door stops the stage before it writes a snapshot (published rows are not allowed to
disappear). Nothing here changes a ``building_key`` or merges keys.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

from ..output.catalog import CATALOG_NAME, PARQUET_NAME
from ..output.rows import doors_select, make_transaction_id, roc_date
from ..storage.parquet import BatchWriter, duckdb_config, rows
from ..storage.runs import PRODUCER_CONFIGS, Stage, bindings, digest, load_snapshot, sha256_file
from ..storage.spill import measured_spill
from .reconcile import haversine_m, load_coordinate_tolerance, load_p2
from .review import _paths

DISTANCE_RULE = "haversine, sphere radius 6371008.8 m, X=longitude Y=latitude"
CONFLICT_REASON = "tgos_manual_conflict"
KINDS = ("final-state", "tgos-state", "offline-state")


def _stage_kind(snapshot: Path) -> str:
    return json.loads((Path(snapshot) / "quality.json").read_text(encoding="utf-8"))["stage"]


def _one(snapshot: Path, manifest: dict, schema: str) -> Path:
    found = _paths(snapshot, manifest, schema)
    if len(found) != 1:
        raise ValueError(f"Expected one {schema} file in {snapshot}, found {len(found)}")
    return Path(found[0])


def _door_table(db, name: str, results: str, overrides: str) -> None:
    """Located doors with their output address: the override, else the source rule of make_row."""
    db.execute(
        f"""CREATE TABLE {name} AS
        SELECT d.component_id, d.raw_record_id, d.ordinal + 1 door_index, d.lng, d.lat,
               CASE WHEN d.door_count = 1 THEN o.raw_address ELSE d.normalized_address END base_address,
               coalesce(d.address_override,
                        CASE WHEN d.door_count = 1 THEN o.raw_address ELSE d.normalized_address END) address
        FROM ({doors_select(results, overrides)}) d JOIN observations o USING (raw_record_id)"""
    )


def _period(row: dict) -> tuple[int, int]:
    date = roc_date(row["tx_date_raw"])
    if date is None:
        raise ValueError(f"Door of {row['transaction_id']} has no valid trade_date and cannot be placed in a period")
    return int(date[:4]), int(date[5:7])


def _csv_path(year: int, month: int, category: str) -> str:
    return f"csv/{year:04d}/taiwanlvr_{year:04d}{month:02d}_{category}.csv"


def build_final_state(
    converted: Path,
    state: Path,
    work_dir: Path,
    *,
    review: Path,
    tgos_state: Path | None = None,
    baseline: Path | None = None,
    config: Path | None = None,
    run_id: str | None = None,
) -> Path:
    converted, state, review = Path(converted), Path(state), Path(review)
    converted_manifest, _ = load_snapshot(converted, "converted")
    converted_hash = sha256_file(converted / "manifest.json")
    state_manifest, state_report = load_p2(state, "offline-state")
    if state_report["converted_snapshot_sha256"] != converted_hash:
        raise ValueError("Offline state was not built from this converted snapshot")
    state_hash = sha256_file(state / "manifest.json")
    hashes = [converted_hash, state_hash]
    status_snapshot, status_manifest, tgos_hash = state, state_manifest, None
    if tgos_state is not None:
        tgos_state = Path(tgos_state)
        status_manifest, tgos_report = load_p2(tgos_state, "tgos-state")
        if (
            tgos_report["converted_snapshot_sha256"] != converted_hash
            or tgos_report["address_source_commit"] != state_report["address_source_commit"]
        ):
            raise ValueError("TGOS state comes from other inputs")
        status_snapshot, tgos_hash = tgos_state, sha256_file(tgos_state / "manifest.json")
        hashes.append(tgos_hash)
    review_manifest, review_report = load_p2(review, "review")
    review_hash = sha256_file(review / "manifest.json")
    if (
        review_report["converted_snapshot_sha256"] != converted_hash
        or review_report["offline_state_manifest_sha256"] != state_hash
        or review_report["tgos_state_manifest_sha256"] != tgos_hash
    ):
        raise ValueError("Review was not built from this converted snapshot, offline state and TGOS state")
    hashes.append(review_hash)
    # Without a baseline the doors are compared with this run's offline state (version 1 was published from
    # offline matching only), so TGOS adoptions count as added.
    baseline_kind, baseline_hash = "offline-state", state_hash
    if baseline is not None:
        baseline = Path(baseline)
        baseline_kind = _stage_kind(baseline)
        if baseline_kind not in KINDS:
            raise ValueError(f"Baseline must be a final-state, tgos-state or offline-state snapshot, not {baseline_kind}")
        baseline_manifest, baseline_report = load_p2(baseline, baseline_kind)
        baseline_hash = sha256_file(baseline / "manifest.json")
        hashes.append(baseline_hash)
    else:
        baseline_manifest = state_manifest
    tolerance = load_coordinate_tolerance(config)
    binding = bindings(
        "final-state",
        {"coordinate_tolerance_m": tolerance, "distance": DISTANCE_RULE, "conflict_reason": CONFLICT_REASON,
         "tgos_state": tgos_state is not None, "baseline_kind": baseline_kind},
        hashes,
    )

    # The baseline comes from an earlier run: its converted snapshot differs, so doors are compared by
    # transaction_id and door_index. It must come from the same program (the producer file hashes of
    # its quality.json against this run's) and the same rebuild epoch.
    baseline_run_id = None
    if baseline is not None:
        ours = PRODUCER_CONFIGS[binding["config_sha256"]]["producer_files"]
        theirs = baseline_report["producer_config"]["producer_files"]
        if ours != theirs:
            raise ValueError("Baseline was built by a different program version (producer file hashes differ)")
        for name, manifest in (("baseline", baseline_manifest), ("converted", converted_manifest),
                               ("offline state", state_manifest)):
            if manifest["bindings"]["rebuild_epoch"] != binding["rebuild_epoch"]:
                raise ValueError(f"Rebuild epoch of the {name} differs from this run")
        baseline_run_id = baseline_report.get("run_id")
    baseline_fingerprint = digest(theirs) if baseline is not None else None

    second = list(rows(_one(review, review_manifest, "second-review")))
    adopted = [r for r in second if r["status"] == "adopted"]
    manual = {r["review_key"]: r for r in adopted
              if r["key_kind"] == "building_key" and r["entry_reason"] != CONFLICT_REASON}
    conflicts = {r["review_key"]: r for r in adopted if r["entry_reason"] == CONFLICT_REASON}
    districts = [r for r in adopted if r["key_kind"] == "district_query"]

    tgos_results: dict[str, list[dict]] = {}
    key_results: dict[str, list[dict]] = {}
    all_results: dict[str, dict] = {}
    if tgos_state is not None:
        queries = {(q["batch_id"], q["query_fingerprint"]): q for path in _paths(
            tgos_state, status_manifest, "tgos-query") for q in rows(path)}
        for path in _paths(tgos_state, status_manifest, "tgos-result"):
            for result in rows(path):
                all_results[result["result_id"]] = result
                query = queries[(result["batch_id"], result["query_fingerprint"])]
                if query["query_kind"] == "building_key":
                    key_results.setdefault(query["building_key"], []).append(result)
                    if result["status"] == "succeeded":
                        tgos_results.setdefault(query["building_key"], []).append(result)

    scratch = Path(work_dir) / "final-state-scratch"
    with measured_spill(scratch, "final-state") as spill:
        build = spill.path / "files"
        build.mkdir()
        # Overlay of the second review on the address status.
        writer = BatchWriter(build / "address_index.parquet", "final-address-result")
        statuses = {"located": 0, "conflict": 0, "unmatched": 0, "outside_scope": 0}
        seen, kept, dropped = set(), [], []
        for path in _paths(status_snapshot, status_manifest, "address-result"):
            for row in rows(path):
                key = row["building_key"]
                if key in manual:
                    decision = manual[key]
                    if row["status"] == "located":
                        raise ValueError(f"Review key {key} is adopted manually but already located")
                    row = {**row, "status": "located", "lng": decision["decision_lng"], "lat": decision["decision_lat"],
                           "evidence_id": digest(["manual", key, decision["decision_file"], decision["decision_row"]])}
                    seen.add(key)
                elif key in conflicts:
                    decision = conflicts[key]
                    if row["status"] != "located":
                        raise ValueError(f"Review key {key} is a {CONFLICT_REASON} but not located")
                    distance = haversine_m((row["lng"], row["lat"]), (decision["decision_lng"], decision["decision_lat"]))
                    if distance <= tolerance:
                        kept.append(key)
                    else:
                        row = {**row, "status": "conflict", "lng": None, "lat": None, "evidence_id": None}
                        dropped.append(key)
                    seen.add(key)
                statuses[row["status"]] += 1
                writer.add(row)
        writer.close()
        missing = sorted((set(manual) | set(conflicts)) - seen)
        if missing:
            raise ValueError(f"{len(missing)} adopted review keys are not in the address index, first {missing[0]}")

        with duckdb.connect(config=duckdb_config(spill.path)) as db:
            def view(name, paths):
                db.read_parquet([str(p) for p in paths]).create_view(name)

            def schema_paths(snapshot, manifest, schema):
                return _paths(Path(snapshot), manifest, schema)

            view("observations", schema_paths(converted, converted_manifest, "observation"))
            view("components", schema_paths(converted, converted_manifest, "address-component"))
            view("off_results", schema_paths(state, state_manifest, "address-result"))
            view("final_results", [writer.path])
            db.execute("CREATE VIEW no_overrides AS SELECT NULL::VARCHAR component_id, NULL::VARCHAR address, "
                       "NULL::DOUBLE lng, NULL::DOUBLE lat WHERE false")
            changed = db.execute(
                """SELECT f.building_key, f.lng, f.lat FROM final_results f
                   LEFT JOIN off_results o ON o.key_version = f.key_version AND o.building_key = f.building_key
                   WHERE f.lng IS NOT NULL
                     AND (o.lng IS NULL OR o.lat IS NULL OR f.lng <> o.lng OR f.lat <> o.lat) ORDER BY 1"""
            ).fetchall()

            # Source, address and evidence of every changed key; the tgos address is the response of the
            # latest successful result at the final coordinate.
            info: dict[str, dict] = {}
            address_missing_keys = []
            for key, lng, lat in changed:
                if key in manual:
                    decision = manual[key]
                    # The decision coordinate of accept_tgos_response is the returned one, also of a rejected result.
                    result = next((r for r in reversed(key_results.get(key, []))
                                   if (r["response_lng"], r["response_lat"])
                                   == (decision["decision_lng"], decision["decision_lat"])
                                   and r["response_address"] == decision["decision_address"]), None)
                    info[key] = {"source": "manual", "address": decision["decision_address"], "review_key": key,
                                 "tgos_result_id": result["result_id"] if result else None,
                                 "decision_file": decision["decision_file"], "decision_row": decision["decision_row"]}
                    continue
                if tgos_state is None:
                    raise ValueError(f"Key {key} changed without a TGOS state or a manual decision")
                result = next((r for r in reversed(tgos_results.get(key, [])) if (r["lng"], r["lat"]) == (lng, lat)), None)
                if result is None:
                    address_missing_keys.append(key)
                info[key] = {"source": "tgos", "address": result["response_address"] if result else None,
                             "review_key": key if key in conflicts else None,
                             "tgos_result_id": result["result_id"] if result else None,
                             "decision_file": None, "decision_row": None}
            db.execute("CREATE TEMP TABLE key_over (building_key VARCHAR, address VARCHAR, lng DOUBLE, lat DOUBLE)")
            coordinates = {key: (lng, lat) for key, lng, lat in changed}
            if coordinates:
                db.executemany("INSERT INTO key_over VALUES (?, ?, ?, ?)",
                               [(key, info[key]["address"], *coordinates[key]) for key in coordinates])
            # District doors: no result row, the decision gives coordinate and address.
            district_doors: dict[str, dict] = {}
            if districts:
                by_id = {}
                for decision in districts:
                    result = next((r for r in all_results.values()
                                   if decision["decision_mode"] == "accept_tgos_response"
                                   and r["query_fingerprint"] == decision["query_fingerprint"]
                                   and (r["response_lng"], r["response_lat"])
                                   == (decision["decision_lng"], decision["decision_lat"])),
                                  None)
                    by_id[decision["review_id"]] = {
                        "source": "manual", "address": decision["decision_address"],
                        "review_key": decision["review_key"],
                        "tgos_result_id": result["result_id"] if result else None,
                        "decision_file": decision["decision_file"], "decision_row": decision["decision_row"],
                        "lng": decision["decision_lng"], "lat": decision["decision_lat"]}
                for path in _paths(review, review_manifest, "second-review-observation"):
                    for observation in rows(path):
                        if observation["review_id"] in by_id:
                            district_doors[observation["component_id"]] = by_id[observation["review_id"]]
            db.execute("CREATE TEMP TABLE district_over (component_id VARCHAR, address VARCHAR, lng DOUBLE, lat DOUBLE)")
            if district_doors:
                db.executemany("INSERT INTO district_over VALUES (?, ?, ?, ?)",
                               [(c, d["address"], d["lng"], d["lat"]) for c, d in sorted(district_doors.items())])
            # A changed key whose TGOS response address is unknown keeps the door address of the source rule.
            db.execute(
                """CREATE TABLE new_overrides AS
                   SELECT c.component_id, k.address, k.lng, k.lat FROM components c
                   JOIN key_over k ON k.building_key = c.building_key
                   UNION ALL SELECT component_id, address, lng, lat FROM district_over"""
            )
            _door_table(db, "after_doors", "final_results", "new_overrides")
            _door_table(db, "off_doors", "off_results", "no_overrides")
            if baseline_kind == "final-state":
                view("base_results", schema_paths(baseline, baseline_manifest, "final-address-result"))
                view("base_sources", schema_paths(baseline, baseline_manifest, "coordinate-source"))
                # Rows of the baseline coordinate sources are matched to this run's components by door identity.
                db.execute(
                    """CREATE VIEW base_overrides AS
                       SELECT c.component_id, s.after_address address, s.after_lng lng, s.after_lat lat
                       FROM base_sources s
                       JOIN (SELECT c.component_id, c.ordinal + 1 door_index,
                                    o.src_batch || '/' || o.member_path || '/' || o.source_row_number transaction_id
                             FROM components c JOIN observations o USING (raw_record_id)) c
                         ON c.transaction_id = s.transaction_id AND c.door_index = s.door_index""")
                _door_table(db, "base_doors", "base_results", "base_overrides")
            else:
                view("base_results", schema_paths(baseline or state, baseline_manifest, "address-result"))
                _door_table(db, "base_doors", "base_results", "no_overrides")

            # Door-level coordinate sources.
            names = ["component_id", "raw_record_id", "door_index", "lng", "lat", "address", "base_address",
                     "before_lng", "before_lat", "building_key", "src_batch", "member_path", "source_row_number",
                     "category", "tx_date_raw"]
            changes = db.execute(
                """SELECT n.component_id, n.raw_record_id, n.door_index, n.lng, n.lat, n.address, n.base_address,
                          o.lng, o.lat, c.building_key, ob.src_batch, ob.member_path, ob.source_row_number,
                          ob.category, ob.tx_date_raw
                   FROM after_doors n LEFT JOIN off_doors o ON o.component_id = n.component_id
                   JOIN components c ON c.component_id = n.component_id
                   JOIN observations ob ON ob.raw_record_id = n.raw_record_id
                   WHERE o.component_id IS NULL OR o.lng <> n.lng OR o.lat <> n.lat OR n.address <> n.base_address"""
            ).fetchall()
            sources = BatchWriter(build / "coordinate_sources.parquet", "coordinate-source")
            source_doors = {"tgos": 0, "manual": 0}
            address_missing_doors = 0
            output = []
            for values in changes:
                door = dict(zip(names, values))
                door["transaction_id"] = make_transaction_id(door["src_batch"], door["member_path"], door["source_row_number"])
                door["period_year"], door["period_month"] = _period(door)
                output.append(door)
            for door in sorted(output, key=lambda d: (d["transaction_id"], d["door_index"])):
                found = info.get(door["building_key"]) if door["building_key"] is not None else district_doors.get(
                    door["component_id"])
                if found is None:
                    raise ValueError(f"Door {door['transaction_id']}#{door['door_index']} changed without a recorded source")
                source_doors[found["source"]] += 1
                if found["address"] is None:
                    address_missing_doors += 1
                sources.add({
                    "transaction_id": door["transaction_id"], "door_index": door["door_index"],
                    "raw_record_id": door["raw_record_id"], "component_id": door["component_id"],
                    "building_key": door["building_key"], "review_key": found["review_key"], "source": found["source"],
                    "before_address": door["base_address"], "before_lng": door["before_lng"],
                    "before_lat": door["before_lat"], "after_address": door["address"],
                    "after_lng": door["lng"], "after_lat": door["lat"], "tgos_result_id": found["tgos_result_id"],
                    "decision_file": found["decision_file"], "decision_row": found["decision_row"],
                    "period_year": door["period_year"], "period_month": door["period_month"],
                    "category": door["category"],
                })
            sources.close()
            db.execute("CREATE TEMP TABLE outcome_keys (building_key VARCHAR, outcome VARCHAR)")
            if kept or dropped:
                db.executemany("INSERT INTO outcome_keys VALUES (?, ?)",
                               [(k, "kept_tgos") for k in kept] + [(k, "conflict") for k in dropped])
            outcome_doors = dict(db.execute(
                "SELECT outcome, count(*) FROM outcome_keys k JOIN components c USING (building_key) "
                "JOIN observations o USING (raw_record_id) GROUP BY 1").fetchall())

            # Affected months against the baseline.
            groups: dict[tuple[int, int, str], dict] = {}
            for values in db.execute(
                """SELECT n.component_id IS NULL is_removed, b.component_id IS NULL is_added,
                          ob.src_batch, ob.member_path, ob.source_row_number, ob.category, ob.tx_date_raw
                   FROM after_doors n FULL OUTER JOIN base_doors b ON b.component_id = n.component_id
                   JOIN observations ob ON ob.raw_record_id = coalesce(n.raw_record_id, b.raw_record_id)
                   WHERE n.component_id IS NULL OR b.component_id IS NULL
                      OR n.lng <> b.lng OR n.lat <> b.lat OR n.address <> b.address"""
            ).fetchall():
                removed, added, batch, member, number, category, date = values
                year, month = _period({"transaction_id": make_transaction_id(batch, member, number), "tx_date_raw": date})
                group = groups.setdefault((year, month, category), {"added": 0, "updated": 0, "removed": 0})
                group["removed" if removed else "added" if added else "updated"] += 1
        totals = {name: sum(g[name] for g in groups.values()) for name in ("added", "updated", "removed")}
        if totals["removed"]:
            # Owner decision 2026-10-18: detect and stop; the detail goes into the message (and run attempts).
            detail = ", ".join(f"{year:04d}{month:02d} {category} {group['removed']}"
                               for (year, month, category), group in sorted(groups.items()) if group["removed"])
            raise ValueError(
                f"{totals['removed']} published doors would disappear from the output; the backfill stops "
                f"(removed rows are not allowed): {detail}"
            )
        months = [
            {"year": year, "month": month, "category": category, **groups[(year, month, category)],
             "csv_path": _csv_path(year, month, category)}
            for year, month, category in sorted(groups)
        ]
        affected = {
            "baseline_kind": baseline_kind, "baseline_manifest_sha256": baseline_hash,
            "baseline_config_fingerprint": baseline_fingerprint, "baseline_run_id": baseline_run_id,
            "files": sorted(m["csv_path"] for m in months) + ([PARQUET_NAME, CATALOG_NAME] if months else []),
            "months": months, "added_total": totals["added"], "updated_total": totals["updated"],
            "removed_total": totals["removed"],
        }
        affected_path = build / "affected_months.json"
        affected_path.write_text(json.dumps(affected, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8", newline="\n")

        stage = Stage(Path(work_dir) / "final-state", "final-state", binding, run_id)
        if stage.reused:
            return stage.path
        stage.store.add(stage.id, "affected_months.json", affected_path, format_name="binary", row_count=None)
        report = {
            "converted_snapshot_sha256": converted_hash,
            "offline_state_manifest_sha256": state_hash,
            "tgos_state_manifest_sha256": tgos_hash,
            "review_manifest_sha256": review_hash,
            "baseline_kind": baseline_kind,
            "baseline_manifest_sha256": baseline_hash,
            "baseline_config_fingerprint": baseline_fingerprint,
            "baseline_run_id": baseline_run_id,
            "address_status_source": "tgos-state" if tgos_state is not None else "offline-state",
            "address_source_commit": state_report["address_source_commit"],
            "coordinate_tolerance_m": tolerance,
            "distance_method": DISTANCE_RULE,
            "run_id": stage.id,
            "status_counts": statuses,
            "decisions": {
                "manual_keys_applied": len(manual),
                "district_keys_adopted": len(districts),
                "district_doors": len(district_doors),
                "district_adoption_not_applied": 0,
                "decision_totals": review_report["second_review"]["decision_totals"],
            },
            "tgos_manual_conflict": {
                "keys": len(conflicts), "kept_tgos": len(kept), "conflict": len(dropped),
                "kept_tgos_doors": outcome_doors.get("kept_tgos", 0), "conflict_doors": outcome_doors.get("conflict", 0),
            },
            "tgos_response_address_missing": {"keys": len(address_missing_keys), "doors": address_missing_doors},
            "source_doors": source_doors,
            "coordinate_source_rows": sources.row_count,
            "affected": {"months": len(months), **totals},
            "dataset_counts": {"final-address-result": writer.row_count, "coordinate-source": sources.row_count},
        }
        return stage.finish(
            [("address_index.parquet", writer.path, "final-address-result", writer.row_count),
             ("coordinate_sources.parquet", sources.path, "coordinate-source", sources.row_count)],
            report,
        )
