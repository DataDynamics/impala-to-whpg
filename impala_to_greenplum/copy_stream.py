"""행 이터레이터를 ``COPY ... FROM STDIN`` 용 바이트 스트림으로 바꾼다.

전체 결과를 메모리에 담지 않고, psycopg2의 ``copy_expert`` 가 read()를 호출할 때마다
Impala 커서에서 필요한 만큼만 꺼내 직렬화한다. 따라서 수억 건을 적재해도
메모리 사용량은 배치 하나 수준으로 유지된다.

포맷은 COPY의 기본 TEXT 포맷을 쓴다. CSV와 달리 빈 문자열과 NULL이
명확히 구분되기 때문이다(NULL은 ``\\N``).
"""

from __future__ import annotations

import io
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Iterable, Iterator, Optional

#: COPY TEXT 포맷에서 이스케이프가 필요한 문자
_ESCAPES = {
    "\\": "\\\\",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\v": "\\v",
    "\b": "\\b",
    "\f": "\\f",
}
_TRANSLATION = str.maketrans({k: v for k, v in _ESCAPES.items()})

NULL_MARKER = "\\N"


def encode_value(value: Any) -> str:
    """파이썬 값 하나를 COPY TEXT 필드 문자열로 직렬화한다."""
    if value is None:
        return NULL_MARKER
    if value is True:
        return "t"
    if value is False:
        return "f"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        # bytea 입력 문법인 \xDEADBEEF 의 백슬래시를 COPY 규칙에 맞게 이스케이프
        return "\\\\x" + bytes(value).hex()
    if not isinstance(value, str):
        value = str(value)
    return value.translate(_TRANSLATION)


def encode_row(row: Iterable[Any]) -> str:
    """행 하나를 탭 구분 COPY TEXT 라인으로 직렬화한다(끝에 개행 포함)."""
    return "\t".join(encode_value(v) for v in row) + "\n"


class RowStream(io.RawIOBase):
    """행 이터레이터를 읽기 전용 바이너리 파일 객체처럼 노출한다.

    psycopg2의 ``copy_expert`` 는 파일 객체의 ``read(size)`` 를 반복 호출하므로,
    호출될 때마다 요청 크기를 채울 만큼만 행을 꺼내 인코딩한다.
    """

    def __init__(
        self,
        rows: Iterable[Iterable[Any]],
        encoding: str = "utf-8",
        on_rows: Optional[Any] = None,
    ) -> None:
        """
        Args:
            rows: 행(시퀀스)들의 이터러블.
            encoding: 바이트 인코딩.
            on_rows: 인코딩한 행 수를 전달받는 콜백(진행 로그/카운트용).
        """
        super().__init__()
        self._rows: Iterator[Iterable[Any]] = iter(rows)
        self._encoding = encoding
        self._on_rows = on_rows
        self._buffer = bytearray()
        self._exhausted = False
        self.row_count = 0

    # -- io.RawIOBase 인터페이스 ------------------------------------------------
    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        if size is None or size < 0:
            while self._fill(1 << 20):
                pass
            chunk = bytes(self._buffer)
            self._buffer.clear()
            return chunk

        self._fill(size)
        chunk = bytes(self._buffer[:size])
        del self._buffer[: len(chunk)]
        return chunk

    def readinto(self, buffer) -> int:  # type: ignore[override]
        chunk = self.read(len(buffer))
        buffer[: len(chunk)] = chunk
        return len(chunk)

    def readline(self, size: int = -1) -> bytes:  # type: ignore[override]
        while b"\n" not in self._buffer and not self._exhausted:
            self._fill(len(self._buffer) + 65536)
        index = self._buffer.find(b"\n")
        if index == -1:
            line = bytes(self._buffer)
            self._buffer.clear()
            return line
        line = bytes(self._buffer[: index + 1])
        del self._buffer[: index + 1]
        return line

    # -- 내부 구현 --------------------------------------------------------------
    def _fill(self, size: int) -> bool:
        """버퍼가 ``size`` 바이트를 넘길 때까지 행을 인코딩한다.

        Returns:
            새로 인코딩한 행이 있으면 True.
        """
        if self._exhausted or len(self._buffer) >= size:
            return False

        produced = 0
        while len(self._buffer) < size:
            try:
                row = next(self._rows)
            except StopIteration:
                self._exhausted = True
                break
            self._buffer.extend(encode_row(row).encode(self._encoding))
            produced += 1

        if produced:
            self.row_count += produced
            if self._on_rows is not None:
                self._on_rows(produced)
        return produced > 0
