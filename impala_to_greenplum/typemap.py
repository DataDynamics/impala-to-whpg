"""Impala 타입을 Greenplum(PostgreSQL) 타입으로 변환한다."""

from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Tuple

#: 파라미터가 없는 단순 타입 매핑
_SIMPLE_TYPES = {
    "boolean": "boolean",
    "tinyint": "smallint",
    "smallint": "smallint",
    "int": "integer",
    "integer": "integer",
    "bigint": "bigint",
    "float": "real",
    "double": "double precision",
    "real": "double precision",
    "string": "text",
    "timestamp": "timestamp",
    "date": "date",
    "binary": "bytea",
    # 복합 타입은 Impala에서 문자열로 직렬화해 넘긴다는 전제로 text 처리
    "array": "text",
    "map": "text",
    "struct": "text",
}

_DECIMAL_RE = re.compile(r"^decimal\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)$")
_CHAR_RE = re.compile(r"^(var)?char\s*\(\s*(\d+)\s*\)$")

#: SQL 식별자로 그대로 쓸 수 있는 형태
_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def impala_type_to_greenplum(impala_type: str) -> str:
    """Impala 타입 문자열을 Greenplum DDL 타입으로 바꾼다.

    알 수 없는 타입은 정보 손실 없이 담을 수 있도록 ``text`` 로 떨어뜨린다.
    """
    normalized = (impala_type or "").strip().lower()
    if not normalized:
        return "text"

    if normalized in _SIMPLE_TYPES:
        return _SIMPLE_TYPES[normalized]

    decimal = _DECIMAL_RE.match(normalized)
    if decimal:
        return f"numeric({decimal.group(1)},{decimal.group(2)})"
    if normalized == "decimal":
        return "numeric"

    char = _CHAR_RE.match(normalized)
    if char:
        # Impala CHAR(n)은 공백 패딩이 있어 varchar로 받으면 비교 시 혼란이 적다
        return f"varchar({char.group(2)})"

    # array<int>, map<string,int>, struct<...> 등 파라미터가 붙은 복합 타입
    base = normalized.split("<", 1)[0].split("(", 1)[0].strip()
    return _SIMPLE_TYPES.get(base, "text")


def quote_ident(identifier: str) -> str:
    """식별자를 큰따옴표로 감싼다 (내부 큰따옴표는 이스케이프)."""
    return '"' + identifier.replace('"', '""') + '"'


def qualified_name(schema: str, table: str) -> str:
    """스키마를 포함한 완전한 테이블명을 만든다."""
    if "." in table:
        schema, table = table.split(".", 1)
    return f"{quote_ident(schema)}.{quote_ident(table)}"


def is_safe_identifier(identifier: str) -> bool:
    """따옴표 없이 써도 안전한 식별자인지 확인한다."""
    return bool(_SAFE_IDENT.match(identifier or ""))


def build_create_table_sql(
    qualified_table: str,
    columns: Sequence[Tuple[str, str]],
    distributed_by: Iterable[str] = (),
) -> str:
    """Impala 컬럼 정보로 Greenplum CREATE TABLE 문을 만든다.

    Args:
        qualified_table: ``"schema"."table"`` 형태의 대상 테이블명.
        columns: ``(컬럼명, Impala 타입)`` 목록.
        distributed_by: 분산키 컬럼명. 비어 있으면 첫 번째 컬럼을 사용한다.
    """
    if not columns:
        raise ValueError("컬럼 정보가 비어 있어 테이블을 생성할 수 없습니다.")

    column_defs: List[str] = [
        f"    {quote_ident(name)} {impala_type_to_greenplum(dtype)}" for name, dtype in columns
    ]
    keys = [k for k in distributed_by] or [columns[0][0]]
    distribution = ", ".join(quote_ident(k) for k in keys)
    return (
        f"CREATE TABLE IF NOT EXISTS {qualified_table} (\n"
        + ",\n".join(column_defs)
        + f"\n)\nDISTRIBUTED BY ({distribution})"
    )
