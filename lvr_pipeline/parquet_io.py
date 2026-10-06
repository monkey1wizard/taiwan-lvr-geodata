"""Bounded Arrow writes, typed reads and disk-backed relation checks for P1."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from .contracts import observation_id, validate_rows
from .contracts.schemas import P1_DATASETS, PRIMARY, SCHEMAS as ALL_SCHEMAS, dataset_schema
from .contracts.validate import validate_dataset_rows

# Retained symbol: the transaction (P1) datasets only; all datasets live in contracts.schemas.
SCHEMAS = {name: ALL_SCHEMAS[name] for name in P1_DATASETS}


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
        self.schema = dataset_schema(dataset)
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
    if not parquet.schema_arrow.equals(dataset_schema(dataset), check_metadata=True):
        raise ValueError("Parquet Arrow schema/metadata mismatch")
    count = 0
    for batch in batches(path):
        values = batch.to_pylist()
        if dataset not in P1_DATASETS:
            validate_dataset_rows(values,dataset)
        elif dataset in {"ingest-record", "disposition"}:
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


def duckdb_config(spill=None) -> dict:
    """DuckDB limits; defaults stay small, full-history runs may raise them."""
    config = {"threads": os.environ.get("LVR_DUCKDB_THREADS", "2"),
              "memory_limit": os.environ.get("LVR_DUCKDB_MEMORY_LIMIT", "256MB")}
    if spill is not None:
        config["temp_directory"] = str(spill)
        config["max_temp_directory_size"] = os.environ.get("LVR_DUCKDB_TEMP_LIMIT", "1GiB")
    return config


def verify_relations(groups: dict[str, list[Path]], *, source_scope: dict | None = None, cutoff: int | None = None) -> dict:
    """Validate across all partitions without collecting IDs or rows in Python."""
    counts = {}
    with tempfile.TemporaryDirectory(prefix="lvr-relations-") as spill:
        with duckdb.connect(config=duckdb_config(spill)) as db:
            for dataset, paths in groups.items():
                name = dataset.replace("-", "_")
                db.read_parquet([str(path) for path in paths]).create_view(name)
                counts[dataset] = db.sql(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                if dataset not in P1_DATASETS:
                    columns=','.join('"'+key+'"' for key in PRIMARY[dataset])
                    if db.sql(f'SELECT count(*) FROM (SELECT {columns} FROM "{name}" GROUP BY {columns} HAVING count(*) > 1)').fetchone()[0]:
                        raise ValueError(f"Duplicate offline identity in {dataset}")
                    continue
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
            if "address-occurrence" in groups and "address-pool" in groups:
                reject("SELECT count(*) FROM address_occurrence a ANTI JOIN address_pool p USING(key_version,building_key) WHERE a.building_key IS NOT NULL", "Occurrence absent from address pool")
            if "address-result" in groups and "address-pool" in groups:
                reject("SELECT count(*) FROM address_result a ANTI JOIN address_pool p USING(key_version,building_key)", "Result absent from address pool")
                reject("SELECT count(*) FROM address_pool p ANTI JOIN address_result a USING(key_version,building_key)", "Address pool missing result")
            if "address-result" in groups and "offline-row" in groups:
                reject("SELECT count(*) FROM address_result a LEFT JOIN offline_row e USING(evidence_id) WHERE a.status='located' AND (e.evidence_id IS NULL OR e.validity<>'valid' OR e.building_key<>a.building_key OR e.lng<>a.lng OR e.lat<>a.lat)", "Located evidence mismatch")
                reject("SELECT count(*) FROM (SELECT building_key,count(*) coordinate_count,sum(n) evidence_count FROM (SELECT building_key,lng,lat,count(*) n FROM offline_row WHERE validity='valid' GROUP BY 1,2,3) GROUP BY 1) e FULL JOIN address_result a USING(building_key) WHERE coalesce(e.coordinate_count,0)<>coalesce(a.coordinate_count,0) OR coalesce(e.evidence_count,0)<>coalesce(a.evidence_count,0)", "Resolution counts differ from evidence")
            if "unmatched-address" in groups and "address-result" in groups:
                reject("SELECT count(*) FROM ((SELECT * FROM address_result WHERE status<>'located' EXCEPT SELECT * FROM unmatched_address) UNION ALL (SELECT * FROM unmatched_address EXCEPT SELECT * FROM address_result WHERE status<>'located'))", "Unmatched selection differs from state")
            if "tgos-query" in groups:
                if "tgos-batch" not in groups:
                    raise ValueError("TGOS queries require batches")
                reject("SELECT count(*) FROM tgos_query q ANTI JOIN tgos_batch b USING(batch_id)", "TGOS query lacks batch")
                reject("SELECT count(*) FROM (SELECT batch_id,address FROM tgos_query GROUP BY 1,2 HAVING count(*)>1)", "TGOS batch Address is ambiguous")
                reject("SELECT count(*) FROM (SELECT batch_id,count(*) n FROM tgos_query GROUP BY 1) q JOIN tgos_batch b USING(batch_id) WHERE q.n<>b.address_count", "TGOS batch count differs from queries")
            if "tgos-result" in groups:
                if "tgos-query" not in groups:
                    raise ValueError("TGOS results require queries")
                reject("SELECT count(*) FROM tgos_result r ANTI JOIN tgos_query q USING(batch_id,query_fingerprint)", "TGOS result lacks submitted query")
            if "verified-alias" in groups:
                reject("SELECT count(*) FROM verified_alias WHERE alias_key=target_key", "Verified alias points to itself")
            if "alias-event" in groups and "verified-alias" in groups:
                reject("SELECT count(*) FROM verified_alias a WHERE NOT EXISTS (SELECT 1 FROM alias_event e WHERE e.alias_key=a.alias_key AND e.target_key=a.target_key AND e.action='verified')", "Verified alias lacks event evidence")
            if "address-patch-provenance" in groups:
                if "address-patch" not in groups:
                    raise ValueError("Address patch provenance requires patch rows")
                reject("SELECT count(*) FROM address_patch_provenance p ANTI JOIN address_patch a USING(patch_id)", "Address patch provenance lacks patch row")
                reject("SELECT count(*) FROM address_patch a WHERE NOT EXISTS (SELECT 1 FROM address_patch_provenance p WHERE p.patch_id=a.patch_id)", "Address patch lacks provenance")
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
