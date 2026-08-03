"""examples/query_to_csv.py 검증 (실제 Impala 없이 가짜 커서로 실행)."""

import argparse
import gzip
import io
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))

import query_to_csv as q  # noqa: E402


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
        delimiter=",",
        null_string="",
        write_header=True,
        progress_every=0,
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

    assert lines[0] == "order_id,name,amount,order_dt"
    assert lines[1] == "1,김철수,10.50,2026-08-01"
    assert len(lines) == 4


def test_quotes_values_containing_delimiter_or_quote():
    lines = run_export().splitlines()
    # 구분자가 값 안에 있으면 따옴표로 감싸야 한다
    assert lines[2] == '2,"쉼표, 포함",,2026-08-02'
    # 큰따옴표는 두 번 반복해 이스케이프한다
    assert lines[3] == '3,"따옴표"" 포함",0,2026-08-03'


def test_null_becomes_empty_by_default():
    assert ",," in run_export().splitlines()[2]


def test_null_string_is_applied():
    lines = run_export(null_string="\\N").splitlines()
    assert lines[2] == '2,"쉼표, 포함",\\N,2026-08-02'


def test_no_header():
    lines = run_export(write_header=False).splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("1,")


def test_custom_delimiter():
    lines = run_export(delimiter="\t").splitlines()
    assert lines[0] == "order_id\tname\tamount\torder_dt"
    # 탭 구분에서는 쉼표를 감쌀 필요가 없다
    assert lines[2] == "2\t쉼표, 포함\t\t2026-08-02"


def test_empty_result_writes_header_only():
    output = run_export(rows=[])
    assert output == "order_id,name,amount,order_dt\r\n"


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
        delimiter=",",
        null_string="",
        write_header=True,
        progress_every=0,
    )
    assert buffer.getvalue().splitlines()[0] == "order_id,name"


def test_arraysize_is_set_before_execute():
    cursor = FakeCursor(COLUMNS, ROWS)
    q.export(
        cursor,
        "SELECT 1",
        io.StringIO(),
        q.PhaseTimer(),
        batch_size=1234,
        delimiter=",",
        null_string="",
        write_header=True,
        progress_every=0,
    )
    assert cursor.arraysize == 1234
    assert cursor.executed == ["SELECT 1"]


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
    )
    options.update(overrides)
    return argparse.Namespace(**options)


def test_config_uses_tls_and_ldap():
    config = q.build_config(make_args(), "secret")
    kwargs = config.connect_kwargs()

    assert kwargs["auth_mechanism"] == "PLAIN"   # impyla에서 LDAP을 뜻한다
    assert kwargs["use_ssl"] is True
    assert kwargs["user"] == "etl_user"
    assert kwargs["password"] == "secret"
    assert kwargs["ca_cert"] == "/etc/ssl/certs/impala-ca.pem"
    assert kwargs["timeout"] == 30
    # LDAP 인증에는 커버로스 서비스명이 끼어들면 안 된다
    assert "kerberos_service_name" not in kwargs


def test_config_without_ca_cert_omits_it():
    kwargs = q.build_config(make_args(ca_cert=None), "secret").connect_kwargs()
    assert kwargs["use_ssl"] is True
    assert "ca_cert" not in kwargs


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
        progress_every=0,
    )
    report = timer.report()
    for phase in ("쿼리 실행 요청", "첫 배치 대기", "데이터 수신", "CSV 쓰기"):
        assert phase in report


def test_human_readable_size():
    assert q.human(0) == "0.0B"
    assert q.human(1536) == "1.5KB"
    assert q.human(5 * 1024**3) == "5.0GB"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
