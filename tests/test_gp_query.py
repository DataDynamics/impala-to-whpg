"""src/gp_query.py 검증 (실제 Greenplum 없이 가짜 커서로 실행)."""

import argparse
import gzip
import sys
import types
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import gp_query as g  # noqa: E402


class FakeCursor:
    """psycopg2 커서 흉내. description 이 None 이면 결과가 없는 문장이다."""

    def __init__(self, columns=None, rows=None, rowcount=-1) -> None:
        self.description = [(c,) for c in columns] if columns else None
        self._rows = list(rows or [])
        self.rowcount = rowcount
        self.executed: List[str] = []
        self.closed = False

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def fetchall(self):
        return list(self._rows)

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_psycopg2(monkeypatch):
    """psycopg2.connect 에 넘어간 인자를 기록하고 가짜 연결을 돌려준다."""
    state: dict = {"cursor": FakeCursor(rowcount=0)}

    def connect(**kwargs):
        state["connect"] = kwargs
        state["conn"] = FakeConnection(state["cursor"])
        return state["conn"]

    module = types.ModuleType("psycopg2")
    module.connect = connect
    monkeypatch.setitem(sys.modules, "psycopg2", module)
    return state


CONFIG = """\
greenplum:
  host: config-host
  port: 5433
  database: dw
  user: config-user
  schema: staging
"""


def parsed(tmp_path, body: str, argv: List[str]):
    """설정 파일을 기본 파일 자리에 놓고 인자를 파싱해 병합까지 마친다."""
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    original = g.appconfig.DEFAULT_CONFIG
    g.appconfig.DEFAULT_CONFIG = path
    try:
        parser = g.build_parser()
        args = parser.parse_args(argv + ["-q", "SELECT 1"])
        g.apply_config(
            args, g.load_greenplum_settings(args), parser, g.sqlfile.load_sql_settings(args)
        )
        return args
    finally:
        g.appconfig.DEFAULT_CONFIG = original


def args_for(**overrides) -> argparse.Namespace:
    options = dict(
        output=None,
        gzip=False,
        delimiter="`",
        encoding="utf-8",
        no_header=False,
        null_string="",
        max_rows=100,
    )
    options.update(overrides)
    return argparse.Namespace(**options)


# -- 인자 없이 실행 ---------------------------------------------------------------


def test_no_arguments_prints_help(capsys):
    assert g.main([]) == 0
    output = capsys.readouterr().out
    assert "usage: bin/gp-query" in output
    assert "--dry-run" in output


def test_no_arguments_does_not_connect(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise AssertionError("도움말만 보는데 접속하면 안 된다")

    monkeypatch.setattr(g, "import_psycopg2", fail)
    assert g.main([]) == 0
    assert "usage: bin/gp-query" in capsys.readouterr().out


# -- 설정 파일 ---------------------------------------------------------------------


def test_config_fills_connection_arguments(tmp_path):
    args = parsed(tmp_path, CONFIG, [])
    assert args.host == "config-host"
    assert args.port == 5433
    assert args.database == "dw"
    assert args.user == "config-user"
    assert args.schema == "staging"


def test_command_line_wins_over_config(tmp_path):
    args = parsed(tmp_path, CONFIG, ["--host", "cli-host", "-d", "other"])
    assert args.host == "cli-host"
    assert args.database == "other"
    assert args.user == "config-user"      # 안 준 값은 설정에서 온다


def test_port_defaults_to_5432(tmp_path):
    args = parsed(tmp_path, "greenplum:\n  host: h\n  database: d\n  user: u\n", [])
    assert args.port == 5432


@pytest.mark.parametrize(
    "body",
    [
        "greenplum:\n  database: d\n  user: u\n",   # host 없음
        "greenplum:\n  host: h\n  user: u\n",       # database 없음
        "greenplum:\n  host: h\n  database: d\n",   # user 없음
    ],
)
def test_missing_required_connection_value_is_reported(tmp_path, body):
    with pytest.raises(SystemExit):
        parsed(tmp_path, body, [])


def test_session_sql_from_config_comes_before_flags(tmp_path):
    body = CONFIG + "  session_sql:\n    - SET a = 1\n"
    args = parsed(tmp_path, body, ["--session-sql", "SET b = 2"])
    assert args.session_sql == ["SET a = 1", "SET b = 2"]


def test_session_sql_accepts_a_single_string(tmp_path):
    body = CONFIG + "  session_sql: SET a = 1\n"
    assert parsed(tmp_path, body, []).session_sql == ["SET a = 1"]


def test_password_comes_from_config(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_GP_PW", "from-env-ref")
    args = parsed(tmp_path, CONFIG + "  password: ${TEST_GP_PW}\n", [])
    assert g.resolve_password(args) == "from-env-ref"


def test_password_falls_back_to_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GP_PASSWORD", "from-env")
    args = parsed(tmp_path, CONFIG, [])
    assert g.resolve_password(args) == "from-env"


def test_missing_password_is_not_fatal(tmp_path, monkeypatch):
    """.pgpass 나 trust 인증을 쓰는 환경도 있으므로 막지 않는다."""
    monkeypatch.delenv("GP_PASSWORD", raising=False)
    args = parsed(tmp_path, CONFIG, ["--no-password-prompt"])
    assert g.resolve_password(args) is None


def test_shipped_default_config_has_a_greenplum_section():
    parser = g.build_parser()
    args = parser.parse_args(["-q", "SELECT 1"])
    assert g.load_greenplum_settings(args)["host"]


# -- 접속 인자 ---------------------------------------------------------------------


def test_connect_kwargs():
    args = argparse.Namespace(
        host="gp", port=5432, database="dw", user="etl", sslmode=None, timeout=None
    )
    assert g.build_connect_kwargs(args, "pw") == {
        "host": "gp",
        "port": 5432,
        "dbname": "dw",
        "user": "etl",
        "password": "pw",
    }


def test_connect_kwargs_omit_empty_values():
    """빈 값을 넘기면 libpq가 다르게 해석할 수 있어 아예 빼야 한다."""
    args = argparse.Namespace(
        host="gp", port=5432, database="dw", user="etl", sslmode=None, timeout=None
    )
    assert "password" not in g.build_connect_kwargs(args, None)
    assert "sslmode" not in g.build_connect_kwargs(args, None)
    assert "connect_timeout" not in g.build_connect_kwargs(args, None)


def test_connect_kwargs_include_ssl_and_timeout():
    args = argparse.Namespace(
        host="gp", port=5432, database="dw", user="etl", sslmode="require", timeout=10
    )
    kwargs = g.build_connect_kwargs(args, "pw")
    assert kwargs["sslmode"] == "require"
    assert kwargs["connect_timeout"] == 10


# -- 결과 표시 ---------------------------------------------------------------------


COLUMNS = ["order_dt", "status", "cnt"]
ROWS = [
    (date(2026, 8, 1), "결제완료", 12),
    (date(2026, 8, 1), "취소", None),
]


def test_render_table_aligns_columns():
    lines = g.render_table(COLUMNS, ROWS).splitlines()
    assert lines[0].startswith("order_dt   | status")
    assert set(lines[1]) <= {"-", "+"}
    assert len(lines) == 4


def test_render_table_pads_by_display_width():
    """한글은 두 칸을 차지하므로 그만큼 덜 채워야 열이 맞는다.

    마지막 열은 rstrip 되므로 구분자 위치로 확인한다.
    """
    lines = g.render_table(["a", "b"], [("한글", 1), ("ab", 2)]).splitlines()
    # 문자 개수가 아니라 표시 폭이 같아야 터미널에서 열이 맞는다
    assert g.display_width(lines[2].split("|")[0]) == g.display_width(
        lines[3].split("|")[0]
    )


def test_render_table_has_no_trailing_whitespace():
    for line in g.render_table(COLUMNS, ROWS).splitlines():
        assert line == line.rstrip()


def test_render_table_shows_null():
    assert "NULL" in g.render_table(COLUMNS, ROWS)


def test_select_prints_a_table(capsys):
    cursor = FakeCursor(COLUMNS, ROWS)
    assert g.run(cursor, "SELECT 1", args_for()) == 0
    out = capsys.readouterr()
    assert "order_dt" in out.out and "결제완료" in out.out
    assert "2행" in out.err


def test_empty_result_says_so(capsys):
    cursor = FakeCursor(COLUMNS, [])
    assert g.run(cursor, "SELECT 1", args_for()) == 0
    assert "(0행)" in capsys.readouterr().out


def test_max_rows_truncates_and_says_so(capsys):
    rows = [(date(2026, 8, 1), "s", i) for i in range(10)]
    cursor = FakeCursor(COLUMNS, rows)
    g.run(cursor, "SELECT 1", args_for(max_rows=3))
    out = capsys.readouterr()
    assert out.out.count("2026-08-01") == 3
    assert "10행" in out.err and "--max-rows 0" in out.err


def test_max_rows_zero_shows_everything(capsys):
    rows = [(date(2026, 8, 1), "s", i) for i in range(10)]
    g.run(FakeCursor(COLUMNS, rows), "SELECT 1", args_for(max_rows=0))
    out = capsys.readouterr()
    assert out.out.count("2026-08-01") == 10
    assert "--max-rows 0" not in out.err


# -- 결과가 없는 문장 --------------------------------------------------------------


def test_dml_reports_affected_rows(capsys):
    cursor = FakeCursor(rowcount=42)
    assert g.run(cursor, "DELETE FROM t", args_for()) == 0
    assert "42행" in capsys.readouterr().err


def test_ddl_without_rowcount_just_reports_done(capsys):
    """DDL 은 rowcount 가 -1 이라 행 수를 말하면 안 된다."""
    cursor = FakeCursor(rowcount=-1)
    assert g.run(cursor, "TRUNCATE t", args_for()) == 0
    err = capsys.readouterr().err
    assert "완료" in err and "-1" not in err


# -- CSV 출력 ----------------------------------------------------------------------


def test_output_writes_csv(tmp_path):
    path = tmp_path / "out.csv"
    cursor = FakeCursor(COLUMNS, ROWS)
    g.run(cursor, "SELECT 1", args_for(output=str(path)))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "order_dt`status`cnt"
    assert lines[1] == "2026-08-01`결제완료`12"
    assert lines[2] == "2026-08-01`취소`"      # NULL 은 기본이 빈 값


def test_output_gzip(tmp_path):
    path = tmp_path / "out.csv.gz"
    g.run(FakeCursor(COLUMNS, ROWS), "SELECT 1", args_for(output=str(path), gzip=True))
    with gzip.open(path, "rt", encoding="utf-8") as fp:
        assert "결제완료" in fp.read()


def test_output_respects_delimiter_and_header(tmp_path):
    path = tmp_path / "out.csv"
    g.run(
        FakeCursor(COLUMNS, ROWS),
        "SELECT 1",
        args_for(output=str(path), delimiter=",", no_header=True, null_string="\\N"),
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "2026-08-01,결제완료,12"
    assert lines[1].endswith("\\N")


def test_output_of_decimal_keeps_precision(tmp_path):
    path = tmp_path / "out.csv"
    g.run(
        FakeCursor(["amount"], [(Decimal("10.50"),)]),
        "SELECT 1",
        args_for(output=str(path), no_header=True),
    )
    assert path.read_text(encoding="utf-8").strip() == "10.50"


# -- 트랜잭션 ----------------------------------------------------------------------


def run_main(fake_psycopg2, argv, tmp_path, body=CONFIG):
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return g.main(["--config", str(path), "--no-password-prompt"] + argv)


def test_success_commits(fake_psycopg2, tmp_path):
    fake_psycopg2["cursor"] = FakeCursor(rowcount=3)
    assert run_main(fake_psycopg2, ["-q", "DELETE FROM t"], tmp_path) == 0
    assert fake_psycopg2["conn"].committed is True
    assert fake_psycopg2["conn"].rolled_back is False


def test_dry_run_rolls_back(fake_psycopg2, tmp_path, capsys):
    fake_psycopg2["cursor"] = FakeCursor(rowcount=3)
    assert run_main(fake_psycopg2, ["-q", "DELETE FROM t", "--dry-run"], tmp_path) == 0
    assert fake_psycopg2["conn"].rolled_back is True
    assert fake_psycopg2["conn"].committed is False
    assert "롤백" in capsys.readouterr().err


def test_failure_rolls_back_and_returns_5(fake_psycopg2, tmp_path, capsys):
    class Failing(FakeCursor):
        def execute(self, sql):
            raise RuntimeError("relation \"t\" does not exist")

    fake_psycopg2["cursor"] = Failing()
    assert run_main(fake_psycopg2, ["-q", "SELECT * FROM t"], tmp_path) == 5
    assert fake_psycopg2["conn"].rolled_back is True
    assert fake_psycopg2["conn"].committed is False
    assert "does not exist" in capsys.readouterr().err


def test_connection_is_always_closed(fake_psycopg2, tmp_path):
    fake_psycopg2["cursor"] = FakeCursor(rowcount=0)
    run_main(fake_psycopg2, ["-q", "SELECT 1"], tmp_path)
    assert fake_psycopg2["conn"].closed is True


def test_schema_and_session_sql_run_before_the_query(fake_psycopg2, tmp_path):
    cursor = FakeCursor(rowcount=0)
    fake_psycopg2["cursor"] = cursor
    run_main(
        fake_psycopg2,
        ["-q", "SELECT 1", "--session-sql", "SET a = 1"],
        tmp_path,
        body=CONFIG,
    )
    assert cursor.executed == ["SET search_path TO staging", "SET a = 1", "SELECT 1"]


# -- 템플릿 (sqlfile 공유) ---------------------------------------------------------


def test_query_file_template_is_rendered(fake_psycopg2, tmp_path):
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "q.sql").write_text("SELECT '{{ dt }}'", encoding="utf-8")

    cursor = FakeCursor(rowcount=0)
    fake_psycopg2["cursor"] = cursor
    run_main(
        fake_psycopg2,
        ["-f", "q.sql", "-V", "dt=2026-08-01", "--sql-dir", str(sql_dir)],
        tmp_path,
    )
    assert cursor.executed[-1] == "SELECT '2026-08-01'"


def test_sql_dir_comes_from_config(tmp_path):
    (tmp_path / "queries").mkdir()
    args = parsed(tmp_path, CONFIG + "sql:\n  dir: queries\n", [])
    assert args.sql_dir == str(tmp_path / "queries")


def test_undefined_template_variable_is_an_error(fake_psycopg2, tmp_path):
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "q.sql").write_text("SELECT '{{ dt }}'", encoding="utf-8")

    with pytest.raises(SystemExit):
        run_main(fake_psycopg2, ["-f", "q.sql", "--sql-dir", str(sql_dir)], tmp_path)


def test_shipped_greenplum_template_renders():
    args = argparse.Namespace(
        query=None,
        query_file=str(g.appconfig.SQL_DIR / "order_summary.sql"),
        var=["dt=2026-08-01"],
    )
    sql = g.sqlfile.read_query(args)
    assert "2026-08-01" in sql and "staging.orders" in sql


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
