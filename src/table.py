"""조회 결과를 psql 처럼 열을 맞춰 표로 만든다.

``-o`` 없이 실행했을 때 두 쿼리 도구가 같은 모양으로 보여주도록 여기 모아둔다.
한글이 섞여도 열이 맞도록 문자 개수가 아니라 표시 폭으로 채운다.
"""

import sys
from typing import Any, List, Optional, Sequence, TextIO, Tuple

from progress import display_width, pad

#: 표로 보여줄 때 기본으로 출력할 최대 행 수. 0이면 제한 없음.
DEFAULT_MAX_ROWS = 100


def format_value(value: Any, null_string: str) -> str:
    if value is None:
        return null_string
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render(
    columns: Sequence[str], rows: Sequence[Sequence[Any]], null_string: str = "NULL"
) -> str:
    """열을 맞춰 표 문자열을 만든다."""
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


def fetch_limited(
    cursor: Any,
    max_rows: int,
    batch_size: int = 10_000,
    on_batch: Any = None,
) -> Tuple[List[Sequence[Any]], bool]:
    """표로 보여줄 만큼만 받는다. (행, 더 있는지) 를 돌려준다.

    100행을 보여주려고 수백만 행을 다 받을 이유가 없다. 한 행 더 받아보고 남아
    있으면 거기서 멈춘다. 그래서 **총 개수는 알 수 없고** "N행 이상"으로 말한다.
    정확한 개수가 필요하면 ``-o`` 로 파일에 받거나 ``--max-rows 0`` 을 준다.

    ``max_rows`` 가 0이면 끝까지 받는다.
    """
    rows: List[Sequence[Any]] = []
    limit = max_rows + 1 if max_rows > 0 else 0
    while True:
        size = batch_size if limit <= 0 else min(batch_size, limit - len(rows))
        if size <= 0:
            break
        batch = cursor.fetchmany(size)
        if not batch:
            break
        rows.extend(batch)
        if on_batch is not None:
            on_batch(len(rows))

    if max_rows > 0 and len(rows) > max_rows:
        return rows[:max_rows], True
    return rows, False


def print_result(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    truncated: bool,
    null_string: str = "NULL",
    stream: Optional[TextIO] = None,
) -> None:
    """표를 stdout 으로, 행 수 요약을 stderr 로 낸다.

    표는 데이터라 stdout 으로 가야 파이프로 넘길 수 있고, 요약은 보고라 stderr
    로 가야 그 데이터에 섞이지 않는다.
    """
    out = stream or sys.stdout
    if rows:
        print(render(columns, rows, null_string), file=out)
    else:
        print(" | ".join(columns), file=out)
        print("(0행)", file=out)

    if truncated:
        print(
            f"{len(rows):,}행 이상 (앞 {len(rows):,}행만 표시 — "
            "전부 보려면 --max-rows 0, 파일로 받으려면 -o)",
            file=sys.stderr,
        )
    else:
        print(f"{len(rows):,}행", file=sys.stderr)
