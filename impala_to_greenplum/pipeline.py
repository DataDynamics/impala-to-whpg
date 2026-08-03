"""Impala → Greenplum 적재 파이프라인.

Impala 커서에서 읽은 행을 메모리에 쌓지 않고 곧바로 Greenplum의 COPY 스트림으로
흘려보낸다. 적재는 하나의 트랜잭션에서 이뤄지므로 중간에 실패하면 전부 롤백된다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Iterator, List, Optional, Sequence, Tuple

from .config import AppConfig, GreenplumConfig, ImpalaConfig, JobConfig
from .source import ImpalaSource
from .target import GreenplumTarget

logger = logging.getLogger(__name__)


@dataclass
class LoadResult:
    """단일 작업의 적재 결과."""

    target_table: str
    mode: str
    rows_read: int
    rows_inserted: int
    rows_deleted: int
    elapsed_seconds: float

    @property
    def rows_per_second(self) -> float:
        return self.rows_read / self.elapsed_seconds if self.elapsed_seconds > 0 else 0.0

    def summary(self) -> str:
        return (
            f"{self.target_table}: {self.rows_read:,}건 읽음 / "
            f"{self.rows_inserted:,}건 적재 / {self.rows_deleted:,}건 삭제, "
            f"{self.elapsed_seconds:.1f}초 ({self.rows_per_second:,.0f} rows/s)"
        )


class _ProgressLogger:
    """일정 건수마다 진행 상황을 로그로 남긴다."""

    def __init__(self, every: int = 100_000) -> None:
        self._every = every
        self._total = 0
        self._next_mark = every
        self._started = time.monotonic()

    def __call__(self, count: int) -> None:
        self._total += count
        if self._total >= self._next_mark:
            elapsed = time.monotonic() - self._started
            rate = self._total / elapsed if elapsed > 0 else 0.0
            logger.info("진행 중: %s건 (%.0f rows/s)", f"{self._total:,}", rate)
            self._next_mark = ((self._total // self._every) + 1) * self._every

    @property
    def total(self) -> int:
        return self._total


def _resolve_columns(
    job: JobConfig,
    source_columns: Sequence[Tuple[str, str]],
    target: GreenplumTarget,
) -> List[str]:
    """COPY에 사용할 대상 테이블 컬럼 목록을 결정한다."""
    if job.target_columns:
        columns = list(job.target_columns)
        if len(columns) != len(source_columns):
            raise ValueError(
                f"target_columns 개수({len(columns)})가 "
                f"SELECT 결과 컬럼 수({len(source_columns)})와 다릅니다."
            )
        return columns

    columns = [name for name, _ in source_columns]
    existing = {c.lower() for c in target.get_columns(job.target_table)}
    missing = [c for c in columns if c.lower() not in existing]
    if missing:
        raise ValueError(
            f"대상 테이블 {job.target_table} 에 없는 컬럼입니다: {missing}. "
            "target_columns로 매핑을 지정하거나 SELECT 절의 별칭을 맞춰주세요."
        )
    return columns


def _prepare_target(
    job: JobConfig,
    source_columns: Sequence[Tuple[str, str]],
    target: GreenplumTarget,
) -> None:
    """모드에 맞춰 대상 테이블을 준비한다(생성 / TRUNCATE / 재생성)."""
    distribution = job.distributed_by or job.key_columns

    if job.mode == "replace":
        target.drop_table(job.target_table)
        target.create_table(job.target_table, source_columns, distribution)
        return

    if not target.table_exists(job.target_table):
        if not job.create_target_if_missing:
            raise RuntimeError(
                f"대상 테이블 {job.target_table} 이 없습니다. "
                "create_target_if_missing를 켜거나 테이블을 먼저 만들어주세요."
            )
        target.create_table(job.target_table, source_columns, distribution)
        return

    if job.mode == "truncate":
        target.truncate_table(job.target_table)


def run_job(job: JobConfig, source: ImpalaSource, target: GreenplumTarget) -> LoadResult:
    """작업 하나를 실행한다. 연결은 호출자가 열어둔 상태여야 한다."""
    started = time.monotonic()
    sql = job.select_sql()
    logger.info("작업 시작: %s (mode=%s)", job.target_table, job.mode)
    logger.debug("Impala SQL: %s", sql)

    source_columns = source.describe(sql)
    if not source_columns:
        raise RuntimeError("Impala SELECT 결과의 컬럼 정보를 확인할 수 없습니다.")
    logger.info("원본 컬럼 %d개: %s", len(source_columns), [c for c, _ in source_columns])

    progress = _ProgressLogger()

    # DDL 준비부터 COPY까지 한 트랜잭션에서 처리한다. 중간에 실패하면 롤백해야
    # 연결을 공유하는 다음 작업이 열린 트랜잭션을 물려받지 않는다.
    try:
        _prepare_target(job, source_columns, target)
        columns = _resolve_columns(job, source_columns, target)
        rows: Iterator[Sequence[Any]] = source.iter_rows(sql, job.batch_size)

        if job.mode == "upsert":
            staging = target.create_staging_like(job.target_table, job.key_columns)
            staged_rows = target.copy_rows(
                job.target_table,
                columns,
                rows,
                on_progress=progress,
                qualified=f'"{staging}"',
            )
            deleted, inserted = target.merge_from_staging(
                job.target_table, staging, columns, job.key_columns
            )
            rows_read = staged_rows
        else:
            inserted = target.copy_rows(
                job.target_table, columns, rows, on_progress=progress
            )
            deleted = 0
            rows_read = inserted

        # 통계 갱신은 커밋 전 같은 트랜잭션에서 수행해도 무방하다
        if job.analyze_after_load:
            target.analyze_table(job.target_table)
        target.commit()
    except Exception:
        logger.exception("적재 실패, 롤백합니다: %s", job.target_table)
        target.rollback()
        raise

    result = LoadResult(
        target_table=job.target_table,
        mode=job.mode,
        rows_read=rows_read,
        rows_inserted=inserted,
        rows_deleted=deleted,
        elapsed_seconds=time.monotonic() - started,
    )
    logger.info("작업 완료: %s", result.summary())
    return result


def run_load(config: AppConfig, only: Optional[Sequence[str]] = None) -> List[LoadResult]:
    """설정에 정의된 모든 작업을 순차 실행한다.

    Args:
        config: 접속 정보와 작업 목록.
        only: 실행할 대상 테이블 이름 목록(미지정 시 전체 실행).
    """
    jobs = [j for j in config.jobs if not only or j.target_table in set(only)]
    if not jobs:
        raise ValueError(f"실행할 작업이 없습니다. (필터: {only})")

    results: List[LoadResult] = []
    with ImpalaSource(config.impala) as source, GreenplumTarget(config.greenplum) as target:
        for job in jobs:
            results.append(run_job(job, source, target))
    return results


def load_query(
    impala: ImpalaConfig,
    greenplum: GreenplumConfig,
    query: str,
    target_table: str,
    **job_options: Any,
) -> LoadResult:
    """설정 파일 없이 쿼리 하나를 바로 적재하는 편의 함수.

    Example:
        >>> load_query(
        ...     ImpalaConfig(host="impala.example.com"),
        ...     GreenplumConfig(host="gp.example.com", database="dw", user="etl"),
        ...     query="SELECT id, name, amount FROM sales.orders WHERE dt = '2026-08-01'",
        ...     target_table="orders",
        ...     mode="upsert",
        ...     key_columns=["id"],
        ... )
    """
    job = JobConfig(query=query, target_table=target_table, **job_options)
    with ImpalaSource(impala) as source, GreenplumTarget(greenplum) as target:
        return run_job(job, source, target)
