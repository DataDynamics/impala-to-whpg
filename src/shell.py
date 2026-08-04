"""대화형 SQL 셸. beeline / sqlplus 처럼 붙어서 쿼리를 주고받는다.

엔진에 중립이다. 접속과 카탈로그 조회처럼 엔진마다 다른 부분만 :class:`Engine`
으로 받고, 나머지(입력 누적, 문장 분리, 메타 명령, 히스토리, 출력)는 여기서
처리한다. 실제 실행은 DB-API 커서로 하므로 impyla 든 psycopg2 든 같은 코드를 탄다.

터미널이 아니면(파이프, 크론) 프롬프트와 히스토리를 끄고 입력을 순서대로 실행한다.

    echo "SELECT 1;" | bin/gp-shell
    bin/gp-shell < script.sql
"""

import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import progress
import sqlfile
import table
from progress import PhaseTimer

#: 히스토리를 두는 곳. 저장소 안에 두면 커밋 사고가 나고, 쿼리에는 값이 그대로 들어 있다.
HISTORY_DIR = os.path.join(os.path.expanduser("~"), ".impala-to-whpg")

#: 한 번에 받아오는 행 수
FETCH_SIZE = 10_000


class Engine:
    """엔진마다 다른 부분만 담는다. 각 도구 모듈이 만들어 넘긴다."""

    def __init__(
        self,
        name: str,
        label: str,
        connect: Callable[[], Any],
        transactional: bool = False,
        enable_autocommit: Optional[Callable[[Any], None]] = None,
    ) -> None:
        self.name = name                      # "Impala" / "Greenplum"
        self.label = label                    # 프롬프트에 쓸 대상 이름
        self._connect = connect
        self.transactional = transactional
        self._enable_autocommit = enable_autocommit

    def connect(self) -> Any:
        conn = self._connect()
        # 셸은 세션이 길다. 한 트랜잭션으로 묶어두면 잠금이 계속 유지되고,
        # 사용자는 문장마다 반영되기를 기대한다. 기본을 autocommit 으로 둔다.
        if self._enable_autocommit is not None:
            self._enable_autocommit(conn)
        return conn


# -- 문장 분리 ---------------------------------------------------------------------


def split_statements(text: str) -> Tuple[List[str], str]:
    """``;`` 로 문장을 끊는다. (완성된 문장들, 남은 조각) 을 돌려준다.

    따옴표와 주석 안의 ``;`` 는 세지 않는다. 그러지 않으면
    ``SELECT ';'`` 한 줄이 두 문장으로 잘린다.

    Greenplum 의 ``$$ ... $$`` 인용은 아직 다루지 않는다. 함수 정의처럼 그 안에
    ``;`` 가 들어가는 문장은 ``\\i`` 로 파일째 실행하는 편이 안전하다.
    """
    statements: List[str] = []
    start = 0
    i = 0
    quote: Optional[str] = None       # 현재 열려 있는 따옴표
    comment: Optional[str] = None     # "line" 또는 "block"

    while i < len(text):
        ch = text[i]
        pair = text[i : i + 2]

        if comment == "line":
            if ch == "\n":
                comment = None
        elif comment == "block":
            if pair == "*/":
                comment = None
                i += 1
        elif quote:
            if ch == quote:
                # 따옴표를 두 번 겹치면 이스케이프다 ('' 또는 "")
                if text[i + 1 : i + 2] == quote:
                    i += 1
                else:
                    quote = None
        elif pair == "--":
            comment = "line"
            i += 1
        elif pair == "/*":
            comment = "block"
            i += 1
        elif ch in ("'", '"'):
            quote = ch
        elif ch == ";":
            chunk = text[start : i].strip()
            if chunk:
                statements.append(chunk)
            start = i + 1
        i += 1

    return statements, text[start:]


def is_complete(buffer: str) -> bool:
    """버퍼가 문장 하나로 끝났는지. 프롬프트를 바꿀지 판단하는 데 쓴다."""
    statements, rest = split_statements(buffer)
    return bool(statements) and not rest.strip()


# -- 셸 ---------------------------------------------------------------------------


class Shell:
    def __init__(self, engine: Engine, args: Any) -> None:
        self.engine = engine
        self.args = args
        self.conn: Any = None
        self.variables: Dict[str, str] = sqlfile.parse_variables(getattr(args, "var", []))
        self.output: Optional[str] = None          # \o 로 지정한 CSV 경로
        self.timing = False
        self.max_rows = getattr(args, "max_rows", table.DEFAULT_MAX_ROWS)
        self.interactive = sys.stdin.isatty() and progress.is_interactive()
        self.buffer = ""

    # -- 프롬프트 --
    def prompt(self) -> str:
        base = self.engine.label
        return f"{base}-> " if self.buffer.strip() else f"{base}=> "

    def history_path(self) -> str:
        return os.path.join(HISTORY_DIR, f"history-{self.engine.name.lower()}")

    def load_history(self) -> Any:
        """readline 이 있으면 히스토리를 붙인다. 없어도 셸은 돌아간다."""
        try:
            import readline
        except ImportError:
            return None
        os.makedirs(HISTORY_DIR, mode=0o700, exist_ok=True)
        path = self.history_path()
        try:
            readline.read_history_file(path)
        except (OSError, ValueError):
            pass
        readline.set_history_length(1000)
        return readline

    def save_history(self, readline: Any) -> None:
        if readline is None:
            return
        path = self.history_path()
        try:
            readline.write_history_file(path)
            # 쿼리에는 값이 그대로 들어 있다. 남이 읽지 못하게 한다.
            os.chmod(path, 0o600)
        except OSError:
            pass

    # -- 실행 --
    def execute(self, sql: str) -> None:
        rendered = sqlfile.render_query(sql, self.variables, "입력", warn_unused=False)
        timer = PhaseTimer(("실행 요청", "결과 수신", "결과 출력"))
        cursor = self.conn.cursor()
        try:
            with timer.measure("실행 요청"):
                cursor.execute(rendered)

            if cursor.description is None:
                affected = getattr(cursor, "rowcount", -1)
                detail = (
                    f"{affected:,}행" if affected is not None and affected >= 0 else "완료"
                )
                print(detail, file=sys.stderr)
            else:
                columns = [d[0].split(".")[-1] for d in cursor.description]
                with timer.measure("결과 수신"):
                    if self.output:
                        rows, truncated = self._fetch_all(cursor)
                    else:
                        rows, truncated = table.fetch_limited(
                            cursor, self.max_rows, FETCH_SIZE
                        )
                with timer.measure("결과 출력"):
                    self._emit(columns, rows, truncated)
        finally:
            cursor.close()

        if self.timing:
            timer.print_report()

    def _fetch_all(self, cursor: Any) -> Tuple[List[Sequence[Any]], bool]:
        rows: List[Sequence[Any]] = []
        while True:
            batch = cursor.fetchmany(FETCH_SIZE)
            if not batch:
                break
            rows.extend(batch)
        return rows, False

    def _emit(self, columns: Sequence[str], rows: Sequence[Sequence[Any]], truncated: bool) -> None:
        if not self.output:
            table.print_result(columns, rows, truncated)
            return
        with table.open_output(self.output, False, "utf-8") as handle:
            written = table.write_csv(handle, columns, rows)
        print(f"{self.output}  {written:,}행", file=sys.stderr)

    # -- 메타 명령 --
    def meta(self, line: str) -> bool:
        """``\\`` 로 시작하는 명령을 처리한다. 계속 돌면 True."""
        parts = line.strip().split(None, 2)
        name, rest = parts[0], parts[1:]

        if name in ("\\q", "\\quit"):
            return False
        if name in ("\\?", "\\h", "\\help"):
            print(HELP)
        elif name == "\\i":
            if not rest:
                print("사용법: \\i 파일.sql", file=sys.stderr)
            else:
                self.run_file(rest[0])
        elif name == "\\set":
            if not rest:
                for key, value in sorted(self.variables.items()):
                    print(f"{key} = {value}")
            elif len(rest) == 1:
                print("사용법: \\set 이름 값", file=sys.stderr)
            else:
                self.variables[rest[0]] = rest[1]
        elif name == "\\unset":
            if rest:
                self.variables.pop(rest[0], None)
        elif name == "\\o":
            self.output = rest[0] if rest else None
            where = self.output or "화면"
            print(f"결과를 {where} 으로 보냅니다.", file=sys.stderr)
        elif name == "\\timing":
            self.timing = not self.timing
            print(f"소요 시간 표시: {'켬' if self.timing else '끔'}", file=sys.stderr)
        elif name in ("\\begin", "\\commit", "\\rollback"):
            self.transaction(name)
        else:
            print(f"모르는 명령입니다: {name}  (\\? 로 목록을 봅니다)", file=sys.stderr)
        return True

    def transaction(self, name: str) -> None:
        if not self.engine.transactional:
            print(f"{self.engine.name} 에는 트랜잭션이 없습니다.", file=sys.stderr)
            return
        cursor = self.conn.cursor()
        try:
            if name == "\\begin":
                cursor.execute("BEGIN")
                print("트랜잭션을 시작했습니다. \\commit 또는 \\rollback 으로 끝냅니다.",
                      file=sys.stderr)
            else:
                cursor.execute("COMMIT" if name == "\\commit" else "ROLLBACK")
                print("반영했습니다." if name == "\\commit" else "되돌렸습니다.",
                      file=sys.stderr)
        finally:
            cursor.close()

    def run_file(self, given: str) -> None:
        """``sql/`` 의 템플릿을 실행한다. ``\\set`` 으로 둔 변수가 그대로 채워진다."""
        path = sqlfile.resolve_query_file(given, getattr(self.args, "sql_dir", None))
        with open(path, "r", encoding="utf-8-sig") as fp:
            text = fp.read()
        rendered = sqlfile.render_query(text, self.variables, path, warn_unused=False)
        statements, rest = split_statements(rendered)
        if rest.strip():
            statements.append(rest)
        for statement in statements:
            print(f"-- {path}", file=sys.stderr)
            self.execute(statement)

    # -- 루프 --
    def feed(self, line: str) -> bool:
        """한 줄을 받아 처리한다. 계속 돌면 True."""
        if not self.buffer.strip() and line.strip().startswith("\\"):
            return self.meta(line)

        self.buffer += line + "\n"
        statements, rest = split_statements(self.buffer)
        self.buffer = rest
        for statement in statements:
            try:
                self.execute(statement)
            except SystemExit:
                raise
            except Exception as exc:
                # 셸은 문장 하나가 실패했다고 끝나지 않는다
                print(f"오류: {type(exc).__name__}: {exc}", file=sys.stderr)
                self._recover()
        return True

    def _recover(self) -> None:
        """실패한 트랜잭션을 정리한다. 안 하면 이후 문장이 전부 거부된다."""
        if not self.engine.transactional:
            return
        try:
            self.conn.rollback()
        except Exception:
            pass

    def run(self) -> int:
        self.conn = self.engine.connect()
        readline = self.load_history() if self.interactive else None
        if self.interactive:
            print(
                f"{self.engine.name} 에 붙었습니다. \\? 로 도움말, \\q 로 종료합니다.",
                file=sys.stderr,
            )
        try:
            while True:
                try:
                    line = input(self.prompt()) if self.interactive else input()
                except EOFError:
                    break
                except KeyboardInterrupt:
                    # 입력 중 Ctrl-C 는 쓰던 문장만 버린다. 셸은 살아 있는다.
                    self.buffer = ""
                    print("^C", file=sys.stderr)
                    continue
                if not self.feed(line):
                    break

            if self.buffer.strip():
                print(
                    "세미콜론 없이 끝난 입력이 있어 실행하지 않았습니다:\n"
                    f"  {self.buffer.strip()[:60]}",
                    file=sys.stderr,
                )
        finally:
            self.save_history(readline)
            try:
                self.conn.close()
            except Exception:
                pass
        return 0


HELP = """\
메타 명령

  \\q                 종료 (Ctrl-D 도 같음)
  \\?                 이 도움말
  \\i 파일.sql        sql/ 의 템플릿 실행 (\\set 변수가 채워집니다)
  \\set               지금 설정된 변수 보기
  \\set 이름 값       템플릿 변수 지정
  \\unset 이름        변수 지우기
  \\o 파일.csv        결과를 CSV 파일로 보내기
  \\o                 결과를 화면으로 되돌리기
  \\timing            구간별 소요 시간 표시 켜기/끄기
  \\begin             트랜잭션 시작 (Greenplum)
  \\commit            반영
  \\rollback          되돌리기

문장은 세미콜론(;) 으로 끝냅니다. 여러 줄로 이어 써도 됩니다.
기본은 문장마다 바로 반영(autocommit)이며, 묶으려면 \\begin 을 씁니다."""
