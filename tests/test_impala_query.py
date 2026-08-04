"""src/impala_query.py 검증 (실제 Impala 없이 가짜 커서로 실행)."""

import argparse
import gzip
import io
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
import types
from typing import List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import impala_query as q  # noqa: E402


class FakeCursor:
    """fetchmany로 배치를 나눠 돌려주는 최소 DB-API 커서."""

    def __init__(self, columns, rows) -> None:
        self.description = [(c, "STRING") for c in columns]
        self._rows = list(rows)
        self._offset = 0
        self.arraysize = 1
        self.executed: List[str] = []

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def fetchmany(self, size: int):
        batch = self._rows[self._offset : self._offset + size]
        self._offset += len(batch)
        return batch

    def close(self) -> None:
        return None


COLUMNS = ["order_id", "name", "amount", "order_dt"]
ROWS = [
    (1, "김철수", Decimal("10.50"), date(2026, 8, 1)),
    (2, "쉼표, 포함", None, date(2026, 8, 2)),
    (3, '따옴표" 포함', Decimal("0"), date(2026, 8, 3)),
]


def run_export(rows=ROWS, columns=COLUMNS, **overrides) -> str:
    options = dict(
        batch_size=2,
        delimiter="`",
        null_string="",
        write_header=True,
        quote=False,
        escapechar="\\",
    )
    options.update(overrides)
    buffer = io.StringIO()
    cursor = FakeCursor(columns, rows)
    count = run_export.last_count = q.export(  # type: ignore[attr-defined]
        cursor, "SELECT 1", buffer, q.PhaseTimer(), **options
    )
    assert count == len(rows)
    return buffer.getvalue()


# -- CSV 출력 --------------------------------------------------------------------


def test_writes_header_and_rows():
    output = run_export()
    lines = output.splitlines()

    # 기본 구분자는 백틱, 값은 따옴표로 감싸지 않는다
    assert lines[0] == "order_id`name`amount`order_dt"
    assert lines[1] == "1`김철수`10.50`2026-08-01"
    assert len(lines) == 4


def test_values_are_not_quoted_by_default():
    lines = run_export().splitlines()
    # 쉼표나 큰따옴표가 들어 있어도 감싸지 않는다
    assert lines[2] == "2`쉼표, 포함``2026-08-02"
    assert lines[3] == '3`따옴표" 포함`0`2026-08-03'


def test_quote_option_wraps_only_when_needed():
    lines = run_export(quote=True).splitlines()
    # QUOTE_MINIMAL은 구분자(백틱)가 없으면 감싸지 않는다. 쉼표는 구분자가 아니다.
    assert lines[2] == "2`쉼표, 포함``2026-08-02"
    # 큰따옴표가 들어 있으면 감싸고 두 번 반복해 이스케이프한다
    assert lines[3] == '3`"따옴표"" 포함"`0`2026-08-03'


def test_delimiter_in_value_is_escaped_when_not_quoted():
    rows = [(1, "백틱`포함", Decimal("1"), date(2026, 8, 1))]
    line = run_export(rows=rows).splitlines()[1]
    assert line == "1`백틱\\`포함`1`2026-08-01"


def test_delimiter_in_value_is_quoted_when_quoting_on():
    rows = [(1, "백틱`포함", Decimal("1"), date(2026, 8, 1))]
    line = run_export(rows=rows, quote=True).splitlines()[1]
    assert line == '1`"백틱`포함"`1`2026-08-01'


def test_without_escapechar_delimiter_in_value_fails_loudly():
    import csv as _csv

    rows = [(1, "백틱`포함", Decimal("1"), date(2026, 8, 1))]
    with pytest.raises(_csv.Error, match="escape"):
        run_export(rows=rows, escapechar="")


def test_null_becomes_empty_by_default():
    assert "``" in run_export().splitlines()[2]


def test_null_string_is_applied():
    lines = run_export(null_string="NULL").splitlines()
    assert lines[2] == "2`쉼표, 포함`NULL`2026-08-02"


def test_null_string_containing_escapechar_gets_escaped():
    """이스케이프 문자가 들어간 NULL 표기는 csv가 한 번 더 이스케이프한다."""
    lines = run_export(null_string="\\N").splitlines()
    assert lines[2] == "2`쉼표, 포함`\\\\N`2026-08-02"
    # 그대로 내보내려면 이스케이프를 끄면 된다
    lines = run_export(null_string="\\N", escapechar="").splitlines()
    assert lines[2] == "2`쉼표, 포함`\\N`2026-08-02"


def test_no_header():
    lines = run_export(write_header=False).splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("1`")


def test_custom_delimiter():
    lines = run_export(delimiter="\t").splitlines()
    assert lines[0] == "order_id\tname\tamount\torder_dt"
    assert lines[2] == "2\t쉼표, 포함\t\t2026-08-02"


def test_comma_delimiter_still_available():
    lines = run_export(delimiter=",").splitlines()
    assert lines[0] == "order_id,name,amount,order_dt"


def test_empty_result_writes_header_only():
    output = run_export(rows=[])
    assert output == "order_id`name`amount`order_dt\r\n"


def test_batches_are_all_consumed():
    rows = [(i, f"n{i}", Decimal("1"), date(2026, 8, 1)) for i in range(250)]
    lines = run_export(rows=rows, batch_size=7).splitlines()
    assert len(lines) == 251  # 헤더 + 250행


def test_column_name_is_stripped_of_table_prefix():
    cursor = FakeCursor(["orders.order_id", "orders.name"], [(1, "a")])
    buffer = io.StringIO()
    q.export(
        cursor,
        "SELECT 1",
        buffer,
        q.PhaseTimer(),
        batch_size=10,
        delimiter="`",
        null_string="",
        write_header=True,
    )
    assert buffer.getvalue().splitlines()[0] == "order_id`name"


def test_export_defaults_to_unquoted():
    """export()를 직접 부를 때도 기본은 따옴표 끔이다."""
    cursor = FakeCursor(["a"], [('따옴표" 있음',)])
    buffer = io.StringIO()
    q.export(
        cursor,
        "SELECT 1",
        buffer,
        q.PhaseTimer(),
        batch_size=10,
        delimiter="`",
        null_string="",
        write_header=False,
    )
    assert buffer.getvalue() == '따옴표" 있음\r\n'


def test_arraysize_is_set_before_execute():
    cursor = FakeCursor(COLUMNS, ROWS)
    q.export(
        cursor,
        "SELECT 1",
        io.StringIO(),
        q.PhaseTimer(),
        batch_size=1234,
        delimiter="`",
        null_string="",
        write_header=True,
    )
    assert cursor.arraysize == 1234
    assert cursor.executed == ["SELECT 1"]


def test_make_writer_quoting_modes():
    import csv as _csv

    quoted = q.make_writer(io.StringIO(), "`", quote=True, escapechar=None)
    assert quoted.dialect.quoting == _csv.QUOTE_MINIMAL

    raw = q.make_writer(io.StringIO(), "`", quote=False, escapechar="\\")
    assert raw.dialect.quoting == _csv.QUOTE_NONE
    assert raw.dialect.escapechar == "\\"


# -- 파일 열기 -------------------------------------------------------------------


def test_open_output_plain(tmp_path):
    path = tmp_path / "out.csv"
    with q.open_output(str(path), use_gzip=False, encoding="utf-8") as handle:
        handle.write("가나다\n")
    assert path.read_text(encoding="utf-8") == "가나다\n"


def test_open_output_gzip(tmp_path):
    path = tmp_path / "out.csv.gz"
    with q.open_output(str(path), use_gzip=True, encoding="utf-8") as handle:
        handle.write("가나다\n")
    with gzip.open(path, "rt", encoding="utf-8") as fp:
        assert fp.read() == "가나다\n"


def test_open_output_utf8_sig_for_excel(tmp_path):
    path = tmp_path / "out.csv"
    with q.open_output(str(path), use_gzip=False, encoding="utf-8-sig") as handle:
        handle.write("가나다\n")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")  # BOM


# -- 접속 설정 -------------------------------------------------------------------


def make_args(**overrides) -> argparse.Namespace:
    options = dict(
        host="impala.example.com",
        port=21050,
        database="sales",
        user="etl_user",
        ca_cert="/etc/ssl/certs/impala-ca.pem",
        timeout=30,
        password_env="IMPALA_PASSWORD",
        auth_mechanism="PLAIN",
        kerberos_service_name="impala",
        no_ssl=False,
        http_transport=False,
        http_path="cliservice",
        sasl_backend="puresasl",
        debug=False,
    )
    options.update(overrides)
    return argparse.Namespace(**options)


def test_connect_kwargs_use_tls_and_ldap():
    kwargs = q.build_connect_kwargs(make_args(), "secret")

    assert kwargs["auth_mechanism"] == "PLAIN"   # impyla에서 LDAP을 뜻한다
    assert kwargs["use_ssl"] is True
    assert kwargs["host"] == "impala.example.com"
    assert kwargs["port"] == 21050
    assert kwargs["database"] == "sales"
    assert kwargs["user"] == "etl_user"
    assert kwargs["password"] == "secret"
    assert kwargs["ca_cert"] == "/etc/ssl/certs/impala-ca.pem"
    assert kwargs["timeout"] == 30
    # LDAP 인증에는 커버로스 서비스명이 끼어들면 안 된다
    assert "kerberos_service_name" not in kwargs


def test_connect_kwargs_omit_unset_optionals():
    kwargs = q.build_connect_kwargs(make_args(ca_cert=None, timeout=None), "secret")
    assert kwargs["use_ssl"] is True
    # None을 넘기면 impyla 버전에 따라 동작이 갈리므로 아예 빼야 한다
    assert "ca_cert" not in kwargs
    assert "timeout" not in kwargs


def test_http_transport_kwargs():
    kwargs = q.build_connect_kwargs(
        make_args(http_transport=True, http_path="cliservice", port=28000), "secret"
    )
    assert kwargs["use_http_transport"] is True
    assert kwargs["http_path"] == "cliservice"
    assert kwargs["port"] == 28000


def test_binary_transport_omits_http_keys():
    kwargs = q.build_connect_kwargs(make_args(), "secret")
    assert "use_http_transport" not in kwargs
    assert "http_path" not in kwargs


def test_no_ssl_turns_tls_off():
    assert q.build_connect_kwargs(make_args(no_ssl=True), "secret")["use_ssl"] is False


def test_nosasl_omits_credentials():
    kwargs = q.build_connect_kwargs(make_args(auth_mechanism="NOSASL"), None)
    assert kwargs["auth_mechanism"] == "NOSASL"
    assert "user" not in kwargs and "password" not in kwargs


def test_gssapi_adds_service_name():
    kwargs = q.build_connect_kwargs(
        make_args(auth_mechanism="GSSAPI", kerberos_service_name="impala"), "x"
    )
    assert kwargs["kerberos_service_name"] == "impala"


def test_plain_omits_kerberos_service_name():
    assert "kerberos_service_name" not in q.build_connect_kwargs(make_args(), "x")


# -- SASL 백엔드 -----------------------------------------------------------------


def test_cyrus_sasl_signature_matches_impyla_call():
    """impyla는 PureSASLClient(host, username=, password=, service=) 로 부른다."""
    import inspect

    params = inspect.signature(q.CyrusSASLClient.__init__).parameters
    assert list(params) == ["self", "host", "username", "password", "service"]
    for optional in ("username", "password", "service"):
        assert params[optional].default is None


def test_cyrus_sasl_delegates_to_client(monkeypatch):
    calls = []

    class FakeClient:
        def setAttr(self, key, value):
            calls.append(("setAttr", key, value))

        def init(self):
            calls.append(("init",))

        def start(self, mechanism):
            return True, mechanism, b"token"

        def step(self, challenge):
            return True, b"next"

        def encode(self, incoming):
            return True, incoming

        def decode(self, outgoing):
            return True, outgoing

        def getError(self):
            return "그런 오류"

    fake_module = types.ModuleType("sasl")
    fake_module.Client = FakeClient
    monkeypatch.setitem(sys.modules, "sasl", fake_module)

    client = q.CyrusSASLClient("impala.example.com", username="u", password="p", service="impala")

    assert ("setAttr", "host", "impala.example.com") in calls
    assert ("setAttr", "service", "impala") in calls
    assert ("setAttr", "username", "u") in calls
    assert ("setAttr", "password", "p") in calls
    assert ("init",) in calls

    assert client.start("PLAIN") == (True, "PLAIN", b"token")
    assert client.step(b"c") == (True, b"next")
    assert client.encode(b"x") == (True, b"x")
    assert client.decode(b"y") == (True, b"y")
    assert client.getError() == "그런 오류"


def test_cyrus_sasl_defaults_service_to_impala(monkeypatch):
    seen = {}

    class FakeClient:
        def setAttr(self, key, value):
            seen[key] = value

        def init(self):
            pass

    fake_module = types.ModuleType("sasl")
    fake_module.Client = FakeClient
    monkeypatch.setitem(sys.modules, "sasl", fake_module)

    q.CyrusSASLClient("h")
    assert seen["service"] == "impala"
    # 값이 없는 항목은 설정하지 않는다
    assert "username" not in seen and "password" not in seen


def test_use_cyrus_sasl_without_package_explains(monkeypatch):
    monkeypatch.setattr(q.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(ImportError) as exc:
        q.use_cyrus_sasl()

    message = str(exc.value)
    assert "pip install sasl" in message
    # 3.11 이상에서 빌드되지 않는다는 점을 알려준다
    assert "3.11" in message
    assert "puresasl" in message


def test_use_cyrus_sasl_swaps_impyla_client(monkeypatch):
    sasl_compat = pytest.importorskip("impala.sasl_compat")
    original = sasl_compat.PureSASLClient
    monkeypatch.setattr(q.importlib.util, "find_spec", lambda name: object())
    try:
        q.use_cyrus_sasl()
        # impyla의 get_transport가 함수 안에서 임포트하므로 이 교체가 반영된다
        assert sasl_compat.PureSASLClient is q.CyrusSASLClient
    finally:
        sasl_compat.PureSASLClient = original


# -- 전송 오류 진단 ---------------------------------------------------------------


def test_transport_hint_lists_ports_and_current_setting():
    hint = q.transport_error_hint(make_args())
    assert "EOF" in hint
    assert "21050" in hint and "28000" in hint
    assert "impala.example.com:21050" in hint
    assert "바이너리 전송" in hint


def test_transport_hint_suggests_http_when_binary():
    hint = q.transport_error_hint(make_args(http_transport=False))
    assert "--http-transport 로 한 번 바꿔보세요" in hint


def test_transport_hint_shows_http_path_when_http():
    hint = q.transport_error_hint(make_args(http_transport=True, http_path="cliservice"))
    assert "cliservice" in hint
    assert "--http-transport 로 한 번 바꿔보세요" not in hint


def test_transport_hint_flips_tls_advice():
    assert "--no-ssl 을 주세요" in q.transport_error_hint(make_args(no_ssl=False))
    assert "--no-ssl 을 빼세요" in q.transport_error_hint(make_args(no_ssl=True))


def test_connect_kwargs_match_impyla_signature():
    """impyla가 실제로 받는 인자만 넘기는지 확인한다(설치돼 있을 때만)."""
    dbapi = pytest.importorskip("impala.dbapi")
    import inspect

    accepted = set(inspect.signature(dbapi.connect).parameters)
    passed = set(q.build_connect_kwargs(make_args(), "secret"))
    assert passed <= accepted, f"impyla가 모르는 인자: {passed - accepted}"


# -- SQL 읽기 (파일 내용을 그대로 실행) -------------------------------------------


MULTILINE = "SELECT\n    order_id,\n    amount\nFROM sales.orders\nWHERE dt = '2026-08-01'"


def read_from(tmp_path, text, encoding="utf-8", newline="\n", var=None):
    path = tmp_path / "daily.sql"
    path.write_bytes(text.replace("\n", newline).encode(encoding))
    return q.read_query(
        argparse.Namespace(query=None, query_file=str(path), var=var or [])
    )


def test_file_is_executed_as_is(tmp_path):
    # 세미콜론도 주석도 손대지 않고 그대로 넘긴다
    text = MULTILINE + ";\n-- 끝\n"
    assert read_from(tmp_path, text) == text


def test_multiline_query_keeps_line_breaks(tmp_path):
    assert read_from(tmp_path, MULTILINE) == MULTILINE


def test_multiple_statements_are_passed_through(tmp_path):
    text = "SELECT 1 FROM t;\nSELECT 2 FROM t;\n"
    assert read_from(tmp_path, text) == text


def test_bom_is_stripped_by_decoding(tmp_path):
    """BOM 제거는 SQL 수정이 아니라 인코딩 해석이다."""
    result = read_from(tmp_path, MULTILINE, encoding="utf-8-sig")
    assert result == MULTILINE
    assert not result.startswith("\ufeff")


def test_crlf_becomes_lf(tmp_path):
    # 텍스트 모드 유니버설 개행 처리
    assert "\r" not in read_from(tmp_path, MULTILINE, newline="\r\n")


def test_bom_and_crlf_together(tmp_path):
    result = read_from(tmp_path, MULTILINE, encoding="utf-8-sig", newline="\r\n")
    assert result == MULTILINE


def test_read_query_from_argument():
    args = argparse.Namespace(query=MULTILINE + ";", query_file=None, var=[])
    assert q.read_query(args) == MULTILINE + ";"


# -- 문법 오류 힌트 ---------------------------------------------------------------


class SyntaxError_(Exception):
    pass


def test_hint_only_for_syntax_errors():
    assert q.query_error_hint("SELECT 1", RuntimeError("연결 끊김")) == ""
    assert q.query_error_hint("SELECT 1", SyntaxError_("Syntax error in line 1")) != ""
    assert q.query_error_hint("SELECT 1", SyntaxError_("AnalysisException: ...")) != ""


def test_hint_mentions_trailing_semicolon():
    hint = q.query_error_hint("SELECT 1 FROM t;\n", SyntaxError_("Syntax error"))
    assert "끝에 세미콜론" in hint


def test_hint_mentions_multiple_statements():
    hint = q.query_error_hint("SELECT 1; SELECT 2", SyntaxError_("Syntax error"))
    assert "문장이 여러 개" in hint


def test_hint_without_semicolon_stays_short():
    hint = q.query_error_hint("SELECT 1 FROM t", SyntaxError_("Syntax error"))
    assert "세미콜론" not in hint
    assert "--debug" in hint


# -- 세션 설정 -------------------------------------------------------------------


def test_parse_session_settings():
    assert q.parse_session_settings(["MEM_LIMIT=8g", "REQUEST_POOL=etl"]) == {
        "MEM_LIMIT": "8g",
        "REQUEST_POOL": "etl",
    }


def test_parse_session_settings_strips_spaces():
    assert q.parse_session_settings([" MEM_LIMIT = 8g "]) == {"MEM_LIMIT": "8g"}


def test_parse_session_settings_empty():
    assert q.parse_session_settings([]) == {}


@pytest.mark.parametrize("bad", ["MEM_LIMIT", "=8g", ""])
def test_parse_session_settings_rejects_malformed(bad):
    with pytest.raises(SystemExit, match="KEY=VALUE"):
        q.parse_session_settings([bad])


# -- 단독 실행 (프로젝트 패키지 비의존) --------------------------------------------


def test_script_has_no_unexpected_dependency():
    """의존성은 impyla와 같은 디렉터리의 appconfig 로 한정한다."""
    import ast

    source = Path(q.__file__).read_text(encoding="utf-8")
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])

    stdlib = {
        "argparse", "contextlib", "csv", "getpass", "gzip", "importlib",
        "logging", "os", "sys", "time", "typing", "unicodedata",
    }
    third_party = {
        "impala",     # 유일한 필수 외부 의존성
        "sasl",       # 선택: --sasl-backend sasl 일 때만 임포트한다
        "jinja2",     # 선택: SQL에 템플릿 문법이 있을 때만 임포트한다
        "appconfig",  # 같은 디렉터리의 설정 로더. PyYAML은 그쪽에서 지연 임포트한다.
        "sqlfile",    # 같은 디렉터리의 쿼리 로더. Jinja2는 그쪽에서 지연 임포트한다.
        "progress",   # 같은 디렉터리의 소요 시간·진행 상황 보고
        "table",      # 같은 디렉터리의 표·CSV 출력
        "shell",      # 같은 디렉터리의 대화형 셸
    }
    assert modules <= stdlib | third_party, (
        f"허용되지 않은 의존성: {modules - stdlib - third_party}"
    )


def test_yaml_is_not_imported_at_module_level():
    """--no-config 로만 쓴다면 PyYAML 없이도 돌아야 한다."""
    import ast

    source = Path(q.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            assert all(a.name != "yaml" for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "yaml"


# -- impyla 임포트 진단 (스크립트 자체 구현) ---------------------------------------


def spec_for(origin, is_package):
    import types

    return types.SimpleNamespace(
        origin=origin,
        submodule_search_locations=["/some/dir"] if is_package else None,
    )


def test_missing_impyla_hint(monkeypatch):
    monkeypatch.setattr(q.importlib.util, "find_spec", lambda name: None)
    hint = q._import_hint(ImportError("No module named 'impala'"))
    assert "pip install impyla" in hint


def test_shadowing_file_hint(monkeypatch):
    monkeypatch.setattr(
        q.importlib.util, "find_spec", lambda name: spec_for("/work/impala.py", False)
    )
    hint = q._import_hint(ImportError("'impala' is not a package"))
    assert "/work/impala.py" in hint and "가리고 있습니다" in hint


def test_shadowing_directory_hint(monkeypatch):
    monkeypatch.setattr(q.importlib.util, "find_spec", lambda name: spec_for(None, True))
    hint = q._import_hint(ImportError("No module named 'impala.dbapi'"))
    assert "디렉터리가 impyla 패키지를 가리고 있습니다" in hint


def test_sasl_dependency_check(monkeypatch):
    monkeypatch.setattr(q.importlib.util, "find_spec", lambda name: None)
    q.check_auth_dependencies("NOSASL")  # SASL 불필요

    with pytest.raises(ImportError) as exc:
        q.check_auth_dependencies("PLAIN")
    assert "pip install pure-sasl thrift-sasl" in str(exc.value)
    assert "--use-pep517" in str(exc.value)


def test_password_from_environment(monkeypatch):
    monkeypatch.setenv("IMPALA_PASSWORD", "from-env")
    assert q.resolve_password(make_args()) == "from-env"


def test_password_env_can_be_renamed(monkeypatch):
    monkeypatch.setenv("OTHER_PW", "xyz")
    assert q.resolve_password(make_args(password_env="OTHER_PW")) == "xyz"


def test_missing_password_without_tty_exits(monkeypatch):
    monkeypatch.delenv("IMPALA_PASSWORD", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit, match="IMPALA_PASSWORD"):
        q.resolve_password(make_args())


def test_password_is_not_a_command_line_option():
    # ps로 비밀번호가 노출되지 않도록 --password 인자는 두지 않는다
    actions = {a.option_strings[0] for a in q.build_parser()._actions if a.option_strings}
    assert "--password" not in actions
    assert "--password-env" in actions


# -- 시간 측정 -------------------------------------------------------------------


def test_timer_accumulates_repeated_phases():
    timer = q.PhaseTimer()
    for _ in range(3):
        with timer.measure("데이터 수신"):
            pass
        with timer.measure("CSV 쓰기"):
            pass

    report = timer.report()
    assert "데이터 수신" in report and "CSV 쓰기" in report
    # 같은 이름은 한 줄로 합쳐진다
    assert report.count("데이터 수신") == 1
    assert "합계" in report


def test_timer_report_lists_phases_in_order():
    timer = q.PhaseTimer()
    for name in ("Impala 접속", "쿼리 실행 요청", "첫 배치 대기"):
        with timer.measure(name):
            pass
    lines = [l for l in timer.report().splitlines() if l.strip().startswith(("1.", "2.", "3."))]
    assert "Impala 접속" in lines[0]
    assert "쿼리 실행 요청" in lines[1]
    assert "첫 배치 대기" in lines[2]


def test_declared_order_wins_over_first_use():
    # 헤더를 먼저 쓰느라 'CSV 쓰기'가 실제로는 앞서 호출되어도,
    # 보고서는 선언한 파이프라인 순서대로 나와야 한다
    timer = q.PhaseTimer(q.PHASES)
    for name in ("CSV 쓰기", "첫 배치 대기", "Impala 접속"):
        with timer.measure(name):
            pass

    numbered = [l for l in timer.report().splitlines() if l.strip()[:2] in ("1.", "2.", "3.")]
    assert "Impala 접속" in numbered[0]
    assert "첫 배치 대기" in numbered[1]
    assert "CSV 쓰기" in numbered[2]


def test_unused_phases_are_hidden():
    timer = q.PhaseTimer(q.PHASES)
    with timer.measure("Impala 접속"):
        pass
    report = timer.report()
    assert "Impala 접속" in report
    assert "데이터 수신" not in report   # 한 번도 실행되지 않았다


def test_report_columns_align_with_mixed_width_names():
    timer = q.PhaseTimer()
    for name in ("Impala 접속", "쿼리 실행 요청", "첫 배치 대기", "CSV 쓰기"):
        with timer.measure(name):
            pass

    # 한글은 두 칸을 차지하므로 len()이 아니라 표시 폭으로 정렬돼야 한다
    rows = [l for l in timer.report().splitlines() if "초 " in l or l.endswith("%")]
    positions = {q.display_width(line.split("초")[0]) for line in rows if "초" in line}
    assert len(positions) == 1, f"초 단위 열이 어긋납니다: {positions}"


def test_display_width_counts_hangul_as_two():
    assert q.display_width("abc") == 3
    assert q.display_width("한글") == 4
    assert q.display_width("CSV 쓰기") == 4 + 4  # "CSV " 4칸 + "쓰기" 4칸


def test_pad_uses_display_width():
    assert q.display_width(q.pad("한글", 10)) == 10
    assert q.display_width(q.pad("abc", 10)) == 10
    assert q.pad("너무긴이름", 2) == "너무긴이름"  # 폭보다 길면 자르지 않는다


def test_export_records_all_phases():
    timer = q.PhaseTimer()
    q.export(
        FakeCursor(COLUMNS, ROWS),
        "SELECT 1",
        io.StringIO(),
        timer,
        batch_size=2,
        delimiter=",",
        null_string="",
        write_header=True,
    )
    report = timer.report()
    for phase in ("쿼리 실행 요청", "첫 배치 대기", "데이터 수신", "CSV 쓰기"):
        assert phase in report


def test_human_readable_size():
    assert q.human(0) == "0.0B"
    assert q.human(1536) == "1.5KB"
    assert q.human(5 * 1024**3) == "5.0GB"


# -- 설정 파일 ---------------------------------------------------------------------


def parsed(tmp_path, body: str, argv: List[str]):
    """설정 파일을 기본 파일 자리에 놓고 인자를 파싱해 병합까지 마친다."""
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    (tmp_path / "impala-ca.pem").write_text("-----BEGIN CERTIFICATE-----", encoding="utf-8")
    original = q.appconfig.DEFAULT_CONFIG
    q.appconfig.DEFAULT_CONFIG = path
    try:
        parser = q.build_parser()
        args = parser.parse_args(argv + ["-q", "SELECT 1", "-o", "out.csv"])
        q.apply_config(
            args, q.load_impala_settings(args), parser, q.load_sql_settings(args)
        )
        return args
    finally:
        q.appconfig.DEFAULT_CONFIG = original


CONFIG = """\
impala:
  host: config-host
  port: 21051
  database: sales
  user: config-user
  auth_mechanism: PLAIN
  use_ssl: true
  ca_cert: impala-ca.pem
  session_settings:
    MEM_LIMIT: 8g
"""


def test_config_fills_connection_arguments(tmp_path):
    args = parsed(tmp_path, CONFIG, [])
    assert args.host == "config-host"
    assert args.port == 21051
    assert args.database == "sales"
    assert args.user == "config-user"
    # 상대 경로는 설정 파일이 있는 디렉터리 기준으로 풀린다
    assert args.ca_cert == str(tmp_path / "impala-ca.pem")
    assert args.no_ssl is False


def test_command_line_wins_over_config(tmp_path):
    args = parsed(tmp_path, CONFIG, ["--host", "cli-host", "--port", "28000"])
    assert args.host == "cli-host"
    assert args.port == 28000
    assert args.user == "config-user"      # 안 준 값은 설정에서 온다


def test_no_config_ignores_the_file(tmp_path):
    args = parsed(tmp_path, CONFIG, ["--no-config", "--host", "h", "-u", "u"])
    assert args.host == "h"
    assert args.database == "default"      # 설정이 아니라 기본값
    assert args.port == 21050


def test_config_use_ssl_false_turns_off_tls(tmp_path):
    args = parsed(tmp_path, "impala:\n  host: h\n  user: u\n  use_ssl: false\n", [])
    assert args.no_ssl is True


def test_no_ssl_flag_is_not_overridden_by_config(tmp_path):
    args = parsed(tmp_path, CONFIG, ["--no-ssl"])
    assert args.no_ssl is True


def test_config_session_settings_are_merged_before_set(tmp_path):
    """--set 이 설정의 session_settings 를 덮어쓴다."""
    args = parsed(tmp_path, CONFIG, ["--set", "MEM_LIMIT=16g", "--set", "NUM_NODES=1"])
    assert q.parse_session_settings(args.set) == {"MEM_LIMIT": "16g", "NUM_NODES": "1"}


def test_config_password_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PW", "from-env-ref")
    args = parsed(tmp_path, "impala:\n  host: h\n  user: u\n  password: ${TEST_PW}\n", [])
    assert q.resolve_password(args) == "from-env-ref"


def test_missing_host_is_reported(tmp_path):
    with pytest.raises(SystemExit):
        parsed(tmp_path, "impala:\n  user: u\n", [])


def test_missing_user_is_reported(tmp_path):
    with pytest.raises(SystemExit):
        parsed(tmp_path, "impala:\n  host: h\n", [])


def test_nosasl_does_not_need_a_user(tmp_path):
    args = parsed(tmp_path, "impala:\n  host: h\n", ["--auth-mechanism", "NOSASL"])
    assert args.user is None


# -- 표로 보여주기 (-o 없이) -------------------------------------------------------


def test_output_is_optional():
    """-o 없이도 파싱된다. 예전에는 required 였다."""
    args = q.build_parser().parse_args(["-q", "SELECT 1"])
    assert args.output is None


def test_show_table_prints_to_stdout(capsys):
    cursor = FakeCursor(COLUMNS, ROWS)
    q.show_table(cursor, "SELECT 1", q.PhaseTimer(), batch_size=10, max_rows=100,
                 null_string="")
    captured = capsys.readouterr()
    assert "order_id" in captured.out and "김철수" in captured.out
    assert "3행" in captured.err


def test_show_table_stops_early(capsys):
    """표로 볼 때는 보여줄 만큼만 받는다. 스트리밍의 의미를 지킨다."""
    rows = [(i, f"n{i}", Decimal("1"), date(2026, 8, 1)) for i in range(100_000)]
    cursor = FakeCursor(COLUMNS, rows)
    q.show_table(cursor, "SELECT 1", q.PhaseTimer(), batch_size=1000, max_rows=10,
                 null_string="")

    assert cursor._offset <= 11
    assert "10행 이상" in capsys.readouterr().err


def test_show_table_max_rows_zero_reads_everything(capsys):
    rows = [(i, f"n{i}", Decimal("1"), date(2026, 8, 1)) for i in range(250)]
    cursor = FakeCursor(COLUMNS, rows)
    q.show_table(cursor, "SELECT 1", q.PhaseTimer(), batch_size=64, max_rows=0,
                 null_string="")
    err = capsys.readouterr().err
    assert "250행" in err and "이상" not in err


def test_show_table_records_phases():
    timer = q.PhaseTimer(q.PHASES)
    q.show_table(FakeCursor(COLUMNS, ROWS), "SELECT 1", timer, batch_size=10,
                 max_rows=100, null_string="")
    report = timer.report()
    assert "쿼리 실행 요청" in report and "표 출력" in report


# -- 인자 없이 실행 ---------------------------------------------------------------


def test_no_arguments_prints_help(capsys):
    """인자 없이 실행하면 오류가 아니라 도움말을 보여준다."""
    assert q.main([]) == 0
    output = capsys.readouterr().out
    assert "usage: bin/impala-query" in output
    assert "--query-file" in output


def test_no_arguments_does_not_read_the_config(tmp_path, monkeypatch, capsys):
    """설정 파일이 깨져 있어도 도움말은 떠야 한다."""
    broken = tmp_path / "config.yaml"
    broken.write_text("impala: [이건: 매핑이: 아니다", encoding="utf-8")
    monkeypatch.setattr(q.appconfig, "DEFAULT_CONFIG", broken)

    assert q.main([]) == 0
    assert "usage: bin/impala-query" in capsys.readouterr().out


def test_help_lists_examples(capsys):
    q.main([])
    assert "예시:" in capsys.readouterr().out


# -- SQL 템플릿 -------------------------------------------------------------------


def render(tmp_path, sql: str, *vars_: str) -> str:
    path = tmp_path / "q.sql"
    path.write_text(sql, encoding="utf-8")
    return q.read_query(
        argparse.Namespace(query=None, query_file=str(path), var=list(vars_))
    )


def test_variable_is_substituted(tmp_path):
    sql = "SELECT * FROM orders WHERE dt = '{{ dt }}'"
    assert render(tmp_path, sql, "dt=2026-08-01") == (
        "SELECT * FROM orders WHERE dt = '2026-08-01'"
    )


def test_multiple_variables(tmp_path):
    sql = "SELECT * FROM {{ tbl }} WHERE dt = '{{ dt }}'"
    out = render(tmp_path, sql, "tbl=sales.orders", "dt=2026-08-01")
    assert out == "SELECT * FROM sales.orders WHERE dt = '2026-08-01'"


def test_conditional_block(tmp_path):
    sql = "SELECT 1{% if status %} AND status = '{{ status }}'{% endif %}"
    assert render(tmp_path, sql, "status=PAID") == "SELECT 1 AND status = 'PAID'"
    assert render(tmp_path, sql) == "SELECT 1"


def test_undefined_variable_is_an_error(tmp_path):
    """빈 문자열로 조용히 채우면 0건을 돌려주는 쿼리가 만들어진다."""
    with pytest.raises(SystemExit) as exc:
        render(tmp_path, "SELECT * FROM orders WHERE dt = '{{ dt }}'")
    assert "dt" in str(exc.value)


def test_optional_variable_is_falsy_but_not_printable(tmp_path):
    """{% if x %} 로 물어보는 건 되지만 {{ x }} 로 출력하면 오류다."""
    assert render(tmp_path, "SELECT 1{% if x %} AND x{% endif %}") == "SELECT 1"
    with pytest.raises(SystemExit):
        render(tmp_path, "SELECT 1{% if true %} AND x = {{ x }}{% endif %}")


def test_default_filter_covers_a_missing_variable(tmp_path):
    sql = "SELECT * FROM orders WHERE dt = '{{ dt | default(\"2026-01-01\") }}'"
    assert render(tmp_path, sql) == "SELECT * FROM orders WHERE dt = '2026-01-01'"


def test_plain_sql_is_untouched(tmp_path):
    """템플릿 문법이 없으면 Jinja를 거치지 않고 그대로 넘어간다."""
    sql = "SELECT a, b FROM t WHERE x = 'a{b}c';\n-- 주석\n"
    assert render(tmp_path, sql) == sql


def test_unused_variables_warn(tmp_path, capsys):
    render(tmp_path, "SELECT 1", "dt=2026-08-01")
    assert "쓰이지 않았습니다" in capsys.readouterr().err


def test_broken_template_syntax_is_reported(tmp_path):
    with pytest.raises(SystemExit) as exc:
        render(tmp_path, "SELECT {% if %} 1")
    assert "템플릿 문법" in str(exc.value)


def test_value_may_contain_equals_and_spaces(tmp_path):
    sql = "SELECT * FROM t WHERE cond = '{{ cond }}'"
    out = render(tmp_path, sql, "cond=a = b, c")
    assert out == "SELECT * FROM t WHERE cond = 'a = b, c'"


def test_inline_query_is_also_rendered(tmp_path):
    args = argparse.Namespace(
        query="SELECT '{{ dt }}'", query_file=None, var=["dt=2026-08-01"]
    )
    assert q.read_query(args) == "SELECT '2026-08-01'"


@pytest.mark.parametrize("bad", ["dt", "=2026-08-01", " =x"])
def test_malformed_var_is_rejected(bad):
    with pytest.raises(SystemExit):
        q.parse_variables([bad])


def test_later_var_wins():
    assert q.parse_variables(["dt=a", "dt=b"]) == {"dt": "b"}


# -- sql/ 디렉터리 ----------------------------------------------------------------


def test_bare_name_is_looked_up_in_sql_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(q.appconfig, "SQL_DIR", tmp_path)
    (tmp_path / "daily.sql").write_text("SELECT 1", encoding="utf-8")
    assert q.resolve_query_file("daily.sql") == str(tmp_path / "daily.sql")


def test_existing_path_is_used_as_given(tmp_path, monkeypatch):
    monkeypatch.setattr(q.appconfig, "SQL_DIR", tmp_path / "sql")
    path = tmp_path / "elsewhere.sql"
    path.write_text("SELECT 1", encoding="utf-8")
    assert q.resolve_query_file(str(path)) == str(path)


def test_unknown_name_lists_available_files(tmp_path, monkeypatch):
    monkeypatch.setattr(q.appconfig, "SQL_DIR", tmp_path)
    (tmp_path / "daily.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "weekly.sql").write_text("SELECT 1", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        q.resolve_query_file("없는쿼리.sql")
    message = str(exc.value)
    assert "daily.sql" in message and "weekly.sql" in message


def test_explicit_sql_dir_is_searched(tmp_path):
    """설정이나 --sql-dir 로 받은 디렉터리에서 찾는다."""
    other = tmp_path / "queries"
    other.mkdir()
    (other / "daily.sql").write_text("SELECT 1", encoding="utf-8")
    assert q.resolve_query_file("daily.sql", str(other)) == str(other / "daily.sql")


def test_explicit_sql_dir_lists_available_files(tmp_path):
    other = tmp_path / "queries"
    other.mkdir()
    (other / "daily.sql").write_text("SELECT 1", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        q.resolve_query_file("없는쿼리.sql", str(other))
    assert "daily.sql" in str(exc.value)


def test_missing_sql_dir_is_reported(tmp_path):
    with pytest.raises(SystemExit) as exc:
        q.resolve_query_file("daily.sql", str(tmp_path / "없는디렉터리"))
    assert "SQL 디렉터리가 없습니다" in str(exc.value)


def test_sql_dir_comes_from_config(tmp_path):
    other = tmp_path / "queries"
    other.mkdir()
    args = parsed(tmp_path, f"impala:\n  host: h\n  user: u\nsql:\n  dir: queries\n", [])
    # 상대 경로는 설정 파일 위치 기준으로 풀린다
    assert args.sql_dir == str(other)


def test_sql_dir_flag_wins_over_config(tmp_path):
    args = parsed(
        tmp_path,
        "impala:\n  host: h\n  user: u\nsql:\n  dir: queries\n",
        ["--sql-dir", "/tmp/from-flag"],
    )
    assert args.sql_dir == "/tmp/from-flag"


def test_sql_dir_defaults_to_repo_sql(tmp_path):
    args = parsed(tmp_path, "impala:\n  host: h\n  user: u\n", [])
    assert args.sql_dir == str(q.appconfig.SQL_DIR)


def test_shipped_config_points_at_the_repo_sql_dir(tmp_path):
    """저장소에 커밋된 conf/config.yaml 의 sql.dir 이 sql/ 을 가리키는지 확인한다."""
    parser = q.build_parser()
    args = parser.parse_args(["-q", "SELECT 1", "-o", "out.csv"])
    assert q.load_sql_settings(args)["dir"] == str(q.appconfig.SQL_DIR)


def test_shipped_sql_templates_render(tmp_path):
    """저장소에 커밋된 .sql 파일이 실제로 렌더링되는지 확인한다."""
    assert q.appconfig.SQL_DIR.is_dir()
    templates = sorted(q.appconfig.SQL_DIR.glob("*.sql"))
    assert templates, "sql/ 에 .sql 파일이 없습니다"

    variables = ["dt=2026-08-01", "from_dt=2026-08-01", "to_dt=2026-08-07"]
    for path in templates:
        args = argparse.Namespace(query=None, query_file=str(path), var=variables)
        assert "{{" not in q.read_query(args)


def test_absolute_ca_cert_is_left_alone(tmp_path):
    cert = tmp_path / "elsewhere.pem"
    cert.write_text("x", encoding="utf-8")
    args = parsed(tmp_path, f"impala:\n  host: h\n  user: u\n  ca_cert: {cert}\n", [])
    assert args.ca_cert == str(cert)


def test_ca_cert_is_resolved_from_config_dir_not_cwd(tmp_path, monkeypatch):
    """작업 디렉터리를 바꿔도 같은 인증서를 가리켜야 한다."""
    monkeypatch.chdir(tmp_path.parent)
    args = parsed(tmp_path, "impala:\n  host: h\n  user: u\n  ca_cert: impala-ca.pem\n", [])
    assert args.ca_cert == str(tmp_path / "impala-ca.pem")


def test_missing_ca_cert_is_reported(tmp_path):
    with pytest.raises(SystemExit):
        parsed(tmp_path, "impala:\n  host: h\n  user: u\n  ca_cert: 없는인증서.pem\n", [])


def test_missing_ca_cert_is_ignored_without_tls(tmp_path):
    """--no-ssl 이면 인증서를 쓰지 않으므로 없어도 상관없다."""
    args = parsed(
        tmp_path, "impala:\n  host: h\n  user: u\n  ca_cert: 없는인증서.pem\n", ["--no-ssl"]
    )
    assert args.no_ssl is True


def test_command_line_ca_cert_is_used_as_given(tmp_path):
    """명령행 경로는 작업 디렉터리 기준이라 그대로 쓴다."""
    cert = tmp_path / "cli.pem"
    cert.write_text("x", encoding="utf-8")
    args = parsed(tmp_path, CONFIG, ["--ca-cert", str(cert)])
    assert args.ca_cert == str(cert)


def test_shipped_default_config_has_an_impala_section(tmp_path):
    """저장소에 커밋된 conf/config.yaml 이 실제로 파싱되는지 확인한다."""
    parser = q.build_parser()
    args = parser.parse_args(["-q", "SELECT 1", "-o", "out.csv"])
    assert q.load_impala_settings(args)["host"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
