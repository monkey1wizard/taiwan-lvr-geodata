from decimal import Decimal

import duckdb
import pyarrow as pa
import pyarrow.parquet as parquet
import pytest

from lvr_pipeline.snapshots import SnapshotStore
from test_p0_snapshots import BINDINGS


def test_arrow_duckdb_exact_types_and_local_snapshot(tmp_path):
    table=pa.table({"county":pa.array(["09007","09020"],type=pa.string()),
                    "amount_minor":pa.array([0,None],type=pa.int64()),
                    "area_m2":pa.array([Decimal("10.25"),None],type=pa.decimal128(18,2))})
    source=tmp_path/"typed.parquet"
    parquet.write_table(table,source)
    with duckdb.connect() as connection:
        rows=connection.execute("SELECT county, amount_minor, area_m2 FROM read_parquet(?) ORDER BY county",[str(source)]).fetchall()
    assert rows==[("09007",0,Decimal("10.25")),("09020",None,None)]
    assert parquet.ParquetFile(source).schema_arrow==table.schema
    store=SnapshotStore(tmp_path/"store")
    store.begin("arrow",expected_parent=None,bindings=BINDINGS)
    store.add("arrow","typed.parquet",source,format_name="parquet",row_count=2)
    store.publish("arrow",expected_parent=None)
    assert store.current()["snapshot_id"]=="arrow"


def test_wrong_parquet_count_stays_uncommitted(tmp_path):
    source=tmp_path/"typed.parquet"
    parquet.write_table(pa.table({"id":[1,2]}),source)
    store=SnapshotStore(tmp_path/"store")
    store.begin("bad",expected_parent=None,bindings=BINDINGS)
    with pytest.raises(ValueError,match="count"):
        store.add("bad","typed.parquet",source,format_name="parquet",row_count=3)
    assert store.current() is None
