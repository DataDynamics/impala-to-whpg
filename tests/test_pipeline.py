"""가짜 DB-API 연결로 파이프라인 전체 흐름을 검증한다."""

from typing import Any, List, Sequence

import pytest

from impala_to_greenplum.config import GreenplumConfig, JobConfig
from impala_to_greenplum.pipeline import run_job
from impala_to_greenplum.target import GreenplumTarget


class FakeCursor:
    def __init__(self, conn: "FakeConnection") -> None:
        self._conn = conn
        self.rowcount = 0

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self._conn.statements.append(" ".join(sql.split()))
        if "to_regclass" in sql:
            self._conn.result = [(1 if self._conn.table_exists else None,)]
        elif "information_schema.columns" in sql:
            self._conn.result = [(c,) for c in self._conn.target_columns]
        else:
            self._conn.result = []
            self.rowcount = self._conn.affected_rows

    def fetchone(self):
        return self._conn.result[0] if self._conn.result else None

    def fetchall(self):
        return self._conn.result

    def copy_expert(self, sql: str, stream: Any, size: int = 8192) -> None:
        self._conn.statements.append(" ".join(sql.split()))
        payload = b""
        while True:
            chunk = stream.read(size)
            if not chunk:
                break
            payload += chunk
        self._conn.copied.append((sql, payload))

    def close(self) -> None:
        return None


class FakeConnection:
    def __init__(self, table_exists: bool = True, target_columns: Sequence[str] = ()) -> None:
        self.statements: List[str] = []
        self.copied: List[Any] = []
        self.result: List[Any] = []
        self.table_exists = table_exists
        self.target_columns = list(target_columns)
        self.affected_rows = 0
        self.committed = 0
        self.rolled_back = 0
        self.autocommit = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        return None


class FakeSource:
    def __init__(self, columns, rows) -> None:
        self.columns = columns
        self.rows = rows

    def describe(self, sql: str):
        return self.columns

    def iter_rows(self, sql: str, batch_size: int):
        yield from self.rows


def make_target(conn: FakeConnection) -> GreenplumTarget:
    target = GreenplumTarget(GreenplumConfig(host="gp", database="dw", user="etl", schema="staging"))
    target._conn = conn
    return target


COLUMNS = [("order_id", "bigint"), ("name", "string"), ("amount", "decimal(18,2)")]
ROWS = [(1, "김철수", None), (2, "tab\there", 10.5)]


def test_append_mode_copies_rows():
    conn = FakeConnection(table_exists=True, target_columns=["order_id", "name", "amount"])
    result = run_job(
        JobConfig(query="SELECT * FROM t", target_table="orders", mode="append"),
        FakeSource(COLUMNS, ROWS),
        make_target(conn),
    )

    sql, payload = conn.copied[0]
    assert sql.startswith('COPY "staging"."orders" ("order_id", "name", "amount") FROM STDIN')
    assert payload.decode() == "1\t김철수\t\\N\n2\ttab\\there\t10.5\n"
    assert result.rows_inserted == 2
    assert conn.committed == 1
    assert not any(s.startswith("TRUNCATE") for s in conn.statements)


def test_truncate_mode_truncates_before_copy():
    conn = FakeConnection(table_exists=True, target_columns=["order_id", "name", "amount"])
    run_job(
        JobConfig(query="SELECT * FROM t", target_table="orders", mode="truncate"),
        FakeSource(COLUMNS, ROWS),
        make_target(conn),
    )
    truncate_index = next(i for i, s in enumerate(conn.statements) if s.startswith("TRUNCATE"))
    copy_index = next(i for i, s in enumerate(conn.statements) if s.startswith("COPY"))
    assert truncate_index < copy_index


def test_replace_mode_drops_and_creates():
    conn = FakeConnection(table_exists=True, target_columns=["order_id", "name", "amount"])
    run_job(
        JobConfig(
            query="SELECT * FROM t",
            target_table="orders",
            mode="replace",
            distributed_by=["order_id"],
        ),
        FakeSource(COLUMNS, ROWS),
        make_target(conn),
    )
    assert any(s.startswith('DROP TABLE IF EXISTS "staging"."orders"') for s in conn.statements)
    create = next(s for s in conn.statements if s.startswith("CREATE TABLE"))
    assert '"amount" numeric(18,2)' in create
    assert 'DISTRIBUTED BY ("order_id")' in create


def test_missing_table_is_created_when_allowed():
    conn = FakeConnection(table_exists=False, target_columns=["order_id", "name", "amount"])
    run_job(
        JobConfig(query="SELECT * FROM t", target_table="orders"),
        FakeSource(COLUMNS, ROWS),
        make_target(conn),
    )
    assert any(s.startswith("CREATE TABLE IF NOT EXISTS") for s in conn.statements)


def test_missing_table_raises_when_not_allowed():
    conn = FakeConnection(table_exists=False)
    with pytest.raises(RuntimeError, match="없습니다"):
        run_job(
            JobConfig(
                query="SELECT * FROM t",
                target_table="orders",
                create_target_if_missing=False,
            ),
            FakeSource(COLUMNS, ROWS),
            make_target(conn),
        )
    assert conn.rolled_back == 1


def test_upsert_stages_then_merges():
    conn = FakeConnection(table_exists=True, target_columns=["order_id", "name", "amount"])
    conn.affected_rows = 2
    result = run_job(
        JobConfig(
            query="SELECT * FROM t",
            target_table="orders",
            mode="upsert",
            key_columns=["order_id"],
        ),
        FakeSource(COLUMNS, ROWS),
        make_target(conn),
    )

    staging_create = next(s for s in conn.statements if s.startswith("CREATE TEMP TABLE"))
    assert 'LIKE "staging"."orders"' in staging_create
    assert "ON COMMIT DROP" in staging_create
    assert 'DISTRIBUTED BY ("order_id")' in staging_create

    copy_sql = conn.copied[0][0]
    assert '"staging"."orders"' not in copy_sql  # 스테이징 테이블로 적재해야 한다

    delete = next(s for s in conn.statements if s.startswith("DELETE FROM"))
    assert 't."order_id" = s."order_id"' in delete
    insert = next(s for s in conn.statements if s.startswith("INSERT INTO"))
    assert '"staging"."orders" ("order_id", "name", "amount")' in insert
    assert conn.statements.index(delete) < conn.statements.index(insert)

    assert result.rows_read == 2
    assert result.rows_deleted == 2
    assert conn.committed == 1


def test_column_not_in_target_raises():
    conn = FakeConnection(table_exists=True, target_columns=["order_id", "name"])
    with pytest.raises(ValueError, match="amount"):
        run_job(
            JobConfig(query="SELECT * FROM t", target_table="orders"),
            FakeSource(COLUMNS, ROWS),
            make_target(conn),
        )
    assert conn.rolled_back == 1


def test_target_columns_mapping_is_used():
    conn = FakeConnection(table_exists=True, target_columns=["a", "b", "c"])
    run_job(
        JobConfig(
            query="SELECT * FROM t",
            target_table="orders",
            target_columns=["a", "b", "c"],
        ),
        FakeSource(COLUMNS, ROWS),
        make_target(conn),
    )
    assert '("a", "b", "c")' in conn.copied[0][0]


def test_target_columns_count_mismatch_raises():
    conn = FakeConnection(table_exists=True, target_columns=["a", "b"])
    with pytest.raises(ValueError, match="개수"):
        run_job(
            JobConfig(query="SELECT * FROM t", target_table="orders", target_columns=["a", "b"]),
            FakeSource(COLUMNS, ROWS),
            make_target(conn),
        )


def test_source_failure_rolls_back():
    conn = FakeConnection(table_exists=True, target_columns=["order_id", "name", "amount"])

    class ExplodingSource(FakeSource):
        def iter_rows(self, sql, batch_size):
            yield (1, "a", 1)
            raise RuntimeError("Impala 연결 끊김")

    with pytest.raises(RuntimeError, match="Impala 연결 끊김"):
        run_job(
            JobConfig(query="SELECT * FROM t", target_table="orders"),
            ExplodingSource(COLUMNS, ROWS),
            make_target(conn),
        )
    assert conn.rolled_back == 1
    assert conn.committed == 0


def test_analyze_can_be_disabled():
    conn = FakeConnection(table_exists=True, target_columns=["order_id", "name", "amount"])
    run_job(
        JobConfig(query="SELECT * FROM t", target_table="orders", analyze_after_load=False),
        FakeSource(COLUMNS, ROWS),
        make_target(conn),
    )
    assert not any(s.startswith("ANALYZE") for s in conn.statements)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
