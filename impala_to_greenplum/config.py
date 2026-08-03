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

    return AppConfig(
        impala=_build(ImpalaConfig, raw["impala"], "impala"),
        greenplum=_build(GreenplumConfig, raw["greenplum"], "greenplum"),
        jobs=[_build(JobConfig, job, f"jobs[{i}]") for i, job in enumerate(raw["jobs"])],
    )
