"""Impala에서 쿼리를 실행해 CSV 파일로 저장하고, 구간별 소요 시간을 보여준다.

TLS(SSL) 위에서 LDAP 인증(auth_mechanism=PLAIN)으로 접속하는 구성을 전제로 한다.

이 스크립트는 **단독으로 동작한다.** 표준 라이브러리와 impyla 외에는 아무것도
필요하지 않으므로, 이 파일 하나만 다른 곳으로 복사해서 써도 된다.

    pip install impyla pure-sasl thrift-sasl
    # "Failed building wheel for pure-sasl" 이 나면
    #     pip install --use-pep517 pure-sasl thrift-sasl

    export IMPALA_PASSWORD='...'
    python query_to_csv.py \
        --host impala.example.com \
        --user etl_user \
        --ca-cert /etc/ssl/certs/impala-ca.pem \
        --query "SELECT * FROM sales.orders WHERE order_dt = '2026-08-01'" \
        --output orders.csv

    # 쿼리를 파일에서 읽고 gzip으로 압축해 저장
    python query_to_csv.py --host impala.example.com --user etl_user \
        --query-file daily_orders.sql --output orders.csv.gz --gzip

비밀번호는 명령행 인자로 받지 않는다. ps로 다른 사용자에게 노출되기 때문에
환경변수(기본 IMPALA_PASSWORD)나 대화형 입력으로만 받는다.
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
    """환경변수 또는 대화형 입력으로 비밀번호를 얻는다."""
    password = os.environ.get(args.password_env)
    if password:
        return password
    if sys.stdin.isatty():
        return getpass.getpass(f"{args.user}@{args.host} 비밀번호: ")
    raise SystemExit(
        f"비밀번호를 찾을 수 없습니다. 환경변수 {args.password_env} 를 설정하세요."
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
) -> int:
    """쿼리를 실행해 CSV로 쓰고 행 수를 돌려준다."""
    with timer.measure("쿼리 실행 요청"):
        cursor.arraysize = batch_size
        cursor.execute(query)

    columns = [desc[0].split(".")[-1] for desc in (cursor.description or [])]
    writer = csv.writer(handle, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Impala 쿼리 결과를 CSV로 저장하고 구간별 소요 시간을 표시합니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    conn = parser.add_argument_group("접속 (TLS + LDAP)")
    conn.add_argument("--host", required=True, help="Impala 데몬 호스트")
    conn.add_argument("--port", type=int, default=21050, help="기본 21050")
    conn.add_argument("-d", "--database", default="default")
    conn.add_argument("-u", "--user", required=True, help="LDAP 사용자")
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
        default=AUTH_MECHANISM,
        choices=["PLAIN", "LDAP", "NOSASL", "GSSAPI"],
        help="PLAIN/LDAP=LDAP 인증(기본), NOSASL=인증 없음, GSSAPI=Kerberos",
    )
    conn.add_argument(
        "--kerberos-service-name",
        default="impala",
        help="GSSAPI일 때 쓸 서비스명 (기본 impala)",
    )
    conn.add_argument("--no-ssl", action="store_true", help="TLS를 끄고 평문으로 접속")
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
    source.add_argument("-f", "--query-file", help="SELECT 문이 담긴 파일")
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
    out.add_argument("--delimiter", default=",", help="구분자 (기본 ,)")
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
    args = build_parser().parse_args(argv)
    timer = PhaseTimer(PHASES)

    query = args.query
    if args.query_file:
        with open(args.query_file, "r", encoding="utf-8") as fp:
            query = fp.read()
    query = query.strip().rstrip(";")

    session_settings = parse_session_settings(args.set)
    password = resolve_password(args) if args.auth_mechanism != NO_AUTH else None
    connect_kwargs = build_connect_kwargs(args, password)

    # 지연 임포트: --help는 impyla 없이도 뜬다.
    # impyla가 없거나 impala.py 파일에 가려져 있으면 원인을 짚어 알려준다.
    try:
        dbapi = import_impala_dbapi()
        check_auth_dependencies(args.auth_mechanism)
    except ImportError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 3

    transport = "HTTP" if args.http_transport else "바이너리"
    tls = "평문" if args.no_ssl else "TLS"
    who = f"{args.user}@" if args.auth_mechanism != NO_AUTH else ""
    print(
        f"접속: {who}{args.host}:{args.port} "
        f"({tls}, {args.auth_mechanism}, {transport} 전송)",
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
                )
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
