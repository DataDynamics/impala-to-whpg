"""src/table.py 검증 — 두 쿼리 도구가 공유하는 표 출력."""

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import progress  # noqa: E402
import table as t  # noqa: E402


class FakeCursor:
    """fetchmany 로 배치를 나눠 돌려주는 최소 커서."""

    def __init__(self, rows) -> None:
        self._rows = list(rows)
        self.offset = 0
        self.calls: List[int] = []

    def fetchmany(self, size: int):
        self.calls.append(size)
        batch = self._rows[self.offset : self.offset + size]
        self.offset += len(batch)
        return batch


COLUMNS = ["order_dt", "status", "cnt"]
ROWS = [
    (date(2026, 8, 1), "결제완료", 12),
    (date(2026, 8, 1), "취소", None),
]


# -- 렌더링 ------------------------------------------------------------------------


def test_columns_are_aligned():
    lines = t.render(COLUMNS, ROWS).splitlines()
    assert lines[0].startswith("order_dt   | status")
    assert set(lines[1]) <= {"-", "+"}
    assert len(lines) == 4


def test_padding_uses_display_width():
    """한글은 두 칸을 차지하므로 그만큼 덜 채워야 열이 맞는다."""
    lines = t.render(["a", "b"], [("한글", 1), ("ab", 2)]).splitlines()
    assert progress.display_width(lines[2].split("|")[0]) == progress.display_width(
        lines[3].split("|")[0]
    )


def test_no_trailing_whitespace():
    for line in t.render(COLUMNS, ROWS).splitlines():
        assert line == line.rstrip()


def test_null_is_shown():
    assert "NULL" in t.render(COLUMNS, ROWS)
    assert "(없음)" in t.render(COLUMNS, ROWS, null_string="(없음)")


def test_booleans_are_lowercase():
    assert t.format_value(True, "") == "true"
    assert t.format_value(False, "") == "false"


def test_decimal_keeps_precision():
    assert t.format_value(Decimal("10.50"), "") == "10.50"


# -- 필요한 만큼만 받기 ------------------------------------------------------------


def test_fetch_limited_stops_early():
    """100행을 보여주려고 수백만 행을 받으면 안 된다."""
    cursor = FakeCursor([(i,) for i in range(100_000)])
    rows, truncated = t.fetch_limited(cursor, max_rows=10, batch_size=4)

    assert len(rows) == 10
    assert truncated is True
    assert cursor.offset == 11        # 더 있는지 확인할 한 행만 더 읽는다


def test_fetch_limited_reports_exact_count_when_it_fits():
    cursor = FakeCursor([(i,) for i in range(5)])
    rows, truncated = t.fetch_limited(cursor, max_rows=10, batch_size=4)

    assert len(rows) == 5
    assert truncated is False


def test_fetch_limited_on_the_boundary():
    """정확히 max_rows 만큼이면 잘린 게 아니다."""
    cursor = FakeCursor([(i,) for i in range(10)])
    rows, truncated = t.fetch_limited(cursor, max_rows=10, batch_size=4)

    assert len(rows) == 10
    assert truncated is False


def test_fetch_limited_zero_means_everything():
    cursor = FakeCursor([(i,) for i in range(1000)])
    rows, truncated = t.fetch_limited(cursor, max_rows=0, batch_size=64)

    assert len(rows) == 1000
    assert truncated is False


def test_fetch_limited_never_asks_for_more_than_it_needs():
    cursor = FakeCursor([(i,) for i in range(100)])
    t.fetch_limited(cursor, max_rows=5, batch_size=1000)
    assert cursor.calls[0] == 6       # max_rows + 1


def test_fetch_limited_reports_progress():
    seen: List[int] = []
    cursor = FakeCursor([(i,) for i in range(50)])
    t.fetch_limited(cursor, max_rows=0, batch_size=10, on_batch=seen.append)
    assert seen == [10, 20, 30, 40, 50]


def test_fetch_limited_handles_an_empty_result():
    rows, truncated = t.fetch_limited(FakeCursor([]), max_rows=10)
    assert rows == [] and truncated is False


# -- 출력 --------------------------------------------------------------------------


def test_table_goes_to_stdout_and_summary_to_stderr(capsys):
    t.print_result(COLUMNS, ROWS, truncated=False)
    captured = capsys.readouterr()
    assert "결제완료" in captured.out
    assert "2행" in captured.err
    assert "행" not in captured.out.replace("order_dt", "")


def test_truncated_result_says_it_is_incomplete(capsys):
    t.print_result(COLUMNS, ROWS, truncated=True)
    err = capsys.readouterr().err
    assert "2행 이상" in err
    assert "--max-rows 0" in err and "-o" in err


def test_empty_result_still_shows_columns(capsys):
    t.print_result(COLUMNS, [], truncated=False)
    captured = capsys.readouterr()
    assert "order_dt" in captured.out
    assert "(0행)" in captured.out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
