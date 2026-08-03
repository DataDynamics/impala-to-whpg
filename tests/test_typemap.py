import pytest

from impala_to_greenplum.typemap import (
    build_create_table_sql,
    impala_type_to_greenplum,
    qualified_name,
    quote_ident,
)


@pytest.mark.parametrize(
    "impala,expected",
    [
        ("STRING", "text"),
        ("int", "integer"),
        ("TINYINT", "smallint"),
        ("bigint", "bigint"),
        ("double", "double precision"),
        ("float", "real"),
        ("boolean", "boolean"),
        ("timestamp", "timestamp"),
        ("date", "date"),
        ("binary", "bytea"),
        ("decimal(18,2)", "numeric(18,2)"),
        ("DECIMAL( 9 , 4 )", "numeric(9,4)"),
        ("decimal", "numeric"),
        ("varchar(50)", "varchar(50)"),
        ("char(10)", "varchar(10)"),
        ("array<int>", "text"),
        ("map<string,int>", "text"),
        ("알수없는타입", "text"),
        ("", "text"),
    ],
)
def test_type_mapping(impala, expected):
    assert impala_type_to_greenplum(impala) == expected


def test_quote_ident_escapes_quotes():
    assert quote_ident("order") == '"order"'
    assert quote_ident('we"ird') == '"we""ird"'


def test_qualified_name():
    assert qualified_name("staging", "orders") == '"staging"."orders"'
    # 테이블명에 스키마가 포함되면 그쪽이 우선한다
    assert qualified_name("staging", "public.orders") == '"public"."orders"'


def test_build_create_table_sql():
    sql = build_create_table_sql(
        '"staging"."orders"',
        [("order_id", "bigint"), ("name", "string"), ("amount", "decimal(18,2)")],
        ["order_id"],
    )
    assert '"order_id" bigint' in sql
    assert '"name" text' in sql
    assert '"amount" numeric(18,2)' in sql
    assert sql.endswith('DISTRIBUTED BY ("order_id")')


def test_build_create_table_defaults_distribution_to_first_column():
    sql = build_create_table_sql('"s"."t"', [("a", "int"), ("b", "int")])
    assert sql.endswith('DISTRIBUTED BY ("a")')


def test_build_create_table_requires_columns():
    with pytest.raises(ValueError):
        build_create_table_sql('"s"."t"', [])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
