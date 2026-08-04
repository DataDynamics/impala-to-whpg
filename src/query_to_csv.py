"""Impala에서 쿼리를 실행해 CSV 파일로 저장하고, 구간별 소요 시간을 보여준다.

TLS(SSL) 위에서 LDAP 인증(auth_mechanism=PLAIN)으로 접속하는 구성을 전제로 한다.

접속 정보는 conf/config.yaml 의 impala 섹션에서 자동으로 읽는다. 명령행으로 준 값이
항상 우선하므로, 설정을 채워두면 쿼리와 출력 경로만 주면 된다.

    pip install impyla pure-sasl thrift-sasl PyYAML
    # "Failed building wheel for pure-sasl" 이 나면
    #     pip install --use-pep517 pure-sasl thrift-sasl

    # 설정 파일에 접속 정보가 있을 때
    bin/query-to-csv \
        --query "SELECT * FROM sales.orders WHERE order_dt = '2026-08-01'" \
        --output orders.csv

    # sql/ 의 템플릿에 변수를 채워 실행
    bin/query-to-csv -f daily_orders.sql --var dt=2026-08-01 -o orders.csv

    # 설정을 무시하고 전부 명령행으로
    export IMPALA_PASSWORD='...'
    bin/query-to-csv --no-config \
        --host impala.example.com \
        --user etl_user \
        --ca-cert /etc/ssl/certs/impala-ca.pem \
        --query-file daily_orders.sql --output orders.csv.gz --gzip

``--query-file`` 은 이름만 주면 저장소의 sql/ 에서 찾는다. .sql 파일은 Jinja
템플릿이라 ``{{ 변수 }}`` 자리에 ``--var`` 로 준 값이 들어간다.

외부 의존성은 impyla, PyYAML, Jinja2 이고 같은 디렉터리의 appconfig 모듈을 함께
쓴다. PyYAML 은 설정 파일을 읽을 때만, Jinja2 는 SQL에 템플릿 문법이 있을 때만
임포트하므로 둘 다 쓰지 않는다면 impyla 만 있어도 된다.

비밀번호는 명령행 인자로 받지 않는다. ps로 다른 사용자에게 노출되기 때문에
설정 파일(보통 ``${IMPALA_PASSWORD}`` 참조), 환경변수, 대화형 입력으로만 받는다.
"""

import argparse
import contextlib
import csv
import getpass
import gzip
import importlib.util
import os
import sys
import time
import unicodedata
from typing import Any, Dict, Iterator, List, Optional, Sequence, TextIO

import appconfig

_INSTALL_HINT = (
    "impyla가 설치되어 있지 않습니다.\n"
    "    pip install impyla\n"
    "  LDAP/Kerberos 인증에는 pure-sasl, thrift-sasl도 필요합니다."
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
        return (
            f"{list(spec.submodule_search_locations)} 디렉터리가 impyla 패키지를 "
            "가리고 있습니다.\n  디렉터리 이름을 바꾸거나 다른 곳으로 옮긴 뒤 다시 "
            "실행하세요.\n  impyla가 아직 없다면 함께 설치하세요: pip install impyla"
        )
    return f"impyla를 불러오지 못했습니다: {exc}\n  {_INSTALL_HINT}"


def import_impala_dbapi() -> Any:
    """impyla의 ``dbapi`` 모듈을 가져온다. 실패하면 원인을 설명하는 오류를 낸다."""
    try:
        from impala import dbapi  # noqa: F401
    except ImportError as exc:
        raise ImportError(_import_hint(exc)) from exc
    return dbapi


class CyrusSASLClient:
    """Cyrus SASL(``sasl`` 패키지)을 impyla가 기대하는 인터페이스로 감싼다.

    impyla는 SASL 클라이언트로 ``puresasl`` 을 고정해서 쓴다. Cyrus SASL을 쓰려면
    ``impala.sasl_compat.PureSASLClient`` 를 이 클래스로 바꿔치기해야 한다.
    ``get_transport`` 가 함수 안에서 임포트하므로 접속 전에 바꾸면 반영된다.

    참고: ``sasl`` 패키지는 C 확장이라 Python 3.11 이상에서는 빌드가 되지 않는다
    (saslwrapper가 3.11에서 없어진 longintrepr.h 를 참조한다). 대부분의 환경에서는
    기본값인 puresasl 을 그대로 쓰면 된다.
    """

    def __init__(self, host: str, username: Any = None, password: Any = None,
                 service: Any = None) -> None:
        import sasl  # 없으면 호출부에서 안내 메시지를 낸다

        client = sasl.Client()
        client.setAttr("host", host)
        client.setAttr("service", service or "impala")
        if username is not None:
            client.setAttr("username", username)
        if password is not None:
            client.setAttr("password", password)
        client.init()
        self._client = client

    def start(self, mechanism: Any) -> Any:
        return self._client.start(mechanism)

    def step(self, challenge: Any) -> Any:
        return self._client.step(challenge)

    def encode(self, incoming: Any) -> Any:
        return self._client.encode(incoming)

    def decode(self, outgoing: Any) -> Any:
        return self._client.decode(outgoing)

    def getError(self) -> Any:
        return self._client.getError()


def use_cyrus_sasl() -> None:
    """impyla가 puresasl 대신 Cyrus SASL을 쓰도록 바꾼다."""
    if importlib.util.find_spec("sasl") is None:
        raise ImportError(
            "--sasl-backend sasl 을 쓰려면 Cyrus SASL 바인딩이 필요합니다.\n"
            "    pip install sasl        # libsasl2-dev 가 먼저 설치돼 있어야 합니다\n"
            "  다만 sasl 패키지는 Python 3.11 이상에서는 빌드되지 않습니다.\n"
            "  (saslwrapper가 3.11에서 없어진 longintrepr.h 를 참조합니다)\n"
            "  기본값인 --sasl-backend puresasl 을 쓰세요. impyla의 기본 동작입니다."
        )

    import impala.sasl_compat

    impala.sasl_compat.PureSASLClient = CyrusSASLClient


def check_auth_dependencies(auth_mechanism: str) -> None:
    """인증 방식에 필요한 SASL 패키지가 있는지 미리 확인한다.

    impyla는 ``auth_mechanism`` 이 NOSASL이 아니면 접속하는 순간에야
    ``thrift_sasl`` / ``puresasl`` 을 임포트한다. 그래서 패키지가 없으면 접속 직전에
    맥락 없는 ModuleNotFoundError가 튀어나온다. 여기서 미리 확인해 알려준다.
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


def display_width(text: str) -> int:
    """터미널에서 차지하는 칸 수. 한글·한자는 두 칸을 쓴다."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text: str, width: int) -> str:
    """표시 폭 기준으로 오른쪽을 채운다(한글이 섞여도 열이 맞는다)."""
    return text + " " * max(0, width - display_width(text))


class PhaseTimer:
    """구간별 누적 시간을 재고 표로 정리한다.

    fetch와 CSV 쓰기는 번갈아 일어나므로 한 번씩 재서는 의미가 없다.
    구간 이름별로 누적해서 "전체 중 어디에 시간을 썼는지"를 본다.
    """

    def __init__(self, order: Sequence[str] = ()) -> None:
        """
        Args:
            order: 보고서에 표시할 구간 순서. 미리 정해두면 실제로 처음 호출된
                순서(예: 헤더를 먼저 쓰느라 'CSV 쓰기'가 앞서는 경우)와 무관하게
                파이프라인 흐름대로 읽힌다.
        """
        self._elapsed: Dict[str, float] = {name: 0.0 for name in order}
        self._order: List[str] = list(order)
        self._started = time.monotonic()

    @contextlib.contextmanager
    def measure(self, name: str) -> Iterator[None]:
        if name not in self._elapsed:
            self._elapsed[name] = 0.0
            self._order.append(name)
        start = time.monotonic()
        try:
            yield
        finally:
            self._elapsed[name] += time.monotonic() - start

    @property
    def total(self) -> float:
        """프로그램 시작부터 지금까지의 실제 경과 시간."""
        return time.monotonic() - self._started

    def report(self) -> str:
        # 한 번도 실행되지 않은 구간은 굳이 보여주지 않는다
        names = [n for n in self._order if self._elapsed[n] > 0]
        measured = sum(self._elapsed.values())
        total = self.total
        width = max((display_width(n) for n in names), default=0)
        width = max(width, display_width("기타"), display_width("합계"))

        lines = ["", "=== 구간별 소요 시간 ==="]
        for index, name in enumerate(names, 1):
            seconds = self._elapsed[name]
            share = seconds / total * 100 if total > 0 else 0.0
            lines.append(f"  {index}. {pad(name, width)}  {seconds:8.3f}초  {share:5.1f}%")

        # 측정 구간 밖에서 흘러간 시간(인자 처리, 파일 열기, 리포트 출력 등)
        other = total - measured
        if other > 0.001:
            share = other / total * 100 if total > 0 else 0.0
            lines.append(f"     {pad('기타', width)}  {other:8.3f}초  {share:5.1f}%")

        lines.append("  " + "─" * (width + 21))
        lines.append(f"     {pad('합계', width)}  {total:8.3f}초  100.0%")
        return "\n".join(lines)


#: 보고서에 표시할 구간 순서 (실제 실행 흐름대로)
PHASES = ("Impala 접속", "쿼리 실행 요청", "첫 배치 대기", "데이터 수신", "CSV 쓰기")


#: impyla에서 LDAP(사용자/비밀번호) 인증을 뜻하는 값
AUTH_MECHANISM = "PLAIN"

#: 인증이 필요 없는 방식
NO_AUTH = "NOSASL"


def build_connect_kwargs(args: argparse.Namespace, password: Optional[str]) -> Dict[str, Any]:
    """접속 인자를 만든다. 기본값은 TLS + LDAP.

    - auth_mechanism='PLAIN' 이 impyla에서 LDAP(사용자/비밀번호) 인증을 뜻한다.
      내부적으로는 SASL PLAIN으로 처리되며, 'LDAP' 을 줘도 같은 경로를 탄다.
    - use_ssl=True 로 TLS를 켜고, ca_cert로 서버 인증서를 검증한다.
      ca_cert를 주면 impyla가 check_hostname=True, CERT_REQUIRED 로 설정한다.
      주지 않으면 암호화는 되지만 인증서 검증은 하지 않는다.
    - 바이너리(21050)가 아니라 HTTP 엔드포인트(보통 28000)를 쓰는 환경이라면
      use_http_transport 를 켜야 한다. 안 그러면 Thrift가 EOF로 끊긴다.
    """
    kwargs: Dict[str, Any] = {
        "host": args.host,
        "port": args.port,
        "database": args.database,
        "auth_mechanism": args.auth_mechanism,
        "use_ssl": not args.no_ssl,
    }
    if args.auth_mechanism != NO_AUTH:
        kwargs["user"] = args.user
        kwargs["password"] = password
    if args.auth_mechanism == "GSSAPI":
        kwargs["kerberos_service_name"] = args.kerberos_service_name

    # None을 그대로 넘기면 impyla 버전에 따라 동작이 갈리므로 값이 있을 때만 넣는다
    if args.ca_cert:
        kwargs["ca_cert"] = args.ca_cert
    if args.timeout is not None:
        kwargs["timeout"] = args.timeout
    if args.http_transport:
        kwargs["use_http_transport"] = True
        kwargs["http_path"] = args.http_path
    return kwargs


def transport_error_hint(args: argparse.Namespace) -> str:
    """Thrift 전송 오류(EOF 등)가 났을 때 점검할 것들을 알려준다.

    "TSocket read 0 bytes" 나 "end of file" 은 서버가 핸드셰이크 도중 연결을
    끊었다는 뜻이다. 대부분 포트·전송 방식·TLS·인증 방식 중 하나가 서버 설정과
    어긋나서 생긴다. 메시지 자체로는 어느 쪽인지 알 수 없으므로 후보를 나열한다.
    """
    lines = [
        "서버가 핸드셰이크 도중 연결을 끊었습니다(EOF). 대개 아래 중 하나입니다.",
        "",
        f"  현재 설정: {args.host}:{args.port} "
        f"/ {'HTTP' if args.http_transport else '바이너리'} 전송"
        f" / TLS {'끄기' if args.no_ssl else '켜기'}"
        f" / 인증 {args.auth_mechanism}",
        "",
        "  1) 전송 방식과 포트가 맞는지",
        "     - 21050: 바이너리 HS2 (기본)",
        "     - 28000: HTTP HS2 → --http-transport 를 켜야 합니다",
        "     - 21000은 예전 beeswax, 25000은 웹 UI라 여기에 붙으면 EOF가 납니다",
    ]
    if not args.http_transport:
        lines.append("     지금 바이너리로 붙고 있으니 --http-transport 로 한 번 바꿔보세요.")
    else:
        lines.append(f"     지금 HTTP path는 '{args.http_path}' 입니다. 보통 cliservice 입니다.")

    lines += [
        "",
        "  2) TLS 설정이 서버와 맞는지",
    ]
    if args.no_ssl:
        lines.append("     지금 TLS를 끄고 있습니다. 서버가 TLS를 요구하면 --no-ssl 을 빼세요.")
    else:
        lines.append("     지금 TLS를 켜고 있습니다. 서버가 평문이면 --no-ssl 을 주세요.")

    lines += [
        "",
        "  3) 인증 방식이 서버와 맞는지",
        f"     지금 {args.auth_mechanism} 입니다. "
        "인증이 없는 서버면 --auth-mechanism NOSASL,",
        "     Kerberos면 GSSAPI 를 쓰세요.",
        "",
        "  4) 앞단에 로드밸런서나 프록시가 있다면 그쪽이 끊었을 수도 있습니다.",
        "",
        "  impala-shell 로 같은 조건이 되는지 먼저 확인하면 범위를 빨리 좁힐 수 있습니다.",
    ]
    return "\n".join(lines)


def parse_variables(items: Sequence[str]) -> Dict[str, str]:
    """``--var KEY=VALUE`` 목록을 딕셔너리로 바꾼다."""
    variables: Dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--var 는 KEY=VALUE 형식이어야 합니다: {item!r}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit(f"--var 의 이름이 비어 있습니다: {item!r}")
        variables[key] = value
    return variables


def resolve_query_file(given: str) -> str:
    """``--query-file`` 경로를 푼다.

    파일 이름만 주면 저장소의 ``sql/`` 에서 찾는다. 경로 구분자가 들어 있거나
    작업 디렉터리에 그 파일이 실제로 있으면 준 그대로 쓴다. 어느 쪽으로도 찾지
    못하면 ``sql/`` 에 무엇이 있는지 함께 알려준다.
    """
    if os.path.isfile(given):
        return given

    candidate = appconfig.SQL_DIR / given
    if os.sep not in given and candidate.is_file():
        return str(candidate)

    available = (
        sorted(p.name for p in appconfig.SQL_DIR.glob("*.sql"))
        if appconfig.SQL_DIR.is_dir()
        else []
    )
    message = f"쿼리 파일을 찾을 수 없습니다: {given}"
    if available:
        listing = "\n".join(f"    {name}" for name in available)
        message += f"\n  {appconfig.SQL_DIR} 에 있는 파일:\n{listing}"
    else:
        message += f"\n  {appconfig.SQL_DIR} 에 .sql 파일이 없습니다."
    raise SystemExit(message)


def render_query(sql: str, variables: Dict[str, str], source: str) -> str:
    """SQL을 Jinja 템플릿으로 보고 변수를 채운다.

    정의되지 않은 변수는 **오류** 다. Jinja 기본값은 빈 문자열로 조용히 치환하는
    것인데, SQL에서는 ``WHERE dt = ''`` 같은 문장이 조용히 만들어져 0건을 돌려주는
    편이 훨씬 위험하다. 오타를 바로 잡아내는 편이 낫다.

    HTML이 아니므로 자동 이스케이프는 켜지 않는다. 값은 SQL에 그대로 들어가므로,
    바깥에서 온 값을 넘긴다면 템플릿 쪽에서 따옴표와 검증을 책임져야 한다.
    """
    if "{{" not in sql and "{%" not in sql:
        # 템플릿 문법이 없으면 Jinja를 부르지 않는다. jinja2 미설치 환경에서도
        # 평범한 .sql 파일은 그대로 돌아간다.
        if variables:
            print(
                f"경고: {source} 에 템플릿 변수가 없어 --var 값이 쓰이지 않았습니다.",
                file=sys.stderr,
            )
        return sql

    try:
        from jinja2 import StrictUndefined, Template
        from jinja2 import TemplateError, UndefinedError
    except ImportError:
        raise SystemExit(
            "템플릿 변수를 쓰려면 Jinja2 가 필요합니다.\n"
            "    pip install Jinja2"
        )

    class OptionalUndefined(StrictUndefined):
        """``{% if x %}`` 로 물어보는 것은 되고, ``{{ x }}`` 로 출력하면 오류.

        StrictUndefined 는 참/거짓 판정까지 막아서 선택적 필터를 못 쓴다.
        여기서는 "값을 SQL에 넣는" 경우만 막으면 된다.
        """

        def __bool__(self) -> bool:
            return False

    try:
        return Template(sql, undefined=OptionalUndefined, keep_trailing_newline=True).render(
            **variables
        )
    except UndefinedError as exc:
        given = ", ".join(sorted(variables)) or "(없음)"
        raise SystemExit(
            f"{source} 의 템플릿 변수를 채우지 못했습니다: {exc}\n"
            f"  지금 준 변수: {given}\n"
            "  --var KEY=VALUE 로 지정하세요."
        )
    except TemplateError as exc:
        raise SystemExit(f"{source} 의 템플릿 문법이 잘못되었습니다: {exc}")


def read_query(args: argparse.Namespace) -> str:
    """``--query`` 또는 ``--query-file`` 에서 SQL을 읽는다.

    파일 내용은 템플릿을 채운 뒤 **그대로** 실행한다. 문장을 쪼개거나 세미콜론을
    떼어내지 않는다. 여러 줄로 이어진 쿼리도 줄바꿈째 그대로 넘어간다.

    파일을 읽을 때만 ``utf-8-sig`` 를 쓴다. 이건 SQL을 고치는 게 아니라 인코딩을
    제대로 해석하는 것이다. 윈도우 편집기로 저장한 .sql 앞머리에는 BOM(U+FEFF)이
    붙는데, utf-8로 읽으면 이 문자가 쿼리 첫 글자 앞에 남아 syntax error가 난다.
    utf-8-sig는 BOM이 있으면 벗기고 없으면 utf-8과 똑같이 동작한다.
    """
    variables = parse_variables(args.var)
    if args.query_file:
        path = resolve_query_file(args.query_file)
        with open(path, "r", encoding="utf-8-sig") as fp:
            return render_query(fp.read(), variables, path)
    return render_query(args.query, variables, "--query")


def query_error_hint(query: str, exc: Exception) -> str:
    """쿼리 실행이 문법 오류로 실패했을 때 짚어볼 것들을 알려준다."""
    message = str(exc).lower()
    if not any(k in message for k in ("syntax", "parseexception", "analysisexception")):
        return ""

    hints = [
        "SQL은 파일에 있는 그대로 서버에 보냅니다. --debug 로 실제로 보낸 내용을 볼 수 있습니다.",
    ]
    stripped = query.rstrip()
    if stripped.endswith(";"):
        hints.append(
            "끝에 세미콜론이 있습니다. Impala는 HS2로 받은 문장의 세미콜론을 "
            "거부할 수 있으니 빼고 다시 해보세요."
        )
    if ";" in stripped[:-1]:
        hints.append(
            "세미콜론이 문장 중간에도 있습니다. 파일에 문장이 여러 개면 "
            "한 번에 하나씩 나눠 실행해야 합니다."
        )
    return "\n".join(f"  - {h}" for h in hints)


def parse_session_settings(items: Sequence[str]) -> Dict[str, str]:
    """``KEY=VALUE`` 목록을 세션 설정 딕셔너리로 바꾼다."""
    settings: Dict[str, str] = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise SystemExit(f"--set 은 KEY=VALUE 형식이어야 합니다: {item!r}")
        settings[key.strip()] = value.strip()
    return settings


def resolve_password(args: argparse.Namespace) -> str:
    """설정 파일, 환경변수, 대화형 입력 순으로 비밀번호를 얻는다.

    설정의 impala.password 는 보통 ``${IMPALA_PASSWORD}`` 형태로 환경변수를
    가리킨다. 어느 경로든 명령행 인자로는 받지 않는다. ps 로 다른 사용자에게
    보이기 때문이다.
    """
    password = getattr(args, "config_password", None) or os.environ.get(args.password_env)
    if password:
        return password
    if sys.stdin.isatty():
        return getpass.getpass(f"{args.user}@{args.host} 비밀번호: ")
    raise SystemExit(
        f"비밀번호를 찾을 수 없습니다. 환경변수 {args.password_env} 를 설정하거나 "
        "설정의 impala.password 를 채우세요."
    )


@contextlib.contextmanager
def open_output(path: str, use_gzip: bool, encoding: str) -> Iterator[TextIO]:
    """CSV 출력 파일을 연다.

    csv 모듈은 자체적으로 개행을 제어하므로 newline='' 로 열어야 한다.
    그러지 않으면 윈도우에서 빈 줄이 하나씩 끼어든다.
    """
    if use_gzip:
        handle = gzip.open(path, "wt", encoding=encoding, newline="")
    else:
        handle = open(path, "w", encoding=encoding, newline="")
    try:
        yield handle
    finally:
        handle.close()


def make_writer(
    handle: TextIO, delimiter: str, quote: bool, escapechar: Optional[str]
) -> Any:
    """CSV writer를 만든다.

    따옴표를 쓰지 않으면(기본) 값 안에 구분자나 줄바꿈이 들어 있을 때 이스케이프할
    문자가 필요하다. 없으면 csv 모듈이 그 행에서 예외를 내고 내보내기가 중단된다.
    구분자가 값에 등장하지 않는 한 이스케이프 문자는 출력에 나타나지 않는다.
    """
    if quote:
        return csv.writer(handle, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
    # quotechar=None 이어야 값 안의 큰따옴표를 건드리지 않는다.
    # 그냥 두면 따옴표를 쓰지 않는데도 " 앞에 이스케이프 문자가 붙는다.
    return csv.writer(
        handle,
        delimiter=delimiter,
        quoting=csv.QUOTE_NONE,
        quotechar=None,
        escapechar=escapechar or None,
    )


def export(
    cursor: Any,
    query: str,
    handle: TextIO,
    timer: PhaseTimer,
    batch_size: int,
    delimiter: str,
    null_string: str,
    write_header: bool,
    progress_every: int,
    quote: bool = False,
    escapechar: Optional[str] = "\\",
) -> int:
    """쿼리를 실행해 CSV로 쓰고 행 수를 돌려준다."""
    with timer.measure("쿼리 실행 요청"):
        cursor.arraysize = batch_size
        cursor.execute(query)

    columns = [desc[0].split(".")[-1] for desc in (cursor.description or [])]
    writer = make_writer(handle, delimiter, quote, escapechar)

    if write_header and columns:
        with timer.measure("CSV 쓰기"):
            writer.writerow(columns)

    total = 0
    first_batch = True
    while True:
        # 첫 배치는 Impala가 결과를 만들어내기까지 기다리는 시간이라 따로 잰다
        phase = "첫 배치 대기" if first_batch else "데이터 수신"
        with timer.measure(phase):
            rows = cursor.fetchmany(batch_size)
        first_batch = False

        if not rows:
            break

        with timer.measure("CSV 쓰기"):
            # csv 모듈은 None을 빈 문자열로 쓴다. NULL을 따로 표시하려면 치환한다.
            if null_string:
                rows = [[null_string if v is None else v for v in row] for row in rows]
            writer.writerows(rows)

        total += len(rows)
        if progress_every and total % progress_every < len(rows):
            elapsed = timer.total
            rate = total / elapsed if elapsed > 0 else 0
            print(f"  {total:,}건 ({rate:,.0f} rows/s)", file=sys.stderr)

    return total


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024 or unit == "GB":
            return f"{size:,.1f}{unit}"
        size /= 1024
    return f"{size:,.1f}GB"


#: 설정의 impala 섹션에서 읽어 쓰는 키. 나머지 키는 무시한다.
IMPALA_SETTINGS = (
    "host",
    "port",
    "database",
    "user",
    "password",
    "auth_mechanism",
    "kerberos_service_name",
    "use_ssl",
    "ca_cert",
    "timeout",
    "session_settings",
)

#: 파일 경로로 다루는 키. 상대 경로는 설정 파일 위치를 기준으로 푼다.
IMPALA_PATH_SETTINGS = ("ca_cert",)


def load_impala_settings(args: argparse.Namespace) -> Dict[str, Any]:
    """설정 파일의 impala 섹션을 읽는다.

    ``--config`` 로 파일을 직접 지정했는데 impala 섹션이 없으면 오타일 가능성이
    높으므로 알린다. 기본 파일이라면 다른 스크립트용 설정만 들어 있을 수 있으니
    넘어간다.
    """
    path = appconfig.resolve_config_path(args)
    return appconfig.load_section(
        path,
        "impala",
        IMPALA_SETTINGS,
        required=bool(args.config),
        path_keys=IMPALA_PATH_SETTINGS,
    )


def apply_config(
    args: argparse.Namespace, config: Dict[str, Any], parser: argparse.ArgumentParser
) -> None:
    """설정 파일 값을 args 에 채운다. 명령행으로 준 값이 항상 우선한다.

    설정에 없고 명령행에도 없는 값은 각 항목의 기본값으로 떨어진다. 접속에 반드시
    필요한 host 와 user 만 끝까지 비어 있으면 오류다.
    """
    args.host = appconfig.pick(args.host, config["host"])
    args.port = int(appconfig.pick(args.port, config["port"], 21050))
    args.database = appconfig.pick(args.database, config["database"], "default")
    args.user = appconfig.pick(args.user, config["user"])
    args.auth_mechanism = appconfig.pick(
        args.auth_mechanism, config["auth_mechanism"], AUTH_MECHANISM
    )
    args.kerberos_service_name = appconfig.pick(
        args.kerberos_service_name, config["kerberos_service_name"], "impala"
    )
    args.ca_cert = appconfig.pick(args.ca_cert, config["ca_cert"])
    args.timeout = appconfig.pick(args.timeout, config["timeout"])

    # --no-ssl 은 store_true 라 "주지 않음"과 False 를 구분할 수 없다. 플래그를
    # 주지 않았을 때만 설정의 use_ssl 을 본다.
    if not args.no_ssl and config["use_ssl"] is False:
        args.no_ssl = True

    # 설정의 session_settings 를 --set 보다 앞에 둬서 명령행이 덮어쓰게 한다
    settings = config["session_settings"]
    if isinstance(settings, dict):
        args.set = [f"{k}={v}" for k, v in settings.items()] + list(args.set)

    # 비밀번호는 설정 > 환경변수 > 대화형 입력 순으로 찾는다
    args.config_password = config["password"] or None

    if args.auth_mechanism != NO_AUTH and not args.user:
        parser.error("사용자를 알 수 없습니다. -u/--user 를 주거나 설정의 impala.user 를 채우세요.")
    if not args.host:
        parser.error("호스트를 알 수 없습니다. --host 를 주거나 설정의 impala.host 를 채우세요.")

    # 없는 인증서를 그대로 넘기면 impyla가 알아보기 어려운 SSL 오류를 낸다.
    # 조용히 무시하면 검증 없이 접속하게 되므로, 여기서 경로를 짚어 알린다.
    if args.ca_cert and not args.no_ssl and not os.path.isfile(args.ca_cert):
        parser.error(
            f"CA 인증서를 찾을 수 없습니다: {args.ca_cert}\n"
            "  설정의 impala.ca_cert 경로를 고치거나, 그 위치에 인증서를 두세요.\n"
            "  인증서 검증 없이 접속하려면 impala.ca_cert 를 비우세요."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bin/query-to-csv",
        description="Impala 쿼리 결과를 CSV로 저장하고 구간별 소요 시간을 표시합니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    appconfig.add_config_arguments(parser)

    # 설정 파일에서 채울 수 있는 값은 기본값을 None 으로 둔다. 그래야 사용자가
    # 직접 준 값과 argparse 기본값을 구분해 우선순위를 매길 수 있다.
    conn = parser.add_argument_group("접속 (TLS + LDAP)")
    conn.add_argument("--host", help="Impala 데몬 호스트")
    conn.add_argument("--port", type=int, help="기본 21050")
    conn.add_argument("-d", "--database")
    conn.add_argument("-u", "--user", help="LDAP 사용자")
    conn.add_argument(
        "--password-env",
        default="IMPALA_PASSWORD",
        metavar="ENV",
        help="비밀번호를 담은 환경변수 이름 (기본 IMPALA_PASSWORD)",
    )
    conn.add_argument("--ca-cert", help="서버 인증서 검증에 쓸 CA 인증서 경로")
    conn.add_argument("--timeout", type=int, help="접속 타임아웃(초)")
    conn.add_argument(
        "--auth-mechanism",
        choices=["PLAIN", "LDAP", "NOSASL", "GSSAPI"],
        help=f"PLAIN/LDAP=LDAP 인증(기본 {AUTH_MECHANISM}), NOSASL=인증 없음, GSSAPI=Kerberos",
    )
    conn.add_argument(
        "--kerberos-service-name",
        help="GSSAPI일 때 쓸 서비스명 (기본 impala)",
    )
    conn.add_argument("--no-ssl", action="store_true", help="TLS를 끄고 평문으로 접속")
    conn.add_argument(
        "--sasl-backend",
        default="puresasl",
        choices=["puresasl", "sasl"],
        help="SASL 구현. puresasl(기본, impyla 기본값) 또는 sasl(Cyrus, Python 3.10 이하만)",
    )
    conn.add_argument(
        "--debug",
        action="store_true",
        help="SASL 핸드셰이크를 포함한 impyla 디버그 로그 출력",
    )
    conn.add_argument(
        "--http-transport",
        action="store_true",
        help="HS2 HTTP 엔드포인트(보통 28000)로 접속. 바이너리로 붙어 EOF가 나면 이걸 켜세요.",
    )
    conn.add_argument(
        "--http-path",
        default="cliservice",
        help="HTTP 전송일 때의 경로 (기본 cliservice)",
    )

    query = parser.add_argument_group("쿼리")
    source = query.add_mutually_exclusive_group(required=True)
    source.add_argument("-q", "--query", help="실행할 SELECT 문")
    source.add_argument(
        "-f",
        "--query-file",
        help=f"SELECT 문이 담긴 .sql 파일. 이름만 주면 {appconfig.SQL_DIR} 에서 찾습니다.",
    )
    query.add_argument(
        "-V",
        "--var",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="쿼리 템플릿에 채울 변수 (예: --var dt=2026-08-01). 여러 번 지정 가능",
    )
    query.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="세션 설정 (예: --set MEM_LIMIT=8g). 여러 번 지정 가능",
    )

    out = parser.add_argument_group("출력")
    out.add_argument("-o", "--output", required=True, help="저장할 CSV 경로")
    out.add_argument("--gzip", action="store_true", help="gzip으로 압축해 저장")
    out.add_argument("--delimiter", default="`", help="컬럼 구분자 (기본 `)")
    out.add_argument(
        "--quote",
        action="store_true",
        help='값을 큰따옴표로 감싼다. 기본은 끔(감싸지 않음).',
    )
    out.add_argument(
        "--escapechar",
        default="\\",
        help="따옴표를 쓰지 않을 때 값 안의 구분자를 이스케이프할 문자 (기본 \\). "
        "빈 문자열을 주면 이스케이프하지 않지만, 값에 구분자가 있으면 오류로 중단됩니다.",
    )
    out.add_argument(
        "--encoding",
        default="utf-8",
        help="파일 인코딩. 엑셀에서 한글이 깨지면 utf-8-sig를 쓰세요.",
    )
    out.add_argument("--no-header", action="store_true", help="헤더 행을 쓰지 않음")
    out.add_argument(
        "--null-string",
        default="",
        help="NULL을 표시할 문자열 (기본: 빈 값). 예: --null-string '\\N'",
    )
    out.add_argument("--batch-size", type=int, default=50_000, help="fetchmany 크기")
    out.add_argument(
        "--progress-every", type=int, default=100_000, help="진행 상황 출력 간격(행)"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_impala_settings(args)
    apply_config(args, config, parser)
    timer = PhaseTimer(PHASES)

    query = read_query(args)
    session_settings = parse_session_settings(args.set)
    password = resolve_password(args) if args.auth_mechanism != NO_AUTH else None
    connect_kwargs = build_connect_kwargs(args, password)

    # 지연 임포트: --help는 impyla 없이도 뜬다.
    # impyla가 없거나 impala.py 파일에 가려져 있으면 원인을 짚어 알려준다.
    if args.debug:
        # impyla와 thrift_sasl이 핸드셰이크 과정을 DEBUG로 남긴다
        import logging

        logging.basicConfig(
            level=logging.DEBUG, stream=sys.stderr, format="%(name)s %(levelname)s %(message)s"
        )
        # 서버로 보내는 SQL. 파일 내용 그대로다.
        print(f"--- 실행할 SQL ---\n{query}\n------------------", file=sys.stderr)

    try:
        dbapi = import_impala_dbapi()
        check_auth_dependencies(args.auth_mechanism)
        if args.sasl_backend == "sasl" and args.auth_mechanism != NO_AUTH:
            use_cyrus_sasl()
    except ImportError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 3

    transport = "HTTP" if args.http_transport else "바이너리"
    tls = "평문" if args.no_ssl else "TLS"
    who = f"{args.user}@" if args.auth_mechanism != NO_AUTH else ""
    # HTTP 전송은 SASL이 아니라 HTTP 기본 인증 헤더를 쓴다
    sasl = ""
    if args.auth_mechanism != NO_AUTH and not args.http_transport:
        sasl = f", SASL {args.auth_mechanism}/{args.sasl_backend}"
    print(
        f"접속: {who}{args.host}:{args.port} ({tls}, {args.auth_mechanism}, {transport} 전송{sasl})",
        file=sys.stderr,
    )

    try:
        with timer.measure("Impala 접속"):
            conn = dbapi.connect(**connect_kwargs)
    except Exception as exc:
        # 전송 오류(EOF 등)는 메시지만으로 원인을 알기 어려워 점검 목록을 붙여준다.
        # impyla 설치에 따라 thrift와 thriftpy2 중 어느 쪽을 쓰는지 달라지므로
        # 모듈 경로로 잡지 않고 예외 이름으로 판별한다.
        if type(exc).__name__ != "TTransportException":
            raise
        print(f"\n접속 실패: {exc}\n", file=sys.stderr)
        print(transport_error_hint(args), file=sys.stderr)
        return 4

    try:
        cursor = conn.cursor()
        try:
            for key, value in session_settings.items():
                cursor.execute(f"SET {key}={value}")

            with open_output(args.output, args.gzip, args.encoding) as handle:
                rows = export(
                    cursor,
                    query,
                    handle,
                    timer,
                    batch_size=args.batch_size,
                    delimiter=args.delimiter,
                    null_string=args.null_string,
                    write_header=not args.no_header,
                    progress_every=args.progress_every,
                    quote=args.quote,
                    escapechar=args.escapechar,
                )
        except Exception as exc:
            hint = query_error_hint(query, exc)
            if not hint:
                raise
            print(f"\n쿼리 실행 실패: {exc}\n", file=sys.stderr)
            print(hint, file=sys.stderr)
            return 5
        finally:
            cursor.close()
    finally:
        conn.close()

    size = os.path.getsize(args.output)
    elapsed = timer.total
    print(timer.report())
    print()
    print(f"{args.output}  {human(size)}  {rows:,}행")
    if elapsed > 0:
        print(f"평균 {rows / elapsed:,.0f} rows/s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
