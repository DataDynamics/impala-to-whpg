"""설정 파일 없이 코드로 바로 적재하는 최소 예제.

    python examples/simple_load.py
"""

import logging
import os

from impala_to_greenplum import GreenplumConfig, ImpalaConfig
from impala_to_greenplum.pipeline import load_query

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

impala = ImpalaConfig(
    host=os.environ.get("IMPALA_HOST", "impala.example.com"),
    port=int(os.environ.get("IMPALA_PORT", "21050")),
    database="sales",
)

greenplum = GreenplumConfig(
    host=os.environ.get("GP_HOST", "greenplum.example.com"),
    port=int(os.environ.get("GP_PORT", "5432")),
    database=os.environ.get("GP_DATABASE", "dw"),
    user=os.environ.get("GP_USER", "etl"),
    password=os.environ.get("GP_PASSWORD"),
    schema="staging",
)

result = load_query(
    impala,
    greenplum,
    query="""
        SELECT order_id, customer_id, order_dt, amount, status
          FROM sales.orders
         WHERE order_dt = '2026-08-01'
    """,
    target_table="orders",
    mode="upsert",
    key_columns=["order_id"],
    batch_size=50_000,
)

print(result.summary())
