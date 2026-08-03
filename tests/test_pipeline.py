"""가짜 DB-API 연결과 가짜 S3 클라이언트로 파이프라인 전체 흐름을 검증한다."""

import gzip
from typing import Any, List, Sequence

import pytest

from impala_to_greenplum.config import GreenplumConfig, JobConfig, S3Config
from impala_to_greenplum.pipeline import run_job
from impala_to_greenplum.s3_stage import S3Stager
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
        elif "pg_attribute" in sql:
            self._conn.result = list(self._conn.target_column_types)
        elif "information_schema.columns" in sql:
            self._conn.result = [(c,) for c, _ in self._conn.target_column_types]
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
    def __init__(self, table_exists: bool = True, target_column_types: Sequence[Any] = ()) -> None:
        self.statements: List[str] = []
        self.copied: List[Any] = []
        self.result: List[Any] = []
        self.table_exists = table_exists
        self.target_column_types = list(target_column_types)
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


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict = {}
        self.deleted: List[str] = []

    def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None):
        self.objects[key] = fileobj.read()

    def delete_objects(self, Bucket, Delete):
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)
            self.deleted.append(item["Key"])
        return {}


class FakeSource:
    def __init__(self, columns, rows) -> None:
        self.columns = columns
        self.rows = rows

    def describe(self, sql: str):
        return self.columns

    def iter_rows(self, sql: str, batch_size: int):
        yield from self.rows


COLUMNS = [("order_id", "bigint"), ("name", "string"), ("amount", "decimal(18,2)")]
TARGET_TYPES = [("order_id", "bigint"), ("name", "text"), ("amount", "numeric(18,2)")]
ROWS = [(1, "김철수", None), (2, "tab\there", 10.5)]
EXPECTED_TSV = "1\t김철수\t\\N\n2\ttab\\there\t10.5\n"


def make_target(conn: FakeConnection) -> GreenplumTarget:
    target = GreenplumTarget(GreenplumConfig(host="gp", database="dw", user="etl", schema="staging"))
    target._conn = conn
    return target


def make_stager(client: FakeS3Client, **overrides) -> S3Stager:
    options = dict(
        bucket="dw-stage",
        prefix="impala",
        endpoint="s3.ap-northeast-2.amazonaws.com",
        gp_config="/home/gpadmin/s3.conf",
        region="ap-northeast-2",
    )
    options.update(overrides)
    return S3Stager(S3Config(**options), client=client)


def copy_job(**kwargs) -> JobConfig:
    kwargs.setdefault("query", "SELECT * FROM t")
    kwargs.setdefault("target_table", "orders")
    kwargs.setdefault("load_method", "copy")
    return JobConfig(**kwargs)


def s3_job(**kwargs) -> JobConfig:
    kwargs.setdefault("load_method", "s3")
    return copy_job(**kwargs)


def uploaded_text(client: FakeS3Client) -> str:
    return "".join(
        gzip.decompress(body).decode("utf-8") for _, body in sorted(client.objects.items())
    )


# -- COPY 경로 -------------------------------------------------------------------


def test_copy_append_mode():
    conn = FakeConnection(target_column_types=TARGET_TYPES)
    result = run_job(copy_job(), FakeSource(COLUMNS, ROWS), make_target(conn))

    sql, payload = conn.copied[0]
    assert sql.startswith('COPY "staging"."orders" ("order_id", "name", "amount") FROM STDIN')
    assert payload.decode() == EXPECTED_TSV
    assert result.rows_inserted == 2
    assert conn.committed == 1
    assert not any(s.startswith("TRUNCATE") for s in conn.statements)


def test_copy_truncate_mode_truncates_before_copy():
    conn = FakeConnection(target_column_types=TARGET_TYPES)
    run_job(copy_job(mode="truncate"), FakeSource(COLUMNS, ROWS), make_target(conn))
    truncate_index = next(i for i, s in enumerate(conn.statements) if s.startswith("TRUNCATE"))
    copy_index = next(i for i, s in enumerate(conn.statements) if s.startswith("COPY"))
    assert truncate_index < copy_index


def test_copy_upsert_stages_then_merges():
    conn = FakeConnection(target_column_types=TARGET_TYPES)
    conn.affected_rows = 2
    result = run_job(
        copy_job(mode="upsert", key_columns=["order_id"]),
        FakeSource(COLUMNS, ROWS),
        make_target(conn),
    )
    copy_sql = conn.copied[0][0]
    assert '"staging"."orders"' not in copy_sql  # 스테이징 테이블로 적재해야 한다
    assert result.rows_deleted == 2


# -- S3 경로 ---------------------------------------------------------------------


def test_s3_uploads_then_inserts_from_external_table():
    conn = FakeConnection(target_column_types=TARGET_TYPES)
    conn.affected_rows = 2
    client = FakeS3Client()
    stager = make_stager(client, cleanup=False)

    result = run_job(s3_job(), FakeSource(COLUMNS, ROWS), make_target(conn), stager)

    # 1) COPY는 쓰지 않고 S3로만 올라가야 한다
    assert conn.copied == []
    assert len(client.objects) == 1
    key = next(iter(client.objects))
    assert key.startswith("impala/orders-") and key.endswith("part-00000.tsv.gz")
    assert gzip.decompress(client.objects[key]).decode("utf-8") == EXPECTED_TSV

    # 2) 외부 테이블은 대상 테이블의 실제 타입을 따라간다
    create = next(s for s in conn.statements if "CREATE READABLE EXTERNAL" in s)
    assert "TEMP TABLE" in create
    assert '"amount" numeric(18,2)' in create
    assert "s3://s3.ap-northeast-2.amazonaws.com/dw-stage/impala/orders-" in create
    assert "config=/home/gpadmin/s3.conf" in create
    assert "region=ap-northeast-2" in create
    assert r"FORMAT 'TEXT' (DELIMITER E'\t' NULL E'\\N' ESCAPE E'\\')" in create

    # 3) 외부 테이블 → 대상 테이블 INSERT 후 외부 테이블을 정리한다
    insert = next(s for s in conn.statements if s.startswith("INSERT INTO"))
    assert '"staging"."orders" ("order_id", "name", "amount")' in insert
    assert any(s.startswith("DROP EXTERNAL TABLE") for s in conn.statements)

    assert result.rows_read == 2
    assert result.rows_inserted == 2
    assert conn.committed == 1


def test_s3_splits_into_multiple_files():
    conn = FakeConnection(target_column_types=TARGET_TYPES)
    client = FakeS3Client()
    # 파일당 1MB로 제한하면 넓은 행 5천 건은 여러 파일로 쪼개진다
    stager = make_stager(client, file_size_mb=1, cleanup=False)
    rows = [(i, "x" * 500, 1.0) for i in range(5000)]

    result = run_job(s3_job(), FakeSource(COLUMNS, rows), make_target(conn), stager)

    assert len(client.objects) > 1
    restored = uploaded_text(client)
    assert restored.count("\n") == 5000
    assert result.rows_read == 5000


def test_s3_cleanup_removes_uploaded_objects():
    conn = FakeConnection(target_column_types=TARGET_TYPES)
    client = FakeS3Client()
    run_job(s3_job(), FakeSource(COLUMNS, ROWS), make_target(conn), make_stager(client))
    assert client.objects == {}
    assert len(client.deleted) == 1


def test_s3_cleanup_can_be_disabled():
    conn = FakeConnection(target_column_types=TARGET_TYPES)
    client = FakeS3Client()
    run_job(
        s3_job(),
        FakeSource(COLUMNS, ROWS),
        make_target(conn),
        make_stager(client, cleanup=False),
    )
    assert len(client.objects) == 1
    assert client.deleted == []


def test_s3_cleanup_runs_even_when_load_fails():
    conn = FakeConnection(target_column_types=TARGET_TYPES)
    client = FakeS3Client()

    class FailingCursor(FakeCursor):
        def execute(self, sql, params=()):
            super().execute(sql, params)
            if sql.startswith("INSERT INTO"):
                raise RuntimeError("외부 테이블 읽기 실패")

    conn.cursor = lambda: FailingCursor(conn)  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="외부 테이블 읽기 실패"):
        run_job(s3_job(), FakeSource(COLUMNS, ROWS), make_target(conn), make_stager(client))

    assert conn.rolled_back == 1
    assert client.objects == {}  # S3에 쓰레기를 남기지 않는다


def test_s3_upsert_loads_into_staging_then_merges():
    conn = FakeConnection(target_column_types=TARGET_TYPES)
    conn.affected_rows = 2
    client = FakeS3Client()

    result = run_job(
        s3_job(mode="upsert", key_columns=["order_id"]),
        FakeSource(COLUMNS, ROWS),
        make_target(conn),
        make_stager(client),
    )

    staging_create = next(s for s in conn.statements if s.startswith("CREATE TEMP TABLE"))
    assert 'LIKE "staging"."orders"' in staging_create
    # 외부 테이블에서 스테이징 테이블로 INSERT 후 병합
    insert = next(s for s in conn.statements if s.startswith("INSERT INTO"))
    assert '"staging"."orders"' not in insert
    delete = next(s for s in conn.statements if s.startswith("DELETE FROM"))
    assert 't."order_id" = s."order_id"' in delete
    assert result.rows_deleted == 2


def test_s3_empty_result_skips_external_table():
    conn = FakeConnection(target_column_types=TARGET_TYPES)
    client = FakeS3Client()
    result = run_job(s3_job(), FakeSource(COLUMNS, []), make_target(conn), make_stager(client))

    assert client.objects == {}
    assert not any("CREATE READABLE EXTERNAL" in s for s in conn.statements)
    assert result.rows_read == 0
    assert conn.committed == 1


def test_s3_reject_limit_adds_log_errors():
    conn = FakeConnection(target_column_types=TARGET_TYPES)
    client = FakeS3Client()
    run_job(
        s3_job(),
        FakeSource(COLUMNS, ROWS),
        make_target(conn),
        make_stager(client, segment_reject_limit=100),
    )
    create = next(s for s in conn.statements if "CREATE READABLE EXTERNAL" in s)
    assert "LOG ERRORS SEGMENT REJECT LIMIT 100 ROWS" in create


def test_s3_without_stager_raises():
    conn = FakeConnection(target_column_types=TARGET_TYPES)
    with pytest.raises(RuntimeError, match="S3 설정이 없습니다"):
        run_job(s3_job(), FakeSource(COLUMNS, ROWS), make_target(conn))


def test_s3_falls_back_to_impala_types_for_new_columns():
    # 대상 테이블에서 타입을 못 찾으면 Impala 타입을 변환해 쓴다
    conn = FakeConnection(target_column_types=[])
    conn.table_exists = False
    client = FakeS3Client()
    run_job(
        s3_job(target_columns=["order_id", "name", "amount"]),
        FakeSource(COLUMNS, ROWS),
        make_target(conn),
        make_stager(client),
    )
    create = next(s for s in conn.statements if "CREATE READABLE EXTERNAL" in s)
    assert '"name" text' in create
    assert '"amount" numeric(18,2)' in create


# -- 공통 동작 --------------------------------------------------------------------


def test_replace_mode_drops_and_creates():
    conn = FakeConnection(target_column_types=TARGET_TYPES)
    run_job(
        copy_job(mode="replace", distributed_by=["order_id"]),
        FakeSource(COLUMNS, ROWS),
        make_target(conn),
    )
    assert any(s.startswith('DROP TABLE IF EXISTS "staging"."orders"') for s in conn.statements)
    create = next(s for s in conn.statements if s.startswith("CREATE TABLE"))
    assert '"amount" numeric(18,2)' in create
    assert 'DISTRIBUTED BY ("order_id")' in create


def test_missing_table_is_created_when_allowed():
    conn = FakeConnection(table_exists=False, target_column_types=TARGET_TYPES)
    run_job(copy_job(), FakeSource(COLUMNS, ROWS), make_target(conn))
    assert any(s.startswith("CREATE TABLE IF NOT EXISTS") for s in conn.statements)


def test_missing_table_raises_when_not_allowed():
    conn = FakeConnection(table_exists=False)
    with pytest.raises(RuntimeError, match="없습니다"):
        run_job(
            copy_job(create_target_if_missing=False),
            FakeSource(COLUMNS, ROWS),
            make_target(conn),
        )
    assert conn.rolled_back == 1


def test_column_not_in_target_raises():
    conn = FakeConnection(target_column_types=TARGET_TYPES[:2])
    with pytest.raises(ValueError, match="amount"):
        run_job(copy_job(), FakeSource(COLUMNS, ROWS), make_target(conn))
    assert conn.rolled_back == 1


def test_target_columns_mapping_is_used():
    conn = FakeConnection(target_column_types=[("a", "bigint"), ("b", "text"), ("c", "numeric")])
    run_job(
        copy_job(target_columns=["a", "b", "c"]),
        FakeSource(COLUMNS, ROWS),
        make_target(conn),
    )
    assert '("a", "b", "c")' in conn.copied[0][0]


def test_target_columns_count_mismatch_raises():
    conn = FakeConnection(target_column_types=TARGET_TYPES[:2])
    with pytest.raises(ValueError, match="개수"):
        run_job(copy_job(target_columns=["a", "b"]), FakeSource(COLUMNS, ROWS), make_target(conn))


def test_source_failure_rolls_back():
    conn = FakeConnection(target_column_types=TARGET_TYPES)

    class ExplodingSource(FakeSource):
        def iter_rows(self, sql, batch_size):
            yield (1, "a", 1)
            raise RuntimeError("Impala 연결 끊김")

    with pytest.raises(RuntimeError, match="Impala 연결 끊김"):
        run_job(copy_job(), ExplodingSource(COLUMNS, ROWS), make_target(conn))
    assert conn.rolled_back == 1
    assert conn.committed == 0


def test_analyze_can_be_disabled():
    conn = FakeConnection(target_column_types=TARGET_TYPES)
    run_job(copy_job(analyze_after_load=False), FakeSource(COLUMNS, ROWS), make_target(conn))
    assert not any(s.startswith("ANALYZE") for s in conn.statements)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
