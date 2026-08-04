"""Greenplum에 SQL을 실행한다. 결과가 있으면 표나 CSV로 보여준다.

접속 정보는 conf/config.yaml 의 greenplum 섹션에서 자동으로 읽는다. 명령행으로 준
값이 항상 우선하므로, 설정을 채워두면 쿼리만 주면 된다.

    pip install psycopg2-binary PyYAML

    # sql/ 의 템플릿에 변수를 채워 실행
    bin/gp-query -f order_summary.sql --var dt=2026-08-01

    # 결과를 CSV로 저장
    bin/gp-query -q "SELECT * FROM staging.orders" -o orders.csv

    # DDL/DML. 기본은 실행 후 커밋한다.
    bin/gp-query -q "TRUNCATE staging.orders"

    # 실제로 반영하지 않고 무엇이 바뀌는지만 확인 (실행 후 롤백)
    bin/gp-query -f cleanup.sql --var dt=2026-08-01 --dry-run

SELECT 이든 DDL 이든 한 트랜잭션에서 실행하고, 성공하면 커밋한다. 중간에 실패하면
전부 롤백된다. ``--dry-run`` 은 실행은 하되 항상 롤백한다.

비밀번호는 명령행 인자로 받지 않는다. ps로 다른 사용자에게 노출되기 때문에
설정 파일(보통 ``${GP_PASSWORD}`` 참조), 환경변수, 대화형 입력으로만 받는다.
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
from typing import Any, Dict, Iterator, List, Optional, Sequence, TextIO

import appconfig
import sqlfile

#: 설정의 greenplum 섹션에서 읽어 쓰는 키. 나머지 키는 무시한다.
GREENPLUM_SETTINGS = (
    "host",
    "port",
    "database",
    "user",
    "password",
    "schema",
    "sslmode",
    "connect_timeout",
    "session_sql",
)

#: 표로 보여줄 때 기본으로 출력할 최대 행 수. 0이면 제한 없음.
DEFAULT_MAX_ROWS = 100


def import_psycopg2() -> Any:
    """psycopg2를 가져온다. 없으면 설치 방법을 알려준다."""
    try:
        import psycopg2  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "psycopg2가 설치되어 있지 않습니다.\n"
            "    pip install psycopg2-binary\n"
            "  소스 빌드를 쓴다면 libpq-dev 를 먼저 설치하세요."
        ) from exc
    return psycopg2


def load_greenplum_settings(args: argparse.Namespace) -> Dict[str, Any]:
    """설정 파일의 greenplum 섹션을 읽는다.

    ``--config`` 로 파일을 직접 지정했는데 greenplum 섹션이 없으면 오타일 가능성이
    높으므로 알린다. 기본 파일이라면 다른 스크립트용 설정만 들어 있을 수 있으니
    넘어간다.
    """
    path = appconfig.resolve_config_path(args)
    return appconfig.load_section(
        path, "greenplum", GREENPLUM_SETTINGS, required=bool(args.config)
    )


def apply_config(
    args: argparse.Namespace,
    config: Dict[str, Any],
    parser: argparse.ArgumentParser,
    sql_config: Optional[Dict[str, Any]] = None,
) -> None:
    """설정 파일 값을 args 에 채운다. 명령행으로 준 값이 항상 우선한다."""
    args.host = appconfig.pick(args.host, config["host"])
    args.port = int(appconfig.pick(args.port, config["port"], 5432))
    args.database = appconfig.pick(args.database, config["database"])
    args.user = appconfig.pick(args.user, config["user"])
    args.schema = appconfig.pick(args.schema, config["schema"])
    args.sslmode = appconfig.pick(args.sslmode, config["sslmode"])
    args.timeout = appconfig.pick(args.timeout, config["connect_timeout"])
    args.sql_dir = sqlfile.resolve_sql_dir(args, sql_config)

    # 설정의 session_sql 을 --session-sql 보다 앞에 둬서 명령행이 뒤에 오게 한다
    session = config["session_sql"]
    if isinstance(session, str):
        session = [session]
    if isinstance(session, list):
        args.session_sql = [str(s) for s in session] + list(args.session_sql)

    args.config_password = config["password"] or None

    for name, flag in (("host", "--host"), ("database", "-d/--database"), ("user", "-u/--user")):
        if not getattr(args, name):
            parser.error(
                f"{name} 을(를) 알 수 없습니다. {flag} 를 주거나 "
                f"설정의 greenplum.{name} 을 채우세요."
            )


def resolve_password(args: argparse.Namespace) -> Optional[str]:
    """설정 파일, 환경변수, 대화형 입력 순으로 비밀번호를 얻는다.

    셋 다 없으면 None을 돌려준다. .pgpass 나 trust 인증을 쓰는 환경도 있어서,
    비밀번호가 없다는 이유만으로 막지는 않는다.
    """
    password = getattr(args, "config_password", None) or os.environ.get(args.password_env)
    if password:
        return password
    if sys.stdin.isatty() and not args.no_password_prompt:
        entered = getpass.getpass(f"{args.user}@{args.host} 비밀번호(없으면 Enter): ")
        return entered or None
    return None


def build_connect_kwargs(args: argparse.Namespace, password: Optional[str]) -> Dict[str, Any]:
    """psycopg2.connect 인자를 만든다."""
    kwargs: Dict[str, Any] = {
        "host": args.host,
        "port": args.port,
        "dbname": args.database,
        "user": args.user,
    }
    # None을 그대로 넘기면 libpq가 빈 값으로 해석할 수 있어 값이 있을 때만 넣는다
    if password:
        kwargs["password"] = password
    if args.sslmode:
        kwargs["sslmode"] = args.sslmode
    if args.timeout is not None:
        kwargs["connect_timeout"] = int(args.timeout)
    return kwargs


def display_width(text: str) -> int:
    """터미널에서 차지하는 칸 수. 한글·한자는 두 칸을 쓴다."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text: str, width: int) -> str:
    """표시 폭 기준으로 오른쪽을 채운다(한글이 섞여도 열이 맞는다)."""
    return text + " " * max(0, width - display_width(text))


def format_value(value: Any, null_string: str) -> str:
    if value is None:
        return null_string
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render_table(
    columns: Sequence[str], rows: Sequence[Sequence[Any]], null_string: str = "NULL"
) -> str:
    """psql 처럼 열을 맞춰 표로 만든다."""
    cells = [[format_value(v, null_string) for v in row] for row in rows]
    widths = [display_width(c) for c in columns]
    for row in cells:
        for i, text in enumerate(row):
            widths[i] = max(widths[i], display_width(text))

    lines = [" | ".join(pad(c, w) for c, w in zip(columns, widths)).rstrip()]
    lines.append("-+-".join("-" * w for w in widths))
    for row in cells:
        lines.append(" | ".join(pad(t, w) for t, w in zip(row, widths)).rstrip())
    return "\n".join(lines)


@contextlib.contextmanager
def open_output(path: str, use_gzip: bool, encoding: str) -> Iterator[TextIO]:
    """CSV 출력 파일을 연다.

    csv 모듈은 자체적으로 개행을 제어하므로 newline='' 로 열어야 한다.
    """
    if use_gzip:
        handle = gzip.open(path, "wt", encoding=encoding, newline="")
    else:
        handle = open(path, "w", encoding=encoding, newline="")
    try:
        yield handle
    finally:
        handle.close()


def write_csv(
    handle: TextIO,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    delimiter: str,
    null_string: str,
    write_header: bool,
) -> int:
    """결과를 CSV로 쓴다. query-to-csv 와 같은 기본값(백틱 구분, 따옴표 없음)이다."""
    writer = csv.writer(
        handle,
        delimiter=delimiter,
        quoting=csv.QUOTE_NONE,
        quotechar=None,
        escapechar="\\",
    )
    if write_header:
        writer.writerow(columns)
    for row in rows:
        writer.writerow([null_string if v is None else v for v in row])
    return len(rows)


def column_names(description: Sequence[Any]) -> List[str]:
    return [d[0] for d in description]


def run(cursor: Any, sql: str, args: argparse.Namespace) -> int:
    """SQL을 실행하고 결과를 내보낸다. 종료 코드를 돌려준다."""
    started = time.monotonic()
    cursor.execute(sql)
    elapsed = time.monotonic() - started

    if cursor.description is None:
        # INSERT/UPDATE/DELETE/DDL. rowcount 는 DDL 이면 -1 이다.
        affected = cursor.rowcount
        detail = f"{affected:,}행" if affected is not None and affected >= 0 else "완료"
        print(f"{detail}, {elapsed:.2f}초", file=sys.stderr)
        return 0

    columns = column_names(cursor.description)
    rows = cursor.fetchall()

    if args.output:
        with open_output(args.output, args.gzip, args.encoding) as handle:
            written = write_csv(
                handle,
                columns,
                rows,
                delimiter=args.delimiter,
                null_string=args.null_string,
                write_header=not args.no_header,
            )
        size = os.path.getsize(args.output)
        print(f"{args.output}  {size:,} bytes  {written:,}행, {elapsed:.2f}초", file=sys.stderr)
        return 0

    shown = rows if args.max_rows <= 0 else rows[: args.max_rows]
    if rows:
        print(render_table(columns, shown, args.null_string or "NULL"))
    else:
        print(" | ".join(columns))
        print("(0행)")

    note = f"{len(rows):,}행, {elapsed:.2f}초"
    if len(shown) < len(rows):
        note += f" (앞 {len(shown):,}행만 표시 — 전부 보려면 --max-rows 0)"
    print(note, file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bin/gp-query",
        description="Greenplum에 SQL을 실행하고 결과를 표나 CSV로 보여줍니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "예시:\n"
            "  # sql/ 의 템플릿에 변수를 채워 실행\n"
            "  bin/gp-query -f order_summary.sql -V dt=2026-08-01\n"
            "\n"
            "  # 결과를 CSV로 저장\n"
            "  bin/gp-query -q \"SELECT * FROM staging.orders\" -o orders.csv\n"
            "\n"
            "  # DDL/DML (성공하면 커밋)\n"
            "  bin/gp-query -q \"TRUNCATE staging.orders\"\n"
            "\n"
            "  # 실행은 하되 반영하지 않고 확인만 (항상 롤백)\n"
            "  bin/gp-query -f cleanup.sql -V dt=2026-08-01 --dry-run\n"
            "\n"
            f"접속 정보는 {appconfig.DEFAULT_CONFIG} 의\n"
            "greenplum 섹션에서 자동으로 읽습니다. 아래 인자를 주면 그 값이 우선합니다.\n"
            "\n"
            "비밀번호는 인자로 받지 않습니다. 설정의 greenplum.password, 환경변수\n"
            "(기본 GP_PASSWORD), 대화형 입력 순으로 찾습니다.\n"
        ),
    )
    appconfig.add_config_arguments(parser)

    # 설정 파일에서 채울 수 있는 값은 기본값을 None 으로 둔다. 그래야 사용자가
    # 직접 준 값과 argparse 기본값을 구분해 우선순위를 매길 수 있다.
    conn = parser.add_argument_group("접속")
    conn.add_argument("--host", help="Greenplum 마스터 호스트")
    conn.add_argument("--port", type=int, help="기본 5432")
    conn.add_argument("-d", "--database", help="데이터베이스 이름")
    conn.add_argument("-u", "--user", help="접속 계정")
    conn.add_argument(
        "--password-env",
        default="GP_PASSWORD",
        metavar="ENV",
        help="비밀번호를 담은 환경변수 이름 (기본 GP_PASSWORD)",
    )
    conn.add_argument(
        "--no-password-prompt",
        action="store_true",
        help="비밀번호를 물어보지 않는다. .pgpass 나 trust 인증을 쓸 때.",
    )
    conn.add_argument("--schema", help="실행 전에 search_path 로 지정할 스키마")
    conn.add_argument("--sslmode", help="libpq sslmode (require, verify-full 등)")
    conn.add_argument("--timeout", type=int, help="접속 타임아웃(초)")

    sqlfile.add_query_arguments(parser)

    session = parser.add_argument_group("세션")
    session.add_argument(
        "--session-sql",
        action="append",
        default=[],
        metavar="SQL",
        help="쿼리 전에 실행할 SET 문 등. 여러 번 지정 가능",
    )
    session.add_argument(
        "--dry-run",
        action="store_true",
        help="실행은 하되 커밋하지 않고 롤백합니다.",
    )
    session.add_argument(
        "--debug",
        action="store_true",
        help="실제로 서버에 보내는 SQL을 출력합니다.",
    )

    out = parser.add_argument_group("출력")
    out.add_argument("-o", "--output", help="결과를 CSV로 저장할 경로 (생략하면 표로 출력)")
    out.add_argument("--gzip", action="store_true", help="gzip으로 압축해 저장")
    out.add_argument("--delimiter", default="`", help="CSV 컬럼 구분자 (기본 `)")
    out.add_argument("--encoding", default="utf-8", help="CSV 파일 인코딩")
    out.add_argument("--no-header", action="store_true", help="CSV에 헤더 행을 쓰지 않음")
    out.add_argument(
        "--null-string",
        default="",
        help="NULL을 표시할 문자열 (CSV 기본: 빈 값, 표 기본: NULL)",
    )
    out.add_argument(
        "--max-rows",
        type=int,
        default=DEFAULT_MAX_ROWS,
        help=f"표로 출력할 최대 행 수 (기본 {DEFAULT_MAX_ROWS}, 0이면 제한 없음)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    # 인자 없이 실행하면 무엇을 할 수 있는지 보여준다. 오류가 아니므로 0으로 끝낸다.
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        parser.print_help()
        return 0

    args = parser.parse_args(argv)
    apply_config(args, load_greenplum_settings(args), parser, sqlfile.load_sql_settings(args))

    sql = sqlfile.read_query(args)
    if args.debug:
        print(f"--- 실행할 SQL ---\n{sql}\n------------------", file=sys.stderr)

    try:
        psycopg2 = import_psycopg2()
    except ImportError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 3

    password = resolve_password(args)
    print(
        f"접속: {args.user}@{args.host}:{args.port}/{args.database}",
        file=sys.stderr,
    )

    try:
        conn = psycopg2.connect(**build_connect_kwargs(args, password))
    except Exception as exc:
        print(f"\n접속 실패: {exc}", file=sys.stderr)
        return 4

    try:
        cursor = conn.cursor()
        try:
            if args.schema:
                cursor.execute(f"SET search_path TO {args.schema}")
            for statement in args.session_sql:
                cursor.execute(statement)

            code = run(cursor, sql, args)
        except Exception as exc:
            conn.rollback()
            print(f"\n쿼리 실행 실패: {exc}", file=sys.stderr)
            return 5
        finally:
            cursor.close()

        if args.dry_run:
            conn.rollback()
            print("--dry-run 이므로 롤백했습니다. 반영되지 않았습니다.", file=sys.stderr)
        else:
            conn.commit()
        return code
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
