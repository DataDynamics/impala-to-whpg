"""설정 파일 없이 코드로 바로 적재하는 최소 예제.

    python examples/simple_load.py
"""

import logging
import os
import sys
from pathlib import Path

# 저장소를 설치하지 않고 바로 실행할 수 있도록 최상위 디렉터리를 경로에 넣는다
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from impala_to_greenplum import GreenplumConfig, ImpalaConfig, S3Config  # noqa: E402
from impala_to_greenplum.pipeline import load_query  # noqa: E402

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

# S3를 거쳐 세그먼트가 병렬로 읽어들인다. s3 인자를 빼면 COPY 방식으로 동작한다.
# 세그먼트에 s3.conf를 미리 배포해야 한다. docs/s3_external_table.md 참고.
s3 = S3Config(
    bucket=os.environ.get("S3_BUCKET", "dw-stage"),
    prefix="impala-to-greenplum",
    endpoint=os.environ.get("S3_ENDPOINT", "s3.ap-northeast-2.amazonaws.com"),
    region=os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-2"),
    gp_config="/home/gpadmin/s3.conf",
    file_size_mb=128,
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
    s3=s3,
    mode="upsert",
    key_columns=["order_id"],
    batch_size=50_000,
)

print(result.summary())
