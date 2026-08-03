"""Impala → Greenplum 적재 파이프라인.

Impala 커서에서 읽은 행을 메모리에 쌓지 않고 곧바로 Greenplum의 COPY 스트림으로
흘려보낸다. 적재는 하나의 트랜잭션에서 이뤄지므로 중간에 실패하면 전부 롤백된다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Iterator, List, Optional, Sequence, Tuple

from .config import AppConfig, GreenplumConfig, ImpalaConfig, JobConfig, S3Config
from .s3_stage import S3Stager
from .source import ImpalaSource
from .target import GreenplumTarget
from .typemap import impala_type_to_greenplum

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


def _external_table_columns(
    job: JobConfig,
    columns: Sequence[str],
    source_columns: Sequence[Tuple[str, str]],
    target: GreenplumTarget,
) -> List[Tuple[str, str]]:
    """외부 테이블을 만들 때 쓸 ``(컬럼명, 타입)`` 을 결정한다.

    대상 테이블의 실제 타입을 그대로 따라가야 ``INSERT ... SELECT`` 에서 불필요한
    캐스팅이나 타입 불일치가 생기지 않는다. 대상에서 못 찾은 컬럼만 Impala 타입에서
    변환해 채운다.
    """
    target_types = {name.lower(): dtype for name, dtype in target.get_column_types(job.target_table)}
    fallback = {name.lower(): impala_type_to_greenplum(dtype) for name, dtype in source_columns}

    resolved: List[Tuple[str, str]] = []
    for index, column in enumerate(columns):
        dtype = target_types.get(column.lower())
        if dtype is None:
            source_name = source_columns[index][0].lower()
            dtype = fallback.get(source_name, "text")
        resolved.append((column, dtype))
    return resolved


def _load_via_s3(
    job: JobConfig,
    source: ImpalaSource,
    target: GreenplumTarget,
    stager: "S3Stager",
    sql: str,
    columns: Sequence[str],
    source_columns: Sequence[Tuple[str, str]],
    destination: str,
    progress: _ProgressLogger,
    staged_keys: List[str],
) -> Tuple[int, int]:
    """Impala 결과를 S3에 올린 뒤 외부 테이블로 ``destination`` 에 적재한다.

    업로드한 키는 호출자가 넘긴 ``staged_keys`` 에 곧바로 기록한다. 이후 단계에서
    실패하더라도 호출자가 정리할 수 있어야 S3에 쓰레기가 남지 않는다.

    Returns:
        (읽은 행 수, 적재한 행 수).
    """
    run_id = stager.new_run_id(job.target_table)
    rows: Iterator[Sequence[Any]] = source.iter_rows(sql, job.batch_size)
    try:
        staged = stager.stage(rows, run_id, on_progress=progress)
    except BaseException:
        # 중간까지 올라간 파일도 정리 대상이다
        staged_keys.extend(stager.staged_keys(run_id))
        raise
    staged_keys.extend(staged.keys)

    if not staged.keys:
        return 0, 0

    segments = target.segment_count
    if segments and staged.file_count < segments:
        logger.warning(
            "S3 파일이 %d개뿐이라 세그먼트 %d개를 다 쓰지 못합니다. "
            "s3.file_size_mb를 줄이면 파일이 더 잘게 나뉩니다.",
            staged.file_count,
            segments,
        )

    external = target.create_external_table(
        _external_table_columns(job, columns, source_columns, target),
        stager.location(run_id),
        temp=stager.config.use_temp_external_table,
        segment_reject_limit=stager.config.segment_reject_limit,
    )
    try:
        inserted = target.insert_select(destination, external, columns)
    finally:
        target.drop_external_table(external)
    return staged.row_count, inserted


def run_job(
    job: JobConfig,
    source: ImpalaSource,
    target: GreenplumTarget,
    stager: Optional["S3Stager"] = None,
) -> LoadResult:
    """작업 하나를 실행한다. 연결은 호출자가 열어둔 상태여야 한다."""
    started = time.monotonic()
    sql = job.select_sql()
    logger.info(
        "작업 시작: %s (mode=%s, load_method=%s)", job.target_table, job.mode, job.load_method
    )
    logger.debug("Impala SQL: %s", sql)

    if job.load_method == "s3" and stager is None:
        raise RuntimeError(
            f"{job.target_table} 작업의 load_method가 s3인데 S3 설정이 없습니다."
        )

    source_columns = source.describe(sql)
    if not source_columns:
        raise RuntimeError("Impala SELECT 결과의 컬럼 정보를 확인할 수 없습니다.")
    logger.info("원본 컬럼 %d개: %s", len(source_columns), [c for c, _ in source_columns])

    progress = _ProgressLogger()
    staged_keys: List[str] = []

    # DDL 준비부터 적재까지 한 트랜잭션에서 처리한다. 중간에 실패하면 롤백해야
    # 연결을 공유하는 다음 작업이 열린 트랜잭션을 물려받지 않는다.
    try:
        _prepare_target(job, source_columns, target)
        columns = _resolve_columns(job, source_columns, target)

        # upsert는 스테이징 테이블에 먼저 담고 키 기준으로 병합한다
        staging = (
            target.create_staging_like(job.target_table, job.key_columns)
            if job.mode == "upsert"
            else None
        )
        destination = f'"{staging}"' if staging else target.qualify(job.target_table)

        if job.load_method == "s3":
            assert stager is not None
            rows_read, inserted = _load_via_s3(
                job,
                source,
                target,
                stager,
                sql,
                columns,
                source_columns,
                destination,
                progress,
                staged_keys,
            )
        else:
            rows: Iterator[Sequence[Any]] = source.iter_rows(sql, job.batch_size)
            rows_read = inserted = target.copy_rows(
                job.target_table, columns, rows, on_progress=progress, qualified=destination
            )

        if staging is not None:
            deleted, inserted = target.merge_from_staging(
                job.target_table, staging, columns, job.key_columns
            )
        else:
            deleted = 0

        # 통계 갱신은 커밋 전 같은 트랜잭션에서 수행해도 무방하다
        if job.analyze_after_load:
            target.analyze_table(job.target_table)
        target.commit()
    except Exception:
        logger.exception("적재 실패, 롤백합니다: %s", job.target_table)
        target.rollback()
        raise
    finally:
        # 적재 성공 여부와 무관하게 스테이징 파일은 남기지 않는다
        if staged_keys and stager is not None and stager.config.cleanup:
            try:
                stager.cleanup(staged_keys)
            except Exception:
                logger.warning("S3 정리에 실패했습니다. 수동으로 삭제하세요: %s", staged_keys[:3])

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

    stager = S3Stager(config.s3) if config.s3 else None
    results: List[LoadResult] = []
    with ImpalaSource(config.impala) as source, GreenplumTarget(config.greenplum) as target:
        for job in jobs:
            results.append(run_job(job, source, target, stager))
    return results


def load_query(
    impala: ImpalaConfig,
    greenplum: GreenplumConfig,
    query: str,
    target_table: str,
    s3: Optional[S3Config] = None,
    **job_options: Any,
) -> LoadResult:
    """설정 파일 없이 쿼리 하나를 바로 적재하는 편의 함수.

    Example:
        >>> load_query(
        ...     ImpalaConfig(host="impala.example.com"),
        ...     GreenplumConfig(host="gp.example.com", database="dw", user="etl"),
        ...     query="SELECT id, name, amount FROM sales.orders WHERE dt = '2026-08-01'",
        ...     target_table="orders",
        ...     s3=S3Config(bucket="my-bucket", endpoint="s3.ap-northeast-2.amazonaws.com",
        ...                 gp_config="/home/gpadmin/s3.conf"),
        ...     mode="upsert",
        ...     key_columns=["id"],
        ... )
    """
    job_options.setdefault("load_method", "s3" if s3 else "copy")
    job = JobConfig(query=query, target_table=target_table, **job_options)
    stager = S3Stager(s3) if s3 else None
    with ImpalaSource(impala) as source, GreenplumTarget(greenplum) as target:
        return run_job(job, source, target, stager)
