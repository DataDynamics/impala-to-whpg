"""Impala 원본 리더."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator, List, Sequence, Tuple

from .config import ImpalaConfig

logger = logging.getLogger(__name__)


class ImpalaSource:
    """Impala에 접속해 SELECT 결과를 배치 단위로 흘려보낸다."""

    def __init__(self, config: ImpalaConfig) -> None:
        self._config = config
        self._conn: Any = None

    def connect(self) -> None:
        from impala.dbapi import connect  # 지연 임포트: 임포트 비용과 선택적 의존성 분리

        logger.info("Impala 접속: %s:%s/%s", self._config.host, self._config.port, self._config.database)
        self._conn = connect(**self._config.connect_kwargs())

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def __enter__(self) -> "ImpalaSource":
        self.connect()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        if self._conn is None:
            raise RuntimeError("Impala에 연결되어 있지 않습니다. connect()를 먼저 호출하세요.")
        cursor = self._conn.cursor()
        try:
            for key, value in self._config.session_settings.items():
                cursor.execute(f"SET {key}={value}")
            yield cursor
        finally:
            cursor.close()

    def describe(self, sql: str) -> List[Tuple[str, str]]:
        """SELECT 결과 스키마를 ``(컬럼명, 타입)`` 목록으로 돌려준다.

        ``LIMIT 0`` 로 감싸 실제 데이터를 읽지 않고 메타데이터만 가져온다.
        """
        with self._cursor() as cursor:
            cursor.execute(f"SELECT * FROM ({sql}) AS __schema_probe LIMIT 0")
            description = cursor.description or []
            return [(col[0].split(".")[-1], str(col[1]).lower()) for col in description]

    def count(self, sql: str) -> int:
        """SELECT 문의 전체 행 수를 센다(진행률 표시가 필요할 때만 사용)."""
        with self._cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM ({sql}) AS __count_probe")
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    def iter_rows(self, sql: str, batch_size: int) -> Iterator[Sequence[Any]]:
        """SELECT 결과를 ``fetchmany`` 로 배치 조회하며 행 단위로 흘려보낸다."""
        with self._cursor() as cursor:
            logger.info("Impala 쿼리 실행 (batch_size=%d)", batch_size)
            cursor.arraysize = batch_size
            cursor.execute(sql)
            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break
                logger.debug("Impala에서 %d건 fetch", len(rows))
                yield from rows
