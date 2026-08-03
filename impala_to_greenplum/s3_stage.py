"""Impala 결과를 S3에 분할 업로드하고, Greenplum 외부 테이블 LOCATION을 만든다.

행을 COPY TEXT 포맷으로 인코딩해 gzip 파일 여러 개로 나눠 올린다. 파일을 나누는
이유는 Greenplum이 S3 오브젝트를 세그먼트에 나눠 할당하기 때문이다. 파일이 하나면
세그먼트 하나만 일하게 되므로, 파일 개수가 최소한 세그먼트 수 이상이 되도록
``file_size_mb`` 를 잡아야 한다.

업로드는 Impala fetch와 겹쳐서 진행된다. 한 파일을 채우는 동안 앞서 채운 파일이
백그라운드 스레드에서 올라가므로 네트워크 대기 시간이 조회 시간에 묻힌다.
"""

from __future__ import annotations

import gzip
import logging
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from tempfile import SpooledTemporaryFile
from typing import IO, Any, Callable, Iterable, Iterator, List, Optional, Sequence, Tuple

from .config import S3Config
from .copy_stream import encode_row

logger = logging.getLogger(__name__)

#: 이 크기를 넘으면 업로드 버퍼를 메모리 대신 디스크에 쓴다
_SPOOL_MAX_BYTES = 32 * 1024 * 1024

#: on_progress 콜백을 호출하는 행 간격
_PROGRESS_INTERVAL = 10_000


@dataclass
class StagedData:
    """S3에 올라간 결과."""

    #: 업로드한 오브젝트 키 목록
    keys: List[str] = field(default_factory=list)
    row_count: int = 0
    uncompressed_bytes: int = 0
    compressed_bytes: int = 0

    @property
    def file_count(self) -> int:
        return len(self.keys)


class S3Stager:
    """행 이터러블을 S3의 gzip 파일 여러 개로 나눠 올린다."""

    def __init__(self, config: S3Config, client: Any = None) -> None:
        """
        Args:
            config: S3 설정.
            client: 주입할 boto3 S3 클라이언트. 미지정 시 설정으로 직접 만든다.
        """
        self._config = config
        self._client = client
        self._staged: List[str] = []

    @property
    def config(self) -> S3Config:
        return self._config

    def staged_keys(self, run_id: Optional[str] = None) -> List[str]:
        """이 스테이저가 올렸거나 올리려고 시도한 키 목록.

        업로드 도중 실패했을 때 정리 대상을 알아내기 위해 쓴다.
        """
        if run_id is None:
            return list(self._staged)
        prefix = self.run_prefix(run_id)
        return [k for k in self._staged if k.startswith(prefix)]

    # -- 클라이언트 -------------------------------------------------------------
    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3  # 지연 임포트

            session = boto3.session.Session(
                aws_access_key_id=self._config.access_key_id,
                aws_secret_access_key=self._config.secret_access_key,
                aws_session_token=self._config.session_token,
                region_name=self._config.region,
            )
            self._client = session.client(
                "s3", endpoint_url=self._config.client_endpoint_url or None
            )
        return self._client

    # -- 경로 -------------------------------------------------------------------
    def run_prefix(self, run_id: str) -> str:
        """작업 실행 하나에 대응하는 오브젝트 키 접두사."""
        base = self._config.prefix.strip("/")
        return f"{base}/{run_id}/" if base else f"{run_id}/"

    @staticmethod
    def new_run_id(target_table: str) -> str:
        """실행마다 고유한 디렉터리 이름을 만든다."""
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in target_table)
        return f"{safe}-{uuid.uuid4().hex[:16]}"

    def location(self, run_id: str) -> str:
        """Greenplum 외부 테이블 LOCATION 문자열을 만든다."""
        prefix = self.run_prefix(run_id)
        cfg = self._config

        if cfg.protocol == "pxf":
            params = [f"PROFILE=s3:text", f"SERVER={cfg.pxf_server}"]
            return f"pxf://{cfg.bucket}/{prefix}?" + "&".join(params)

        endpoint = (cfg.endpoint or "").rstrip("/")
        options: List[str] = []
        if cfg.region:
            options.append(f"region={cfg.region}")
        if cfg.gp_config:
            options.append(f"config={cfg.gp_config}")
        if cfg.gp_config_server:
            options.append(f"config_server={cfg.gp_config_server}")
        if cfg.gp_config_section:
            options.append(f"section={cfg.gp_config_section}")
        suffix = (" " + " ".join(options)) if options else ""
        return f"s3://{endpoint}/{cfg.bucket}/{prefix}{suffix}"

    # -- 업로드 -----------------------------------------------------------------
    def stage(
        self,
        rows: Iterable[Sequence[Any]],
        run_id: str,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> StagedData:
        """행을 분할 인코딩해 S3에 올리고 결과를 돌려준다."""
        cfg = self._config
        prefix = self.run_prefix(run_id)
        extension = "tsv.gz" if cfg.compress else "tsv"
        result = StagedData()
        # 업로드가 밀릴 때 버퍼가 무한정 쌓이지 않도록 대기 중인 파일 수를 제한한다
        slots = threading.Semaphore(cfg.max_upload_workers * 2)

        logger.info(
            "S3 스테이징 시작: s3://%s/%s (파일당 최대 %dMB, 압축=%s)",
            cfg.bucket,
            prefix,
            cfg.file_size_mb,
            cfg.compress,
        )

        futures: List[Future] = []
        with ThreadPoolExecutor(max_workers=cfg.max_upload_workers) as pool:
            try:
                for index, chunk in enumerate(self._chunks(rows, on_progress)):
                    buffer, row_count, raw_bytes, packed_bytes = chunk
                    key = f"{prefix}part-{index:05d}.{extension}"
                    result.keys.append(key)
                    result.row_count += row_count
                    result.uncompressed_bytes += raw_bytes
                    result.compressed_bytes += packed_bytes

                    slots.acquire()
                    self._staged.append(key)
                    futures.append(pool.submit(self._upload, key, buffer, slots))
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
            finally:
                # 실패했더라도 이미 제출된 업로드는 끝날 때까지 기다린다
                for future in futures:
                    if not future.cancelled():
                        future.result()

        if not result.keys:
            logger.warning("Impala 조회 결과가 비어 있어 업로드한 파일이 없습니다.")
        else:
            logger.info(
                "S3 스테이징 완료: %d개 파일, %s건, %.1fMB (압축 전 %.1fMB)",
                result.file_count,
                f"{result.row_count:,}",
                result.compressed_bytes / 1024 / 1024,
                result.uncompressed_bytes / 1024 / 1024,
            )
        return result

    def _upload(self, key: str, buffer: IO[bytes], slots: threading.Semaphore) -> None:
        extra: dict = {}
        if self._config.server_side_encryption:
            extra["ServerSideEncryption"] = self._config.server_side_encryption
        try:
            logger.debug("업로드: s3://%s/%s", self._config.bucket, key)
            self.client.upload_fileobj(buffer, self._config.bucket, key, ExtraArgs=extra or None)
        finally:
            buffer.close()
            slots.release()

    def _chunks(
        self,
        rows: Iterable[Sequence[Any]],
        on_progress: Optional[Callable[[int], None]],
    ) -> Iterator[Tuple[IO[bytes], int, int, int]]:
        """행을 파일 단위로 묶어 ``(버퍼, 행 수, 원본 바이트, 최종 바이트)`` 로 넘긴다."""
        limit = self._config.file_size_mb * 1024 * 1024
        buffer: Optional[IO[bytes]] = None
        writer: Optional[IO[bytes]] = None
        raw_bytes = 0
        row_count = 0
        pending = 0

        for row in rows:
            if buffer is None:
                buffer = SpooledTemporaryFile(max_size=_SPOOL_MAX_BYTES)
                writer = gzip.GzipFile(fileobj=buffer, mode="wb") if self._config.compress else buffer

            line = encode_row(row).encode("utf-8")
            writer.write(line)  # type: ignore[union-attr]
            raw_bytes += len(line)
            row_count += 1
            pending += 1

            if on_progress is not None and pending >= _PROGRESS_INTERVAL:
                on_progress(pending)
                pending = 0

            if raw_bytes >= limit:
                yield self._close_chunk(buffer, writer, row_count, raw_bytes)
                buffer, writer, raw_bytes, row_count = None, None, 0, 0

        if on_progress is not None and pending:
            on_progress(pending)
        if buffer is not None and row_count:
            yield self._close_chunk(buffer, writer, row_count, raw_bytes)
        elif buffer is not None:
            buffer.close()

    @staticmethod
    def _close_chunk(
        buffer: IO[bytes], writer: Optional[IO[bytes]], row_count: int, raw_bytes: int
    ) -> Tuple[IO[bytes], int, int, int]:
        if writer is not None and writer is not buffer:
            writer.close()  # gzip 트레일러를 기록한다
        packed_bytes = buffer.tell()
        buffer.seek(0)
        return buffer, row_count, raw_bytes, packed_bytes

    # -- 정리 -------------------------------------------------------------------
    def cleanup(self, keys: Optional[Sequence[str]] = None) -> int:
        """업로드한 오브젝트를 삭제한다.

        접두사 전체를 지우지 않고 이 스테이저가 실제로 올린 키만 지운다.
        같은 버킷을 다른 작업과 공유하더라도 남의 파일을 건드리지 않는다.
        """
        targets = list(keys) if keys is not None else list(self._staged)
        if not targets:
            return 0

        deleted = 0
        for start in range(0, len(targets), 1000):  # delete_objects는 요청당 1000개 제한
            batch = targets[start : start + 1000]
            response = self.client.delete_objects(
                Bucket=self._config.bucket,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
            )
            errors = (response or {}).get("Errors") or []
            for error in errors:
                logger.warning("S3 오브젝트 삭제 실패: %s (%s)", error.get("Key"), error.get("Message"))
            deleted += len(batch) - len(errors)

        self._staged = [k for k in self._staged if k not in set(targets)]
        logger.info("S3 정리 완료: %d개 오브젝트 삭제", deleted)
        return deleted


def render_gp_s3_config(
    access_key_id: str,
    secret_access_key: str,
    section: str = "default",
    version: int = 1,
    encryption: bool = True,
) -> str:
    """Greenplum s3 프로토콜이 요구하는 설정 파일 내용을 만든다.

    이 파일은 마스터와 **모든 세그먼트 호스트의 동일한 경로** 에 배포해야 하며,
    ``gpscp`` 나 구성 관리 도구로 직접 옮겨야 한다. 자격증명이 평문으로 들어가므로
    소유자만 읽을 수 있도록 권한을 600으로 두는 것이 좋다.
    """
    return (
        f"[{section}]\n"
        f"accessid = {access_key_id}\n"
        f"secret = {secret_access_key}\n"
        f"threadnum = 4\n"
        f"chunksize = 67108864\n"
        f"encryption = {'true' if encryption else 'false'}\n"
        f"version = {version}\n"
    )
