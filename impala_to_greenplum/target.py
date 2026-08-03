"""Greenplum 적재 대상."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from .config import GreenplumConfig
from .copy_stream import RowStream
from .typemap import build_create_table_sql, qualified_name, quote_ident

logger = logging.getLogger(__name__)


class GreenplumTarget:
    """Greenplum에 ``COPY ... FROM STDIN`` 으로 대량 적재한다.

    INSERT 문을 반복하는 방식보다 수십 배 빠르고, 스트림으로 넘기기 때문에
    데이터 전체를 메모리에 올리지 않는다.
    """

    def __init__(self, config: GreenplumConfig) -> None:
        self._config = config
        self._conn: Any = None
        self._segment_count = 0

    # -- 연결 관리 --------------------------------------------------------------
    def connect(self) -> None:
        import psycopg2  # 지연 임포트

        logger.info(
            "Greenplum 접속: %s:%s/%s", self._config.host, self._config.port, self._config.database
        )
        self._conn = psycopg2.connect(**self._config.connect_kwargs())
        self._conn.autocommit = False
        if self._config.session_sql:
            with self._conn.cursor() as cursor:
                for statement in self._config.session_sql:
                    logger.debug("세션 SQL 실행: %s", statement)
                    cursor.execute(statement)
            self._conn.commit()
        # 파일 분할 개수를 판단할 때 쓰므로 미리 읽어 캐시한다. 실패해도 치명적이지 않다.
        self._segment_count = self._query_segment_count()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def __enter__(self) -> "GreenplumTarget":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, *_: Any) -> None:
        if self._conn is not None and exc_type is not None:
            self._conn.rollback()
        self.close()

    @property
    def connection(self) -> Any:
        if self._conn is None:
            raise RuntimeError("Greenplum에 연결되어 있지 않습니다. connect()를 먼저 호출하세요.")
        return self._conn

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def qualify(self, table: str) -> str:
        return qualified_name(self._config.schema, table)

    @property
    def segment_count(self) -> int:
        """프라이머리 세그먼트 수. 조회에 실패했으면 0."""
        return self._segment_count

    def _query_segment_count(self) -> int:
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM gp_segment_configuration "
                    "WHERE content >= 0 AND role = 'p'"
                )
                count = int(cursor.fetchone()[0])
            self.connection.commit()
            logger.info("Greenplum 프라이머리 세그먼트 %d개", count)
            return count
        except Exception as exc:  # 순수 PostgreSQL이면 카탈로그가 없다
            logger.debug("세그먼트 수를 확인하지 못했습니다: %s", exc)
            self.connection.rollback()
            return 0

    def _split(self, table: str) -> Tuple[str, str]:
        """``schema.table`` 또는 ``table`` 을 (스키마, 테이블)로 분리한다."""
        if "." in table:
            schema, name = table.split(".", 1)
            return schema, name
        return self._config.schema, table

    # -- DDL --------------------------------------------------------------------
    def table_exists(self, table: str) -> bool:
        schema, name = self._split(table)
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass(%s)", (f"{schema}.{name}",))
            return cursor.fetchone()[0] is not None

    def create_table(
        self,
        table: str,
        columns: Sequence[Tuple[str, str]],
        distributed_by: Iterable[str] = (),
    ) -> None:
        sql = build_create_table_sql(self.qualify(table), columns, distributed_by)
        logger.info("대상 테이블 생성:\n%s", sql)
        with self.connection.cursor() as cursor:
            cursor.execute(sql)

    def drop_table(self, table: str) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS {self.qualify(table)}")

    def truncate_table(self, table: str) -> None:
        logger.info("TRUNCATE %s", self.qualify(table))
        with self.connection.cursor() as cursor:
            cursor.execute(f"TRUNCATE TABLE {self.qualify(table)}")

    def analyze_table(self, table: str) -> None:
        logger.info("ANALYZE %s", self.qualify(table))
        with self.connection.cursor() as cursor:
            cursor.execute(f"ANALYZE {self.qualify(table)}")

    def get_column_types(self, table: str) -> List[Tuple[str, str]]:
        """대상 테이블의 ``(컬럼명, 타입)`` 을 정의 순서대로 돌려준다.

        외부 테이블을 대상 테이블과 똑같은 타입으로 만들기 위해 사용한다. 타입이
        어긋나면 ``INSERT ... SELECT`` 단계에서 캐스팅 비용이나 오류가 생긴다.
        """
        schema, name = self._split(table)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT a.attname, format_type(a.atttypid, a.atttypmod)
                  FROM pg_attribute a
                  JOIN pg_class c ON c.oid = a.attrelid
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = %s AND c.relname = %s
                   AND a.attnum > 0 AND NOT a.attisdropped
                 ORDER BY a.attnum
                """,
                (schema, name),
            )
            return [(row[0], row[1]) for row in cursor.fetchall()]

    def get_columns(self, table: str) -> List[str]:
        """대상 테이블의 컬럼명을 정의 순서대로 돌려준다."""
        schema, name = self._split(table)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema = %s AND table_name = %s
                 ORDER BY ordinal_position
                """,
                (schema, name),
            )
            return [row[0] for row in cursor.fetchall()]

    # -- 적재 -------------------------------------------------------------------
    def copy_rows(
        self,
        table: str,
        columns: Sequence[str],
        rows: Iterable[Sequence[Any]],
        on_progress: Optional[Any] = None,
        qualified: Optional[str] = None,
    ) -> int:
        """행 이터러블을 ``COPY ... FROM STDIN`` 으로 적재하고 적재 건수를 돌려준다."""
        target = qualified or self.qualify(table)
        column_list = ", ".join(quote_ident(c) for c in columns)
        sql = f"COPY {target} ({column_list}) FROM STDIN WITH (FORMAT text, NULL '\\N')"

        stream = RowStream(rows, on_rows=on_progress)
        with self.connection.cursor() as cursor:
            logger.info("COPY 시작 → %s", target)
            cursor.copy_expert(sql, stream, size=1 << 20)
        logger.info("COPY 완료 → %s (%d건)", target, stream.row_count)
        return stream.row_count

    # -- 외부 테이블 -------------------------------------------------------------
    def create_external_table(
        self,
        columns: Sequence[Tuple[str, str]],
        location: str,
        temp: bool = True,
        segment_reject_limit: int = 0,
    ) -> str:
        """S3를 가리키는 읽기 전용 외부 테이블을 만들고 정규화된 이름을 돌려준다.

        포맷은 스테이징 파일과 동일한 COPY TEXT(탭 구분, ``\\N`` NULL)로 맞춘다.
        gzip 파일은 ``.gz`` 확장자를 보고 Greenplum이 알아서 풀어 읽는다.
        """
        if not columns:
            raise ValueError("외부 테이블을 만들 컬럼 정보가 없습니다.")

        name = f"ext_{uuid.uuid4().hex[:16]}"
        qualified = quote_ident(name) if temp else qualified_name(self._config.schema, name)
        column_defs = ",\n".join(
            f"    {quote_ident(col)} {dtype}" for col, dtype in columns
        )
        # LOCATION은 문자열 리터럴로 들어가므로 작은따옴표를 이스케이프한다
        escaped_location = location.replace("'", "''")
        reject = (
            f"\nLOG ERRORS SEGMENT REJECT LIMIT {int(segment_reject_limit)} ROWS"
            if segment_reject_limit > 0
            else ""
        )
        sql = (
            f"CREATE READABLE EXTERNAL {'TEMP ' if temp else ''}TABLE {qualified} (\n"
            f"{column_defs}\n)\n"
            f"LOCATION ('{escaped_location}')\n"
            r"FORMAT 'TEXT' (DELIMITER E'\t' NULL E'\\N' ESCAPE E'\\')"
            "\nENCODING 'UTF8'"
            f"{reject}"
        )
        logger.info("외부 테이블 생성:\n%s", sql)
        with self.connection.cursor() as cursor:
            cursor.execute(sql)
        return qualified

    def drop_external_table(self, qualified: str) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(f"DROP EXTERNAL TABLE IF EXISTS {qualified}")

    def insert_select(
        self,
        destination: str,
        source: str,
        columns: Sequence[str],
    ) -> int:
        """``INSERT INTO dest (cols) SELECT cols FROM source`` 를 실행한다.

        외부 테이블에서 읽는 경우 각 세그먼트가 자기 몫의 S3 파일을 병렬로 읽어
        곧바로 자기 자리에 적재하므로, 마스터를 거치는 COPY보다 빠르다.
        """
        column_list = ", ".join(quote_ident(c) for c in columns)
        sql = f"INSERT INTO {destination} ({column_list}) SELECT {column_list} FROM {source}"
        logger.info("외부 테이블에서 적재: %s ← %s", destination, source)
        with self.connection.cursor() as cursor:
            cursor.execute(sql)
            inserted = cursor.rowcount
        logger.info("적재 완료: %s (%s건)", destination, f"{inserted:,}")
        return inserted

    def create_staging_like(self, table: str, distributed_by: Sequence[str] = ()) -> str:
        """대상 테이블과 동일한 구조의 임시 스테이징 테이블을 만들고 이름을 돌려준다."""
        staging = f"stg_{uuid.uuid4().hex[:16]}"
        distribution = (
            f" DISTRIBUTED BY ({', '.join(quote_ident(k) for k in distributed_by)})"
            if distributed_by
            else " DISTRIBUTED RANDOMLY"
        )
        sql = (
            f"CREATE TEMP TABLE {quote_ident(staging)} "
            f"(LIKE {self.qualify(table)}) ON COMMIT DROP{distribution}"
        )
        logger.debug("스테이징 테이블 생성: %s", sql)
        with self.connection.cursor() as cursor:
            cursor.execute(sql)
        return staging

    def merge_from_staging(
        self,
        table: str,
        staging: str,
        columns: Sequence[str],
        key_columns: Sequence[str],
    ) -> Tuple[int, int]:
        """스테이징 테이블 내용을 키 기준으로 대상 테이블에 반영한다.

        Greenplum 6는 ``INSERT ... ON CONFLICT`` 를 지원하지 않으므로
        ``DELETE ... USING`` 후 ``INSERT ... SELECT`` 로 처리한다. 두 구문은
        호출자의 트랜잭션 안에서 실행되므로 원자적으로 반영된다.

        Returns:
            (삭제 건수, 삽입 건수).
        """
        target = self.qualify(table)
        staged = quote_ident(staging)
        join = " AND ".join(
            f"t.{quote_ident(k)} = s.{quote_ident(k)}" for k in key_columns
        )
        column_list = ", ".join(quote_ident(c) for c in columns)

        with self.connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {target} t USING {staged} s WHERE {join}")
            deleted = cursor.rowcount
            cursor.execute(
                f"INSERT INTO {target} ({column_list}) SELECT {column_list} FROM {staged}"
            )
            inserted = cursor.rowcount
        logger.info("MERGE 완료 → %s (삭제 %d건, 삽입 %d건)", target, deleted, inserted)
        return deleted, inserted
