"""Bounded Arrow writes, typed reads and disk-backed relation checks for P1."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from .contracts import observation_id, validate_rows


def _schema(name, fields):
    return pa.schema([pa.field(key, kind, nullable=nullable) for key, kind, nullable in fields],
                     metadata={b"lvr.dataset": name.encode(), b"lvr.schema_version": b"1.0"})


S, I = pa.string(), pa.int64()
SCHEMAS = {
    "ingest-record": _schema("ingest-record", [(key, kind, False) for key, kind in [
        ("raw_record_id", S), ("input_sha256", S), ("src_batch", S), ("category", S),
        ("member_path", S), ("source_row_number", I), ("source_line_end", I),
        ("raw_fields_json", S), ("raw_values_json", S), ("header_json", S), ("row_status", S)]]),
    "observation": _schema("observation", [
        ("raw_record_id", S, False), ("schema_version", S, False), ("category", S, False),
        ("src_batch", S, False), ("source_serial", S, True), ("input_sha256", S, False),
        ("member_path", S, False), ("source_row_number", I, False), ("record_grain", S, False),
        ("transaction_key", S, True), ("raw_address", S, False), ("tx_date_raw", S, True),
        ("tx_yyyymm", I, True), ("run_cutoff_yyyymm", I, False), ("amount_minor", I, True),
        ("area_m2_decimal", S, True), ("props_json", S, False), ("parse_status", S, False),
        ("currency", S, False), ("amount_scale", I, False)]),
    "address-component": _schema("address-component", [
        ("raw_record_id", S, False), ("component_id", S, False), ("ordinal", I, False),
        ("normalized_address", S, False), ("key_version", S, False), ("building_key", S, True),
        ("legacy_building_key", S, True), ("expansion_status", S, False)]),
    "exclusion": _schema("exclusion", [("raw_record_id", S, False), ("reason", S, False), ("source_ref", S, False)]),
    "diagnostic": _schema("diagnostic", [("raw_record_id", S, False), ("code", S, False),
                                           ("detail", S, False), ("source_ref", S, False)]),
    "disposition": _schema("disposition", [(key, kind, False) for key, kind in [
        ("raw_record_id", S), ("input_sha256", S), ("src_batch", S), ("category", S),
        ("member_path", S), ("source_row_number", I), ("source_line_end", I),
        ("raw_fields_json", S), ("raw_values_json", S), ("header_json", S), ("row_status", S),
        ("outcome", S), ("reason", S)]])
}


class BatchWriter:
    def __init__(self, path: Path, dataset: str, batch_rows: int = 1024, *, max_bytes: int = 8 * 2**20):
        if isinstance(batch_rows, bool) or not 1 <= batch_rows <= 100_000:
            raise ValueError("batch_rows must be between 1 and 100000")
        self.path, self.dataset, self.batch_rows = Path(path), dataset, batch_rows
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("Invalid Arrow byte budget")
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raise FileExistsError(self.path)
        self.schema = SCHEMAS[dataset]
        self.writer = pq.ParquetWriter(self.path, self.schema, compression="zstd")
        self.buffer = []
        self.row_count = self.max_buffer_rows = 0
        self.buffer_bytes = self.max_buffer_bytes = 0

    def add(self, row: dict):
        if set(row) != set(self.schema.names):
            raise ValueError("Row fields differ from declared Arrow schema")
        if any(row[field.name] is None and not field.nullable for field in self.schema):
            raise ValueError("Null in nonnullable Arrow field")
        size = sum(len(value.encode("utf-8")) if isinstance(value, str) else 8 for value in row.values())
        if size > self.max_bytes:
            raise ValueError("Single record exceeds Arrow byte budget")
        if self.buffer_bytes + size > self.max_bytes:
            self.flush()
        self.buffer.append(row)
        self.buffer_bytes += size
        self.max_buffer_bytes = max(self.max_buffer_bytes, self.buffer_bytes)
        self.max_buffer_rows = max(self.max_buffer_rows, len(self.buffer))
        if len(self.buffer) >= self.batch_rows:
            self.flush()

    def flush(self):
        if self.buffer:
            self.writer.write_table(pa.Table.from_pylist(self.buffer, schema=self.schema))
            self.row_count += len(self.buffer)
            self.buffer.clear()
            self.buffer_bytes = 0

    def close(self):
        self.flush()
        self.writer.close()


def batches(path: Path, batch_rows: int = 1024):
    parquet = pq.ParquetFile(path)
    # Keep producer row-group byte boundaries rather than merging groups.
    for group in range(parquet.num_row_groups):
        yield from parquet.iter_batches(batch_size=batch_rows, row_groups=[group])


def rows(path: Path, batch_rows: int = 1024):
    for batch in batches(path, batch_rows):
        yield from batch.to_pylist()


def inspect_parquet(path: Path, dataset: str) -> int:
    parquet = pq.ParquetFile(path)
    if dataset not in SCHEMAS or not parquet.schema_arrow.equals(SCHEMAS[dataset], check_metadata=True):
        raise ValueError("Parquet Arrow schema/metadata mismatch")
    count = 0
    for batch in batches(path):
        values = batch.to_pylist()
        if dataset in {"ingest-record", "disposition"}:
            for row in values:
                if row["raw_record_id"] != observation_id(row["input_sha256"], row["member_path"], row["source_row_number"]):
                    raise ValueError("Raw/disposition identity mismatch")
                if row["source_line_end"] < row["source_row_number"] or row["row_status"] not in {"parsed", "failed"}:
                    raise ValueError("Raw row lineage/status invalid")
                if row["category"] not in {"sales", "presale", "rent"}:
                    raise ValueError("Unknown category")
                if not isinstance(json.loads(row["raw_fields_json"]), dict):
                    raise ValueError("Original fields must be an object")
                for key in ["raw_values_json", "header_json"]:
                    if not isinstance(json.loads(row[key]), list):
                        raise ValueError("Original CSV values/header must be arrays")
                if dataset == "disposition" and row["outcome"] not in {"retained", "excluded", "failed"}:
                    raise ValueError("Unknown disposition")
        else:
            if dataset == "observation":
                for row in values:
                    row["props_json"] = json.loads(row["props_json"])
            validate_rows(values, dataset)
        count += len(values)
    if count != parquet.metadata.num_rows:
        raise ValueError("Parquet footer count mismatch")
    return count


def verify_relations(groups: dict[str, list[Path]], *, source_scope: dict | None = None, cutoff: int | None = None) -> dict:
    """Validate across all partitions without collecting IDs or rows in Python."""
    counts = {}
    with tempfile.TemporaryDirectory(prefix="lvr-relations-") as spill:
        with duckdb.connect(config={"threads": "2", "memory_limit": "256MB", "temp_directory": spill,
                                    "max_temp_directory_size": "1GiB"}) as db:
            for dataset, paths in groups.items():
                name = dataset.replace("-", "_")
                db.read_parquet([str(path) for path in paths]).create_view(name)
                counts[dataset] = db.sql(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                primary = "component_id" if dataset == "address-component" else "raw_record_id"
                if dataset != "diagnostic":
                    if db.sql(f'SELECT count(*) FROM (SELECT "{primary}" FROM "{name}" GROUP BY 1 HAVING count(*) > 1)').fetchone()[0]:
                        raise ValueError(f"Duplicate {primary} across {dataset} partitions")

            def reject(sql, message):
                if db.sql(sql).fetchone()[0]:
                    raise ValueError(message)

            if source_scope is not None:
                if not isinstance(source_scope, dict) or not source_scope:
                    raise ValueError("Declared source scope required")
                db.register("declared_scope", pa.table({"src_batch": list(source_scope), "input_sha256": list(source_scope.values())}))
                for name in ["ingest-record", "observation", "disposition"]:
                    if name in groups:
                        view = name.replace("-", "_")
                        reject(f'SELECT count(*) FROM "{view}" ANTI JOIN declared_scope USING(src_batch,input_sha256)', "Rows differ from declared source scope")
            if cutoff is not None:
                if isinstance(cutoff, bool) or not isinstance(cutoff, int):
                    raise ValueError("Invalid declared cutoff")
                if "observation" in groups:
                    reject(f"SELECT count(*) FROM observation WHERE run_cutoff_yyyymm<>{cutoff}", "Observation cutoff differs from declared configuration")

            if "address-component" in groups:
                if "observation" not in groups:
                    raise ValueError("Components require observations")
                reject("SELECT count(*) FROM address_component c ANTI JOIN observation o USING(raw_record_id)", "Orphan component")
                reject("SELECT count(*) FROM (SELECT raw_record_id, ordinal FROM address_component GROUP BY 1,2 HAVING count(*)>1)", "Duplicate component ordinal")
            if "observation" in groups and "exclusion" in groups:
                reject("SELECT count(*) FROM observation JOIN exclusion USING(raw_record_id)", "Observation also excluded")
            if "disposition" in groups:
                required = {"observation", "address-component", "exclusion", "diagnostic"}
                if not required.issubset(groups):
                    raise ValueError("Converted snapshots require all datasets")
                reject("SELECT count(*) FROM observation o ANTI JOIN disposition d USING(raw_record_id)", "Observation lacks lineage")
                reject("SELECT count(*) FROM observation o ANTI JOIN address_component c USING(raw_record_id)", "Observation lacks address component")
                reject("SELECT count(*) FROM exclusion e ANTI JOIN disposition d USING(raw_record_id)", "Exclusion lacks lineage")
                reject("SELECT count(*) FROM diagnostic e ANTI JOIN disposition d USING(raw_record_id)", "Diagnostic lacks lineage")
                reject("SELECT count(*) FROM disposition d LEFT JOIN observation o USING(raw_record_id) LEFT JOIN exclusion e USING(raw_record_id) WHERE (d.outcome='retained' AND (o.raw_record_id IS NULL OR e.raw_record_id IS NOT NULL)) OR (d.outcome='excluded' AND (e.raw_record_id IS NULL OR o.raw_record_id IS NOT NULL)) OR (d.outcome='failed' AND (o.raw_record_id IS NOT NULL OR e.raw_record_id IS NOT NULL))", "Disposition/outcome mismatch")
                reject("SELECT count(*) FROM observation o JOIN disposition d USING(raw_record_id) WHERE o.input_sha256<>d.input_sha256 OR o.member_path<>d.member_path OR o.source_row_number<>d.source_row_number OR o.category<>d.category OR o.src_batch<>d.src_batch", "Observation lineage differs")
                reject("SELECT count(*) FROM disposition d ANTI JOIN diagnostic e USING(raw_record_id) WHERE d.outcome='failed'", "Failed row lacks diagnostic")
    return counts
