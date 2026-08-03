"""Impala 원본 리더."""

from __future__ import annotations

import importlib.util
import logging
from contextlib import contextmanager
from typing import Any, Iterator, List, Sequence, Tuple

from .config import ImpalaConfig

logger = logging.getLogger(__name__)

_INSTALL_HINT = (
    "impyla가 설치되어 있지 않습니다.\n"
    "    pip install impyla\n"
    "  Kerberos(GSSAPI) 환경이라면 pure-sasl, thrift-sasl도 함께 설치하세요."
)


def _import_hint(exc: ImportError) -> str:
    """impyla 임포트 실패 원인을 짚어 준다.

    'impala' 라는 이름의 파일이나 디렉터리가 있으면 진짜 패키지를 가려버리는데,
    이때 나오는 "'impala' is not a package" 메시지만 봐서는 원인을 알기 어렵다.
    """
    try:
        spec = importlib.util.find_spec("impala")
    except (ImportError, ValueError):
        spec = None

    if spec is None:
        return _INSTALL_HINT

    if spec.submodule_search_locations is None:
        # 패키지가 아니라 단일 모듈로 잡혔다 = 같은 이름의 .py 파일이 가리고 있다
        return (
            f"'{spec.origin}' 파일이 impyla 패키지를 가리고 있습니다.\n"
            "  이 파일의 이름을 바꾸거나 다른 디렉터리로 옮긴 뒤 다시 실행하세요.\n"
            "  (파이썬은 현재 디렉터리를 먼저 뒤지므로, impala.py 라는 파일이 있으면\n"
            "   설치된 impyla 대신 그 파일을 가져옵니다.)"
        )
    if spec.origin is None:
        # __init__.py 없는 impala/ 디렉터리가 네임스페이스 패키지로 잡힌 경우
        locations = list(spec.submodule_search_locations)
        return (
            f"{locations} 디렉터리가 impyla 패키지를 가리고 있습니다.\n"
            "  디렉터리 이름을 바꾸거나 다른 곳으로 옮긴 뒤 다시 실행하세요.\n"
            "  impyla가 아직 없다면 함께 설치하세요: pip install impyla"
        )
    return f"impyla를 불러오지 못했습니다: {exc}\n  {_INSTALL_HINT}"


def import_impala_dbapi() -> Any:
    """impyla의 ``dbapi`` 모듈을 가져온다. 실패하면 원인을 설명하는 오류를 낸다."""
    try:
        from impala import dbapi  # noqa: F401
    except ImportError as exc:
        raise ImportError(_import_hint(exc)) from exc
    return dbapi


def check_auth_dependencies(auth_mechanism: str) -> None:
    """인증 방식에 필요한 SASL 패키지가 있는지 미리 확인한다.

    impyla는 ``auth_mechanism`` 이 NOSASL이 아니면 접속하는 순간에야
    ``thrift_sasl`` / ``puresasl`` 을 임포트한다. 그래서 패키지가 없으면 접속 직전에
    맥락 없는 ModuleNotFoundError가 튀어나온다. 여기서 미리 확인해 무엇을 깔아야
    하는지 알려준다.
    """
    if (auth_mechanism or "NOSASL").upper() == "NOSASL":
        return

    missing = [
        name
        for name in ("thrift_sasl", "puresasl")
        if importlib.util.find_spec(name) is None
    ]
    if not missing:
        return

    raise ImportError(
        f"auth_mechanism={auth_mechanism} 인증에는 SASL 패키지가 필요한데 "
        f"{', '.join(missing)} 이(가) 없습니다.\n"
        "    pip install pure-sasl thrift-sasl\n"
        "  데비안/우분투에서 'Failed building wheel for pure-sasl' 이 나면:\n"
        "    pip install --use-pep517 pure-sasl thrift-sasl"
    )


class ImpalaSource:
    """Impala에 접속해 SELECT 결과를 배치 단위로 흘려보낸다."""

    def __init__(self, config: ImpalaConfig) -> None:
        self._config = config
        self._conn: Any = None

    def connect(self) -> None:
        dbapi = import_impala_dbapi()  # 지연 임포트: 임포트 비용과 선택적 의존성 분리
        check_auth_dependencies(self._config.auth_mechanism)

        logger.info("Impala 접속: %s:%s/%s", self._config.host, self._config.port, self._config.database)
        self._conn = dbapi.connect(**self._config.connect_kwargs())

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
