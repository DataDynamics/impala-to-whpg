"""src/shell.py 검증 — 대화형 SQL 셸 (실제 DB 없이 가짜 커서로 실행)."""

import argparse
import sys
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
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise RuntimeError("문법 오류")
        columns, rows, rowcount = self.conn.result_for(sql)
        self.description = [(c,) for c in columns] if columns else None
        self._rows = list(rows)
        self._offset = 0
        self.rowcount = rowcount

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
        self.fail_on: str = ""
        self.result = ([], [], -1)      # (columns, rows, rowcount)

    def result_for(self, sql: str):
        if sql.strip().upper().startswith(("BEGIN", "COMMIT", "ROLLBACK", "SET")):
            return ([], [], -1)
        return self.result

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

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
