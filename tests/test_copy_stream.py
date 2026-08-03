from datetime import date, datetime
from decimal import Decimal

import pytest

from impala_to_greenplum.copy_stream import RowStream, encode_row, encode_value


def test_encode_scalar_types():
    assert encode_value(None) == "\\N"
    assert encode_value(True) == "t"
    assert encode_value(False) == "f"
    assert encode_value(42) == "42"
    assert encode_value(Decimal("10.50")) == "10.50"
    assert encode_value(date(2026, 8, 1)) == "2026-08-01"
    assert encode_value(datetime(2026, 8, 1, 12, 30, 5)) == "2026-08-01T12:30:05"


def test_encode_escapes_delimiters():
    # 탭/개행/백슬래시가 COPY 필드 경계를 깨뜨리면 안 된다
    assert encode_value("a\tb") == "a\\tb"
    assert encode_value("a\nb") == "a\\nb"
    assert encode_value("a\\b") == "a\\\\b"
    assert encode_value("\\N") == "\\\\N"  # NULL 마커와 구분되어야 한다


def test_encode_empty_string_is_not_null():
    assert encode_value("") == ""
    assert encode_value("") != encode_value(None)


def test_encode_bytes_uses_bytea_hex():
    assert encode_value(b"\xde\xad") == "\\\\xdead"


def test_encode_row_is_tab_separated():
    assert encode_row([1, "kim", None]) == "1\tkim\t\\N\n"


def test_row_stream_reads_all_rows():
    rows = [(i, f"name{i}") for i in range(1000)]
    stream = RowStream(rows)
    data = b""
    while True:
        chunk = stream.read(64)
        if not chunk:
            break
        data += chunk
    assert stream.row_count == 1000
    lines = data.decode().splitlines()
    assert len(lines) == 1000
    assert lines[0] == "0\tname0"
    assert lines[-1] == "999\tname999"


def test_row_stream_read_all_at_once():
    stream = RowStream([(1, "a"), (2, "b")])
    assert stream.read(-1) == b"1\ta\n2\tb\n"
    assert stream.read(-1) == b""


def test_row_stream_is_lazy():
    consumed = []

    def generate():
        for i in range(100):
            consumed.append(i)
            yield (i,)

    stream = RowStream(generate())
    stream.read(4)
    # 요청한 바이트를 채울 만큼만 소비해야 한다 (전체 100건이 아님)
    assert len(consumed) < 10


def test_row_stream_progress_callback():
    seen = []
    stream = RowStream([(i,) for i in range(50)], on_rows=seen.append)
    stream.read(-1)
    assert sum(seen) == 50


def test_row_stream_readline():
    stream = RowStream([(1, "a"), (2, "b")])
    assert stream.readline() == b"1\ta\n"
    assert stream.readline() == b"2\tb\n"
    assert stream.readline() == b""


def test_row_stream_handles_multibyte():
    stream = RowStream([("한글", "테스트")])
    assert stream.read(-1).decode("utf-8") == "한글\t테스트\n"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
