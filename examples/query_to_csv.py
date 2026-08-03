"""Impala에서 쿼리를 실행해 CSV 파일로 저장하고, 구간별 소요 시간을 보여준다.

TLS(SSL) 위에서 LDAP 인증(auth_mechanism=PLAIN)으로 접속하는 구성을 전제로 한다.

    export IMPALA_PASSWORD='...'
    python examples/query_to_csv.py \
        --host impala.example.com \
        --user etl_user \
        --ca-cert /etc/ssl/certs/impala-ca.pem \
        --query "SELECT * FROM sales.orders WHERE order_dt = '2026-08-01'" \
        --output orders.csv

    # 쿼리를 파일에서 읽고 gzip으로 압축해 저장
    python examples/query_to_csv.py --host impala.example.com --user etl_user \
        --query-file daily_orders.sql --output orders.csv.gz --gzip

비밀번호는 명령행 인자로 받지 않는다. ps로 다른 사용자에게 노출되기 때문에
환경변수(기본 IMPALA_PASSWORD)나 대화형 입력으로만 받는다.
"""

import argparse
import contextlib
import csv
import getpass
import gzip
import os
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, TextIO

# 저장소를 설치하지 않고 바로 실행할 수 있도록 최상위 디렉터리를 경로에 넣는다
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from impala_to_greenplum.config import ImpalaConfig  # noqa: E402
from impala_to_greenplum.source import (  # noqa: E402
    check_auth_dependencies,
    import_impala_dbapi,
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


def build_config(args: argparse.Namespace, password: str) -> ImpalaConfig:
    """TLS + LDAP 접속 설정을 만든다.

    - auth_mechanism='PLAIN' 이 impyla에서 LDAP(사용자/비밀번호) 인증을 뜻한다.
    - use_ssl=True 로 TLS를 켜고, ca_cert로 서버 인증서를 검증한다.
      ca_cert를 주지 않으면 암호화는 되지만 인증서 검증은 하지 않으므로,
      운영 환경에서는 CA 인증서 경로를 지정하는 편이 안전하다.
    """
    return ImpalaConfig(
        host=args.host,
        port=args.port,
        database=args.database,
        user=args.user,
        password=password,
        auth_mechanism="PLAIN",
        use_ssl=True,
        ca_cert=args.ca_cert,
        timeout=args.timeout,
    )


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

    config = build_config(args, resolve_password(args))
    for item in args.set:
        key, _, value = item.partition("=")
        config.session_settings[key.strip()] = value.strip()

    # 지연 임포트: --help는 impyla 없이도 뜬다.
    # impyla가 없거나 impala.py 파일에 가려져 있으면 원인을 짚어 알려준다.
    try:
        dbapi = import_impala_dbapi()
        check_auth_dependencies(config.auth_mechanism)
    except ImportError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 3

    print(f"접속: {args.user}@{args.host}:{args.port} (TLS, LDAP)", file=sys.stderr)
    with timer.measure("Impala 접속"):
        conn = dbapi.connect(**config.connect_kwargs())

    try:
        cursor = conn.cursor()
        try:
            for key, value in config.session_settings.items():
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
