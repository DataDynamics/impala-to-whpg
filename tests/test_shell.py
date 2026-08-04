"""src/shell.py 검증 — 대화형 SQL 셸 (실제 DB 없이 가짜 커서로 실행)."""

import argparse
import os
import sys
import threading
from datetime import date
from pathlib import Path
from typing import Any, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import shell as sh  # noqa: E402
import table  # noqa: E402


class FakeCursor:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.description = None
        self._rows: List[Any] = []
        self._offset = 0
        self.rowcount = -1

    def execute(self, sql: str) -> None:
        self.conn.executed.append(sql)
        if self.conn.block is not None:
            # 오래 걸리는 문장 흉내. cancel() 이 풀어준다.
            self.conn.block.wait(2)
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise RuntimeError("문법 오류")
        columns, rows, rowcount = self.conn.result_for(sql)
        self.description = [(c,) for c in columns] if columns else None
        self._rows = list(rows)
        self._offset = 0
        self.rowcount = rowcount

    def fetchall(self):
        rows = self._rows[self._offset :]
        self._offset = len(self._rows)
        return rows

    def fetchmany(self, size: int):
        batch = self._rows[self._offset : self._offset + size]
        self._offset += len(batch)
        return batch

    def close(self) -> None:
        pass


class FakeConnection:
    def __init__(self) -> None:
        self.executed: List[str] = []
        self.autocommit = False
        self.closed = False
        self.rolled_back = 0
        self.cancelled = 0
        self.block = None
        self.fail_on: str = ""
        self.result = ([], [], -1)      # (columns, rows, rowcount)

    def result_for(self, sql: str):
        if sql.strip().upper().startswith(("BEGIN", "COMMIT", "ROLLBACK", "SET")):
            return ([], [], -1)
        return self.result

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def cancel(self) -> None:
        self.cancelled += 1
        if self.block is not None:
            self.block.set()

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed = True


def feed_lines(monkeypatch, lines):
    """input() 흉내. 다 떨어지면 실제 input 처럼 EOFError 를 낸다."""
    it = iter(lines)

    def fake_input(*args):
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr("builtins.input", fake_input)


def make_shell(transactional=True, **overrides):
    conn = FakeConnection()
    engine = sh.Engine(
        name="Greenplum",
        label="dw",
        connect=lambda: conn,
        transactional=transactional,
        enable_autocommit=(lambda c: setattr(c, "autocommit", True)) if transactional else None,
        cancel=lambda conn, cursor: conn.cancel(),
        list_tables_sql=lambda pattern: (
            f"SHOW TABLES LIKE '{pattern}'" if pattern else "SHOW TABLES"
        ),
        describe_sql=lambda target: f"DESCRIBE {target}",
        table_names_sql=lambda: "SHOW TABLES",
        ddl_sql=lambda target: f"SHOW CREATE TABLE {target}",
        describe_extra_sql=lambda target: f"DISTKEY {target}",
    )
    options = dict(var=[], max_rows=100, sql_dir=None)
    options.update(overrides)
    s = sh.Shell(engine, argparse.Namespace(**options))
    s.conn = engine.connect()
    s.interactive = False
    return s, conn


# -- 문장 분리 ---------------------------------------------------------------------


def test_splits_on_semicolon():
    assert sh.split_statements("SELECT 1; SELECT 2;") == (["SELECT 1", "SELECT 2"], "")


def test_keeps_the_incomplete_tail():
    statements, rest = sh.split_statements("SELECT 1; SELECT 2")
    assert statements == ["SELECT 1"]
    assert rest.strip() == "SELECT 2"


def test_semicolon_inside_single_quotes_is_not_a_terminator():
    """SELECT ';' 한 줄이 두 문장으로 잘리면 안 된다."""
    statements, _ = sh.split_statements("SELECT ';' FROM t;")
    assert statements == ["SELECT ';' FROM t"]


def test_semicolon_inside_double_quotes_is_not_a_terminator():
    statements, _ = sh.split_statements('SELECT "a;b" FROM t;')
    assert statements == ['SELECT "a;b" FROM t']


def test_doubled_quote_is_an_escape():
    statements, _ = sh.split_statements("SELECT 'it''s; fine' FROM t;")
    assert statements == ["SELECT 'it''s; fine' FROM t"]


def test_semicolon_in_a_line_comment_is_ignored():
    statements, _ = sh.split_statements("SELECT 1  -- 주석; 안에\nFROM t;")
    assert statements == ["SELECT 1  -- 주석; 안에\nFROM t"]


def test_semicolon_in_a_block_comment_is_ignored():
    statements, _ = sh.split_statements("SELECT 1 /* 주석; 안 */ FROM t;")
    assert len(statements) == 1


def test_multiline_statement():
    text = "SELECT\n  a,\n  b\nFROM t\nWHERE x = 1;"
    statements, rest = sh.split_statements(text)
    assert len(statements) == 1 and rest.strip() == ""


def test_empty_statements_are_dropped():
    assert sh.split_statements(";;  ;")[0] == []


def test_is_complete():
    assert sh.is_complete("SELECT 1;")
    assert not sh.is_complete("SELECT 1")
    assert not sh.is_complete("SELECT 1; SELECT 2")


# -- 프롬프트 ----------------------------------------------------------------------


def test_prompt_changes_while_a_statement_is_open():
    s, _ = make_shell()
    assert s.prompt() == "dw=> "
    s.buffer = "SELECT 1\n"
    assert s.prompt() == "dw-> "


# -- 실행 --------------------------------------------------------------------------


def test_select_prints_a_table(capsys):
    s, conn = make_shell()
    conn.result = (["a", "b"], [(1, "x"), (2, "y")], -1)
    s.feed("SELECT * FROM t;")
    out = capsys.readouterr()
    assert "a" in out.out and "x" in out.out
    assert "2행" in out.err


def test_statement_runs_only_after_the_semicolon(capsys):
    s, conn = make_shell()
    s.feed("SELECT 1")
    assert conn.executed == []
    s.feed("FROM t;")
    assert conn.executed == ["SELECT 1\nFROM t"]


def test_dml_reports_rowcount(capsys):
    s, conn = make_shell()
    conn.result = ([], [], 7)
    s.feed("DELETE FROM t;")
    assert "7행" in capsys.readouterr().err


def test_error_does_not_end_the_shell(capsys):
    s, conn = make_shell()
    conn.fail_on = "BROKEN"
    assert s.feed("BROKEN;") is True
    assert "오류: RuntimeError" in capsys.readouterr().err


def test_failed_statement_rolls_back(capsys):
    """실패한 트랜잭션을 정리하지 않으면 이후 문장이 전부 거부된다."""
    s, conn = make_shell()
    conn.fail_on = "BROKEN"
    s.feed("BROKEN;")
    assert conn.rolled_back == 1


def test_autocommit_is_on_by_default():
    """셸은 세션이 길다. 한 트랜잭션으로 묶어두면 잠금이 계속 유지된다."""
    _, conn = make_shell()
    assert conn.autocommit is True


def test_template_variables_are_applied(capsys):
    s, conn = make_shell(var=["dt=2026-08-01"])
    s.feed("SELECT '{{ dt }}';")
    assert conn.executed == ["SELECT '2026-08-01'"]


# -- 메타 명령 ---------------------------------------------------------------------


def test_quit_stops_the_loop():
    s, _ = make_shell()
    assert s.feed("\\q") is False


def test_help_lists_commands(capsys):
    s, _ = make_shell()
    s.feed("\\?")
    out = capsys.readouterr().out
    assert "\\i" in out and "\\set" in out and "\\timing" in out


def test_set_and_unset(capsys):
    s, conn = make_shell()
    s.feed("\\set dt 2026-08-01")
    assert s.variables == {"dt": "2026-08-01"}
    s.feed("\\set")
    assert "dt = 2026-08-01" in capsys.readouterr().out
    s.feed("\\unset dt")
    assert s.variables == {}


def test_set_variable_feeds_the_template(capsys):
    s, conn = make_shell()
    s.feed("\\set dt 2026-08-01")
    s.feed("SELECT '{{ dt }}';")
    assert conn.executed == ["SELECT '2026-08-01'"]


def test_timing_toggles(capsys):
    s, conn = make_shell()
    assert s.timing is False
    s.feed("\\timing")
    assert s.timing is True
    conn.result = (["a"], [(1,)], -1)
    s.feed("SELECT 1;")
    assert "구간별 소요 시간" in capsys.readouterr().err


def test_output_redirects_to_csv(tmp_path, capsys):
    s, conn = make_shell()
    conn.result = (["a", "b"], [(1, "x")], -1)
    target = tmp_path / "out.csv"
    s.feed(f"\\o {target}")
    s.feed("SELECT * FROM t;")

    assert target.read_text(encoding="utf-8").splitlines() == ["a`b", "1`x"]
    assert "SELECT" not in capsys.readouterr().out     # 화면에는 안 나온다


def test_output_back_to_screen(tmp_path, capsys):
    s, conn = make_shell()
    conn.result = (["a"], [(1,)], -1)
    s.feed(f"\\o {tmp_path / 'out.csv'}")
    s.feed("\\o")
    assert s.output is None
    s.feed("SELECT 1;")
    assert "a" in capsys.readouterr().out


def test_run_file_uses_variables(tmp_path, capsys):
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "q.sql").write_text("SELECT '{{ dt }}';", encoding="utf-8")

    s, conn = make_shell(sql_dir=str(sql_dir))
    s.feed("\\set dt 2026-08-01")
    s.feed(f"\\i q.sql")
    assert conn.executed == ["SELECT '2026-08-01'"]


def test_run_file_without_a_trailing_semicolon(tmp_path):
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "q.sql").write_text("SELECT 1", encoding="utf-8")

    s, conn = make_shell(sql_dir=str(sql_dir))
    s.feed("\\i q.sql")
    assert conn.executed == ["SELECT 1"]


def test_unknown_meta_command_is_reported(capsys):
    s, _ = make_shell()
    assert s.feed("\\없는명령") is True
    assert "모르는 명령" in capsys.readouterr().err


def test_backslash_inside_a_statement_is_not_a_meta_command():
    """문장을 쓰는 중에는 \\ 로 시작해도 메타 명령이 아니다."""
    s, conn = make_shell()
    s.feed("SELECT")
    s.feed("  '\\q' FROM t;")
    assert conn.executed == ["SELECT\n  '\\q' FROM t"]


def test_no_warning_for_plain_statements_after_set(capsys):
    """\\set 해둔 변수가 있어도 평범한 문장을 계속 치는 것이 정상이다."""
    s, conn = make_shell()
    s.feed("\\set dt 2026-08-01")
    capsys.readouterr()
    s.feed("SELECT 1;")
    assert "쓰이지 않았습니다" not in capsys.readouterr().err


# -- 트랜잭션 ----------------------------------------------------------------------


def test_begin_commit(capsys):
    s, conn = make_shell()
    s.feed("\\begin")
    s.feed("\\commit")
    assert conn.executed == ["BEGIN", "COMMIT"]


def test_rollback(capsys):
    s, conn = make_shell()
    s.feed("\\rollback")
    assert conn.executed == ["ROLLBACK"]


def test_transaction_commands_are_rejected_without_support(capsys):
    s, conn = make_shell(transactional=False)
    s.feed("\\begin")
    assert conn.executed == []
    assert "트랜잭션이 없습니다" in capsys.readouterr().err


# -- 비대화형 ----------------------------------------------------------------------


def test_non_interactive_runs_piped_statements(monkeypatch, capsys):
    """echo "SELECT 1;" | bin/gp-shell 이 동작해야 한다."""
    s, conn = make_shell()
    feed_lines(monkeypatch, ["SELECT 1;", "SELECT 2;"])
    assert s.run() == 0
    assert conn.executed == ["SELECT 1", "SELECT 2"]
    assert conn.closed is True


def test_unterminated_input_is_reported_not_executed(monkeypatch, capsys):
    s, conn = make_shell()
    feed_lines(monkeypatch, ["SELECT 1"])
    s.run()
    assert conn.executed == []
    assert "세미콜론 없이 끝난 입력" in capsys.readouterr().err


def test_history_is_not_touched_when_not_interactive(tmp_path, monkeypatch):
    monkeypatch.setattr(sh, "HISTORY_DIR", str(tmp_path / "hist"))
    s, _ = make_shell()
    feed_lines(monkeypatch, [])
    s.run()
    assert not (tmp_path / "hist").exists()


# -- 큰 결과 ----------------------------------------------------------------------


def test_screen_output_stops_early(capsys):
    """표로 볼 때는 보여줄 만큼만 받는다. 큰 테이블에 터미널이 잠기지 않는다."""
    s, conn = make_shell(max_rows=5)
    conn.result = (["a"], [(i,) for i in range(100_000)], -1)
    s.feed("SELECT * FROM big;")
    assert "5행 이상" in capsys.readouterr().err


def test_csv_output_takes_everything(tmp_path):
    s, conn = make_shell(max_rows=5)
    conn.result = (["a"], [(i,) for i in range(1000)], -1)
    target = tmp_path / "out.csv"
    s.feed(f"\\o {target}")
    s.feed("SELECT * FROM big;")
    assert len(target.read_text(encoding="utf-8").splitlines()) == 1001


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# -- 달러 인용 ---------------------------------------------------------------------


FUNCTION = """CREATE FUNCTION f() RETURNS int AS $$
BEGIN
  SELECT 1; SELECT 2;
  RETURN 3;
END;
$$ LANGUAGE plpgsql;"""


def test_dollar_quoted_body_stays_one_statement():
    """함수 본문의 세미콜론을 문장 끝으로 세면 CREATE FUNCTION 이 잘린다."""
    statements, rest = sh.split_statements(FUNCTION)
    assert len(statements) == 1
    assert "RETURN 3" in statements[0]
    assert rest.strip() == ""


def test_dollar_quoted_body_then_another_statement():
    statements, _ = sh.split_statements(FUNCTION + "\nSELECT 9;")
    assert len(statements) == 2
    assert statements[1] == "SELECT 9"


def test_tagged_dollar_quote():
    statements, _ = sh.split_statements("SELECT $tag$ a; b $tag$ FROM t;")
    assert len(statements) == 1


def test_unclosed_dollar_quote_stays_incomplete():
    statements, rest = sh.split_statements("CREATE FUNCTION f() AS $$ SELECT 1;")
    assert statements == []
    assert rest.strip().startswith("CREATE FUNCTION")


def test_dollar_sign_that_is_not_a_quote():
    """$1 같은 자리표시자는 인용이 아니다."""
    statements, _ = sh.split_statements("SELECT $1 FROM t;")
    assert statements == ["SELECT $1 FROM t"]


# -- 세로 출력 ---------------------------------------------------------------------


def test_vertical_toggle(capsys):
    s, conn = make_shell()
    conn.result = (["a", "b"], [(1, "x")], -1)
    s.feed("\\x")
    assert s.vertical is True
    s.feed("SELECT 1;")
    out = capsys.readouterr().out
    assert "RECORD 1" in out
    assert "a | 1" in out


def test_vertical_off_returns_to_table(capsys):
    s, conn = make_shell()
    conn.result = (["a"], [(1,)], -1)
    s.feed("\\x")
    s.feed("\\x")
    assert s.vertical is False
    s.feed("SELECT 1;")
    assert "RECORD" not in capsys.readouterr().out


# -- 카탈로그 ---------------------------------------------------------------------


def test_dt_lists_tables(capsys):
    s, conn = make_shell()
    conn.result = (["table"], [("orders",)], -1)
    s.feed("\\dt")
    assert conn.executed == ["SHOW TABLES"]
    assert "orders" in capsys.readouterr().out


def test_dt_with_a_pattern(capsys):
    s, conn = make_shell()
    s.feed("\\dt order%")
    assert conn.executed == ["SHOW TABLES LIKE 'order%'"]


def test_d_describes_a_table(capsys):
    s, conn = make_shell()
    conn.result = (["name", "type"], [("order_id", "bigint")], -1)
    s.feed("\\d orders")
    assert conn.executed[0] == "DESCRIBE orders"
    assert "order_id" in capsys.readouterr().out


def test_d_without_a_name_lists_tables(capsys):
    s, conn = make_shell()
    s.feed("\\d")
    assert conn.executed == ["SHOW TABLES"]


def test_catalog_error_does_not_end_the_shell(capsys):
    s, conn = make_shell()
    conn.fail_on = "SHOW TABLES"
    assert s.feed("\\dt") is True
    assert "오류" in capsys.readouterr().err


def test_catalog_unsupported_is_reported(capsys):
    conn = FakeConnection()
    engine = sh.Engine("Something", "x", lambda: conn)
    s = sh.Shell(engine, argparse.Namespace(var=[], max_rows=100, sql_dir=None))
    s.conn = conn
    s.interactive = False
    s.feed("\\dt")
    assert "지원하지 않습니다" in capsys.readouterr().err


# -- 취소 -------------------------------------------------------------------------


def interrupt_on_wait(monkeypatch):
    """메인이 스레드를 기다리는 첫 순간에 Ctrl-C 가 들어온 상황을 만든다."""
    real_join = threading.Thread.join
    fired = {"done": False}

    def fake_join(self, timeout=None):
        if timeout is not None and not fired["done"]:
            fired["done"] = True
            raise KeyboardInterrupt
        return real_join(self) if timeout is None else real_join(self, timeout)

    monkeypatch.setattr(threading.Thread, "join", fake_join)


def test_cancel_asks_the_server_and_keeps_the_shell(capsys, monkeypatch):
    """Ctrl-C 는 실행 중인 문장만 취소하고 셸은 살아 있어야 한다."""
    s, conn = make_shell()
    conn.block = threading.Event()      # 문장이 매달려 있다
    interrupt_on_wait(monkeypatch)

    assert s.feed("SELECT pg_sleep(60);") is True

    assert conn.cancelled == 1          # 서버에 취소를 요청했다
    assert conn.rolled_back == 1        # 실패한 트랜잭션을 정리했다
    err = capsys.readouterr().err
    assert "취소하는 중" in err and "취소했습니다" in err


def test_cancelled_statement_prints_no_result(capsys, monkeypatch):
    s, conn = make_shell()
    conn.block = threading.Event()
    conn.result = (["a"], [(1,)], -1)
    interrupt_on_wait(monkeypatch)

    s.feed("SELECT 1;")
    assert capsys.readouterr().out == ""


def test_engine_without_cancel_says_so(capsys, monkeypatch):
    conn = FakeConnection()
    conn.block = threading.Event()
    engine = sh.Engine("Something", "x", lambda: conn)
    s = sh.Shell(engine, argparse.Namespace(var=[], max_rows=100, sql_dir=None))
    s.conn = conn
    s.interactive = False
    interrupt_on_wait(monkeypatch)
    # 취소할 방법이 없으니 문장이 스스로 끝날 때까지 기다린다
    threading.Timer(0.05, conn.block.set).start()

    s.feed("SELECT 1;")
    assert "취소를 지원하지 않습니다" in capsys.readouterr().err


def test_statement_runs_in_a_thread():
    """드라이버가 GIL 을 놓기 때문에 별도 스레드가 아니면 Ctrl-C 가 먹지 않는다."""
    s, conn = make_shell()
    seen = {}

    class Recording(FakeCursor):
        def execute(self, sql):
            seen["thread"] = sh.threading.current_thread().name
            super().execute(sql)

    conn.cursor = lambda: Recording(conn)
    s.feed("SELECT 1;")
    assert seen["thread"] != sh.threading.main_thread().name


# -- \e ---------------------------------------------------------------------------


def test_edit_runs_what_was_saved(tmp_path, monkeypatch, capsys):
    s, conn = make_shell()

    def fake_editor(argv):
        with open(argv[1], "w", encoding="utf-8") as fp:
            fp.write("SELECT 42;")
        return 0

    monkeypatch.setenv("EDITOR", "fake")
    monkeypatch.setattr("subprocess.call", fake_editor)
    s.feed("\\e")
    assert conn.executed == ["SELECT 42"]


def test_edit_without_an_editor_is_reported(monkeypatch, capsys):
    s, _ = make_shell()
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    s.feed("\\e")
    assert "EDITOR" in capsys.readouterr().err


def test_edit_accepts_input_without_a_semicolon(monkeypatch):
    s, conn = make_shell()

    def fake_editor(argv):
        with open(argv[1], "w", encoding="utf-8") as fp:
            fp.write("SELECT 7")
        return 0

    monkeypatch.setenv("EDITOR", "fake")
    monkeypatch.setattr("subprocess.call", fake_editor)
    s.feed("\\e")
    assert conn.executed == ["SELECT 7"]


# -- \\paste ----------------------------------------------------------------------


def test_paste_runs_a_block_without_a_trailing_semicolon(monkeypatch):
    """붙여넣는 글에는 세미콜론이 없을 때가 많다."""
    s, conn = make_shell()
    feed_lines(monkeypatch, ["SELECT", "  order_id", "FROM orders"])
    s.feed("\\paste")
    assert conn.executed == ["SELECT\n  order_id\nFROM orders"]


def test_paste_accepts_the_spark_shell_spelling(monkeypatch):
    s, conn = make_shell()
    feed_lines(monkeypatch, ["SELECT 1"])
    s.feed(":paste")
    assert conn.executed == ["SELECT 1"]


def test_paste_splits_multiple_statements(monkeypatch):
    s, conn = make_shell()
    feed_lines(monkeypatch, ["SELECT 1;", "SELECT 2;", "SELECT 3"])
    s.feed("\\paste")
    assert conn.executed == ["SELECT 1", "SELECT 2", "SELECT 3"]


def test_paste_does_not_treat_backslash_lines_as_meta(monkeypatch):
    """붙여넣은 글에 \\ 로 시작하는 줄이 있어도 명령으로 잡히면 안 된다."""
    s, conn = make_shell()
    feed_lines(monkeypatch, [r"SELECT '\q' AS x", r"FROM t"])
    s.feed("\\paste")
    assert conn.executed == [r"SELECT '\q' AS x" + "\nFROM t"]


def test_paste_ends_on_the_dot_terminator(monkeypatch):
    s, conn = make_shell()
    feed_lines(monkeypatch, ["SELECT 1", "\\.", "SELECT 2"])
    s.feed("\\paste")
    assert conn.executed == ["SELECT 1"]


def test_paste_keeps_dollar_quoted_bodies_together(monkeypatch):
    s, conn = make_shell()
    feed_lines(monkeypatch, FUNCTION.splitlines())
    s.feed("\\paste")
    assert len(conn.executed) == 1
    assert "SELECT 1; SELECT 2;" in conn.executed[0]


def test_paste_applies_template_variables(monkeypatch):
    s, conn = make_shell()
    s.feed("\\set dt 2026-08-01")
    feed_lines(monkeypatch, ["SELECT '{{ dt }}'"])
    s.feed("\\paste")
    assert conn.executed == ["SELECT '2026-08-01'"]


def test_paste_of_nothing_is_reported(monkeypatch, capsys):
    s, conn = make_shell()
    feed_lines(monkeypatch, [])
    s.feed("\\paste")
    assert conn.executed == []
    assert "내용이 없습니다" in capsys.readouterr().err


def test_paste_inside_a_statement_is_literal_text():
    """문장을 쓰는 중이면 \\q 와 마찬가지로 명령이 아니라 SQL 이다."""
    s, conn = make_shell()
    s.feed("SELECT")
    s.feed("  '\\paste' FROM t;")
    assert conn.executed == ["SELECT\n  '\\paste' FROM t"]


def test_paste_survives_a_failing_statement(monkeypatch, capsys):
    s, conn = make_shell()
    conn.fail_on = "BROKEN"
    feed_lines(monkeypatch, ["BROKEN;", "SELECT 2;"])
    assert s.feed("\\paste") is True
    assert conn.executed == ["BROKEN", "SELECT 2"]
    assert "오류" in capsys.readouterr().err


def test_paste_cancelled_by_ctrl_c(monkeypatch, capsys):
    s, conn = make_shell()

    def fake_input(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", fake_input)
    s.feed("\\paste")
    assert conn.executed == []
    assert "붙여넣기를 취소했습니다" in capsys.readouterr().err


def test_paste_is_listed_in_help(capsys):
    s, _ = make_shell()
    s.feed("\\?")
    assert "\\paste" in capsys.readouterr().out


# -- 자동완성 ---------------------------------------------------------------------


def test_completes_meta_commands():
    s, _ = make_shell()
    assert s.candidates("\\d") == ["\\d", "\\ddl", "\\dt"]
    assert "\\paste" in s.candidates("\\p")


def test_completes_the_spark_shell_spelling():
    s, _ = make_shell()
    assert s.candidates(":") == [":paste"]


def test_completes_table_names():
    s, conn = make_shell()
    conn.result = (["name"], [("orders",), ("order_items",), ("customers",)], -1)
    assert s.candidates("order")[:2] == ["order_items", "orders"]
    assert "customers" not in s.candidates("order")


def test_completes_sql_keywords():
    s, conn = make_shell()
    conn.result = (["name"], [], -1)
    assert "SELECT" in s.candidates("sel")


def test_table_names_come_before_keywords():
    """이름은 기억하기 어렵고 키워드는 외우고 있다."""
    s, conn = make_shell()
    conn.result = (["name"], [("selected_orders",)], -1)
    assert s.candidates("sel")[0] == "selected_orders"


def test_table_names_are_cached():
    """카탈로그 조회가 느릴 수 있어 Tab 마다 부르지 않는다."""
    s, conn = make_shell()
    conn.result = (["name"], [("orders",)], -1)
    s.candidates("o")
    s.candidates("o")
    assert conn.executed.count("SHOW TABLES") == 1


def test_dt_refreshes_the_cache():
    s, conn = make_shell()
    conn.result = (["name"], [("orders",)], -1)
    s.candidates("o")
    s.feed("\\dt")
    s.candidates("o")
    assert conn.executed.count("SHOW TABLES") == 3     # 최초 + \dt + 다시 채우기


def test_completion_failure_is_silent(capsys):
    """자동완성이 실패했다고 셸이 시끄러워질 이유는 없다."""
    s, conn = make_shell()
    conn.fail_on = "SHOW TABLES"
    assert s.candidates("orders") == []      # 테이블 이름은 못 받았지만
    assert capsys.readouterr().err == ""     # 조용하다


def test_completion_without_engine_support():
    """카탈로그를 못 주는 엔진이어도 키워드 완성은 된다."""
    conn = FakeConnection()
    engine = sh.Engine("Something", "x", lambda: conn)
    s = sh.Shell(engine, argparse.Namespace(var=[], max_rows=100, sql_dir=None))
    s.conn = conn
    assert s.candidates("orders") == []
    assert "ORDER BY" in s.candidates("order")


def test_complete_callback_walks_the_matches():
    s, conn = make_shell()
    conn.result = (["name"], [("orders",), ("order_items",)], -1)
    assert s.complete("order", 0) == "order_items"
    assert s.complete("order", 1) == "orders"
    assert s.complete("order", 2) == "ORDER BY"     # 그다음은 키워드
    assert s.complete("order", 3) is None


def test_empty_prefix_completes_nothing():
    """빈 입력에 테이블 전체를 쏟아내면 방해가 된다."""
    s, _ = make_shell()
    assert s.candidates("") == []


# -- 페이저 -----------------------------------------------------------------------


def test_pager_is_not_used_when_not_interactive(capsys, monkeypatch):
    s, _ = make_shell()
    monkeypatch.setattr(sh.subprocess, "Popen", lambda *a, **k: pytest.fail("페이저 금지"))
    s.page("한 줄\n" * 200)
    assert "한 줄" in capsys.readouterr().out


def test_pager_off_prints_directly(capsys, monkeypatch):
    s, _ = make_shell()
    s.interactive = True
    s.feed("\\pager off")
    monkeypatch.setattr(sh.subprocess, "Popen", lambda *a, **k: pytest.fail("페이저 금지"))
    s.page("x\n" * 500)
    assert "x" in capsys.readouterr().out


def test_short_output_skips_the_pager_in_auto(capsys, monkeypatch):
    """화면에 들어가는 결과까지 페이저를 거치면 성가시다."""
    s, _ = make_shell()
    s.interactive = True
    monkeypatch.setattr(sh.shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((80, 40)))
    monkeypatch.setattr(sh.subprocess, "Popen", lambda *a, **k: pytest.fail("페이저 금지"))
    s.page("줄\n" * 5)
    assert "줄" in capsys.readouterr().out


def test_long_output_uses_the_pager(monkeypatch):
    s, _ = make_shell()
    s.interactive = True
    monkeypatch.setattr(sh.shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((80, 10)))
    seen = {}

    class FakeProc:
        def communicate(self, text):
            seen["text"] = text
        def poll(self):
            return 0

    monkeypatch.setattr(sh.subprocess, "Popen", lambda *a, **k: FakeProc())
    s.page("줄\n" * 100)
    assert seen["text"].count("줄") == 100


def test_pager_on_always_pages(monkeypatch):
    s, _ = make_shell()
    s.interactive = True
    s.feed("\\pager on")
    used = {}

    class FakeProc:
        def communicate(self, text):
            used["yes"] = True
        def poll(self):
            return 0

    monkeypatch.setattr(sh.subprocess, "Popen", lambda *a, **k: FakeProc())
    s.page("한 줄")
    assert used.get("yes")


def test_broken_pipe_is_not_an_error(monkeypatch):
    """페이저를 먼저 끝내면 BrokenPipe 가 난다. 오류가 아니다."""
    s, _ = make_shell()
    s.interactive = True
    s.pager = "on"

    class FakeProc:
        def communicate(self, text):
            raise BrokenPipeError
        def poll(self):
            return 0

    monkeypatch.setattr(sh.subprocess, "Popen", lambda *a, **k: FakeProc())
    s.page("x")          # 예외가 새어 나오면 안 된다


def test_missing_pager_falls_back_to_printing(capsys, monkeypatch):
    s, _ = make_shell()
    s.interactive = True
    s.pager = "on"

    def boom(*a, **k):
        raise OSError("없는 명령")

    monkeypatch.setattr(sh.subprocess, "Popen", boom)
    s.page("내용")
    out = capsys.readouterr()
    assert "내용" in out.out
    assert "페이저를 실행하지 못했습니다" in out.err


def test_pager_shows_current_setting(capsys):
    s, _ = make_shell()
    s.feed("\\pager")
    assert "auto" in capsys.readouterr().err


def test_pager_is_not_used_for_file_output(tmp_path, monkeypatch):
    s, conn = make_shell()
    s.interactive = True
    s.pager = "on"
    conn.result = (["a"], [(1,)], -1)
    monkeypatch.setattr(sh.subprocess, "Popen", lambda *a, **k: pytest.fail("페이저 금지"))
    s.feed(f"\\o {tmp_path / 'out.csv'}")
    s.feed("SELECT 1;")


# -- \\ddl / 스키마 ----------------------------------------------------------------


DDL = "CREATE TABLE staging.orders (\n    order_id bigint\n)\nDISTRIBUTED BY (order_id);"


def test_ddl_prints_the_statement_raw(capsys):
    """생성문은 그 자체가 여러 줄이라 표에 넣으면 읽기 어렵다."""
    s, conn = make_shell()
    conn.result = (["ddl"], [(DDL,)], -1)
    s.feed("\\ddl staging.orders")

    out = capsys.readouterr().out
    assert out.strip() == DDL
    assert "|" not in out.splitlines()[0]      # 표 테두리가 없다


def test_ddl_asks_the_engine_for_sql():
    s, conn = make_shell()
    conn.result = (["ddl"], [(DDL,)], -1)
    s.feed("\\ddl staging.orders")
    assert conn.executed == ["SHOW CREATE TABLE staging.orders"]


def test_ddl_without_a_name_is_reported(capsys):
    s, conn = make_shell()
    s.feed("\\ddl")
    assert conn.executed == []
    assert "사용법" in capsys.readouterr().err


def test_ddl_unsupported_engine_is_reported(capsys):
    conn = FakeConnection()
    engine = sh.Engine("Something", "x", lambda: conn)
    s = sh.Shell(engine, argparse.Namespace(var=[], max_rows=100, sql_dir=None))
    s.conn = conn
    s.interactive = False
    s.feed("\\ddl t")
    assert "지원하지 않습니다" in capsys.readouterr().err


def test_ddl_error_does_not_end_the_shell(capsys):
    s, conn = make_shell()
    conn.fail_on = "SHOW CREATE TABLE"
    assert s.feed("\\ddl t") is True
    assert "오류" in capsys.readouterr().err


def test_describe_adds_extra_info(capsys):
    """분산키는 컬럼 목록에 안 나온다. Greenplum 에서 가장 먼저 볼 값이다."""
    s, conn = make_shell()
    conn.result = (["name"], [("분산키: DISTRIBUTED BY (order_id)",)], -1)
    s.feed("\\d orders")
    assert conn.executed == ["DESCRIBE orders", "DISTKEY orders"]
    assert "분산키" in capsys.readouterr().out


def test_describe_without_extra_support():
    conn = FakeConnection()
    engine = sh.Engine("Impala", "x", lambda: conn,
                       describe_sql=lambda t: f"DESCRIBE {t}")
    s = sh.Shell(engine, argparse.Namespace(var=[], max_rows=100, sql_dir=None))
    s.conn = conn
    s.interactive = False
    s.feed("\\d orders")
    assert conn.executed == ["DESCRIBE orders"]


def test_dt_does_not_run_the_extra_query():
    s, conn = make_shell()
    s.feed("\\dt")
    assert conn.executed == ["SHOW TABLES"]


def test_run_text_sql_skips_null_rows(capsys):
    s, conn = make_shell()
    conn.result = (["x"], [("첫 줄",), (None,), ("셋째 줄",)], -1)
    s.run_text_sql("SELECT 1")
    assert capsys.readouterr().out.splitlines() == ["첫 줄", "셋째 줄"]


def test_ddl_is_completed():
    s, _ = make_shell()
    assert "\\ddl" in s.candidates("\\dd")
