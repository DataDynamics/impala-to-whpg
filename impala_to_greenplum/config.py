"""YAML 파일과 환경변수로부터 접속/작업 설정을 로드한다.

비밀번호처럼 민감한 값은 YAML에 직접 쓰는 대신 ``${ENV_VAR}`` 형태로 참조하면
로드 시점에 환경변수 값으로 치환된다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

VALID_MODES = ("append", "truncate", "replace", "upsert")
VALID_LOAD_METHODS = ("s3", "copy")
VALID_S3_PROTOCOLS = ("s3", "pxf")


class ConfigError(ValueError):
    """설정 파일이 잘못되었을 때 발생한다."""


@dataclass
class ImpalaConfig:
    host: str
    port: int = 21050
    database: str = "default"
    user: Optional[str] = None
    password: Optional[str] = None
    #: NOSASL(기본) / PLAIN(LDAP) / GSSAPI(Kerberos)
    auth_mechanism: str = "NOSASL"
    kerberos_service_name: str = "impala"
    use_ssl: bool = False
    ca_cert: Optional[str] = None
    timeout: Optional[int] = None
    #: 세션 단위로 실행할 SET 구문 (예: MEM_LIMIT)
    session_settings: Dict[str, str] = field(default_factory=dict)

    def connect_kwargs(self) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "auth_mechanism": self.auth_mechanism,
            "use_ssl": self.use_ssl,
        }
        if self.user:
            kwargs["user"] = self.user
        if self.password:
            kwargs["password"] = self.password
        if self.auth_mechanism == "GSSAPI":
            kwargs["kerberos_service_name"] = self.kerberos_service_name
        if self.ca_cert:
            kwargs["ca_cert"] = self.ca_cert
        if self.timeout is not None:
            kwargs["timeout"] = self.timeout
        return kwargs


@dataclass
class GreenplumConfig:
    host: str
    port: int = 5432
    database: str = "postgres"
    user: str = "gpadmin"
    password: Optional[str] = None
    schema: str = "public"
    sslmode: Optional[str] = None
    connect_timeout: int = 30
    #: 적재 세션에서 먼저 실행할 SQL (예: SET gp_autostats_mode='none')
    session_sql: List[str] = field(default_factory=list)

    def connect_kwargs(self) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "dbname": self.database,
            "user": self.user,
            "connect_timeout": self.connect_timeout,
        }
        if self.password:
            kwargs["password"] = self.password
        if self.sslmode:
            kwargs["sslmode"] = self.sslmode
        return kwargs


@dataclass
class S3Config:
    """S3 스테이징 및 Greenplum 외부 테이블 설정."""

    bucket: str
    #: 오브젝트 키 접두사. 작업마다 하위에 고유 디렉터리를 만든다.
    prefix: str = "impala-to-greenplum"

    # -- boto3 업로드 설정 ------------------------------------------------------
    region: Optional[str] = None
    access_key_id: Optional[str] = None
    secret_access_key: Optional[str] = None
    session_token: Optional[str] = None
    #: MinIO 등 S3 호환 스토리지를 쓸 때 boto3가 접속할 엔드포인트 URL
    client_endpoint_url: Optional[str] = None
    #: 업로드 시 적용할 서버측 암호화 (예: AES256, aws:kms)
    server_side_encryption: Optional[str] = None
    #: 파일 하나당 목표 크기(압축 전 MB). 세그먼트 수 이상으로 파일이 나뉘어야 병렬로 읽힌다.
    file_size_mb: int = 128
    #: gzip 압축 여부. s3/pxf 프로토콜 모두 .gz 파일을 자동으로 인식한다.
    compress: bool = True
    #: 동시 업로드 스레드 수
    max_upload_workers: int = 4
    #: 적재 후 업로드한 오브젝트를 삭제할지 여부
    cleanup: bool = True

    # -- Greenplum 외부 테이블 설정 ---------------------------------------------
    #: s3(내장 s3 프로토콜) 또는 pxf
    protocol: str = "s3"
    #: LOCATION에 쓸 S3 엔드포인트 (예: s3.ap-northeast-2.amazonaws.com)
    endpoint: Optional[str] = None
    #: 세그먼트 호스트에 배포된 s3 프로토콜 설정 파일 경로
    gp_config: Optional[str] = None
    #: gpfdist 기반 config_server를 쓸 때의 URL
    gp_config_server: Optional[str] = None
    #: s3 설정 파일에서 사용할 섹션명
    gp_config_section: Optional[str] = None
    #: protocol이 pxf일 때 사용할 PXF 서버 이름
    pxf_server: Optional[str] = None
    #: 외부 테이블을 임시 테이블로 만들지 여부 (false면 greenplum.schema에 생성)
    use_temp_external_table: bool = True
    #: 형식 오류 행을 버릴 허용 한도. 0이면 오류 발생 시 즉시 실패한다.
    segment_reject_limit: int = 0

    def __post_init__(self) -> None:
        if not self.bucket:
            raise ConfigError("s3.bucket은 필수입니다.")
        if self.protocol not in VALID_S3_PROTOCOLS:
            raise ConfigError(
                f"s3.protocol은 {VALID_S3_PROTOCOLS} 중 하나여야 합니다. (입력값: {self.protocol})"
            )
        if self.file_size_mb <= 0:
            raise ConfigError("s3.file_size_mb는 1 이상이어야 합니다.")
        if self.max_upload_workers <= 0:
            raise ConfigError("s3.max_upload_workers는 1 이상이어야 합니다.")
        if self.segment_reject_limit < 0:
            raise ConfigError("s3.segment_reject_limit는 0 이상이어야 합니다.")
        if self.protocol == "s3":
            if not self.endpoint:
                raise ConfigError(
                    "protocol이 s3이면 s3.endpoint가 필요합니다. "
                    "(예: s3.ap-northeast-2.amazonaws.com)"
                )
            if not self.gp_config and not self.gp_config_server:
                raise ConfigError(
                    "Greenplum s3 프로토콜은 세그먼트에 배포된 설정 파일이 필요합니다. "
                    "s3.gp_config 또는 s3.gp_config_server를 지정하세요. "
                    "설정 파일은 docs/s3_external_table.md를 참고하세요."
                )
        elif not self.pxf_server:
            raise ConfigError("protocol이 pxf이면 s3.pxf_server가 필요합니다.")


@dataclass
class JobConfig:
    """단일 적재 작업 정의."""

    #: Impala에서 실행할 SELECT 문 (query 또는 source_table 중 하나는 필수)
    query: Optional[str] = None
    #: SELECT * 로 읽어올 Impala 원본 테이블 (db.table)
    source_table: Optional[str] = None
    #: 적재 대상 Greenplum 테이블명 (스키마 제외)
    target_table: str = ""
    #: append | truncate | replace | upsert
    mode: str = "append"
    #: upsert 모드에서 사용할 키 컬럼
    key_columns: List[str] = field(default_factory=list)
    #: Impala fetch 및 COPY 배치 크기
    batch_size: int = 50_000
    #: 대상 테이블이 없으면 Impala 스키마를 기반으로 생성한다
    create_target_if_missing: bool = True
    #: replace/create 시 사용할 분산키 (미지정 시 key_columns → 첫 컬럼 순으로 추론)
    distributed_by: List[str] = field(default_factory=list)
    #: 적재 후 ANALYZE 실행 여부
    analyze_after_load: bool = True
    #: 대상 테이블에 적재할 컬럼 순서 (미지정 시 SELECT 결과 컬럼 순서)
    target_columns: List[str] = field(default_factory=list)
    #: s3(S3 업로드 후 외부 테이블 적재) 또는 copy(COPY FROM STDIN 직접 스트리밍)
    load_method: str = "s3"

    def __post_init__(self) -> None:
        if bool(self.query) == bool(self.source_table):
            raise ConfigError("query 또는 source_table 중 정확히 하나만 지정해야 합니다.")
        if not self.target_table:
            raise ConfigError("target_table은 필수입니다.")
        if self.mode not in VALID_MODES:
            raise ConfigError(f"mode는 {VALID_MODES} 중 하나여야 합니다. (입력값: {self.mode})")
        if self.mode == "upsert" and not self.key_columns:
            raise ConfigError("upsert 모드에서는 key_columns가 필요합니다.")
        if self.batch_size <= 0:
            raise ConfigError("batch_size는 1 이상이어야 합니다.")
        if self.load_method not in VALID_LOAD_METHODS:
            raise ConfigError(
                f"load_method는 {VALID_LOAD_METHODS} 중 하나여야 합니다. "
                f"(입력값: {self.load_method})"
            )

    def select_sql(self) -> str:
        """Impala에서 실행할 SELECT 문을 돌려준다."""
        if self.query:
            return self.query.strip().rstrip(";")
        return f"SELECT * FROM {self.source_table}"


@dataclass
class AppConfig:
    impala: ImpalaConfig
    greenplum: GreenplumConfig
    jobs: List[JobConfig]
    s3: Optional[S3Config] = None


def _expand_env(value: Any) -> Any:
    """``${VAR}`` / ``${VAR:-default}`` 패턴을 환경변수 값으로 치환한다."""
    if isinstance(value, str):

        def replace(match: "re.Match[str]") -> str:
            name, default = match.group(1), match.group(2)
            resolved = os.environ.get(name, default)
            if resolved is None:
                raise ConfigError(f"환경변수 {name} 가 정의되지 않았습니다.")
            return resolved

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _build(cls: type, section: Dict[str, Any], name: str) -> Any:
    known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    unknown = set(section) - known
    if unknown:
        raise ConfigError(f"{name} 섹션에 알 수 없는 키가 있습니다: {sorted(unknown)}")
    return cls(**section)


def load_config(path: str) -> AppConfig:
    """YAML 설정 파일을 읽어 :class:`AppConfig` 로 변환한다."""
    with open(path, "r", encoding="utf-8") as fp:
        raw = yaml.safe_load(fp) or {}

    raw = _expand_env(raw)
    for required in ("impala", "greenplum", "jobs"):
        if required not in raw:
            raise ConfigError(f"설정 파일에 '{required}' 섹션이 없습니다.")
    if not isinstance(raw["jobs"], list) or not raw["jobs"]:
        raise ConfigError("jobs는 비어 있지 않은 목록이어야 합니다.")

    config = AppConfig(
        impala=_build(ImpalaConfig, raw["impala"], "impala"),
        greenplum=_build(GreenplumConfig, raw["greenplum"], "greenplum"),
        jobs=[_build(JobConfig, job, f"jobs[{i}]") for i, job in enumerate(raw["jobs"])],
        s3=_build(S3Config, raw["s3"], "s3") if raw.get("s3") else None,
    )

    if config.s3 is None:
        needs_s3 = [j.target_table for j in config.jobs if j.load_method == "s3"]
        if needs_s3:
            raise ConfigError(
                f"load_method가 s3인 작업({needs_s3})이 있지만 's3' 섹션이 없습니다. "
                "s3 섹션을 추가하거나 load_method를 copy로 바꾸세요."
            )
    return config
