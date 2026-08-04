-- S3에 올린 파일을 외부 테이블로 읽어 적재한다. bin/gp-query 로 실행한다.
--
--     bin/gp-query -f load_orders.sql --var dt=2026-08-01
--
-- 기본값을 쓰지 않으려면 함께 준다.
--
--     bin/gp-query -f load_orders.sql --var dt=2026-08-01 \
--         --var bucket=dw-stage --var endpoint=s3.ap-northeast-2.amazonaws.com
--
-- 세 문장이 한 트랜잭션에서 돌고, 중간에 실패하면 전부 롤백된다.
-- 외부 테이블 컬럼은 대상 테이블의 실제 타입을 그대로 따라가야 캐스팅이 없다.
--
-- FORMAT 절은 파일을 만든 옵션과 맞아야 한다. 아래는 이렇게 뽑은 파일 기준이다.
--     bin/impala-query -f daily_orders.sql --var dt=... \
--         -o orders.csv.gz --gzip --delimiter $'\t' --null-string '\N' --no-header
CREATE READABLE EXTERNAL TEMP TABLE ext_orders_{{ dt | replace("-", "") }} (
    order_id     bigint,
    customer_id  bigint,
    order_dt     date,
    amount       numeric(18,2),
    status       text
)
LOCATION ('s3://{{ endpoint | default("s3.ap-northeast-2.amazonaws.com") }}/{{ bucket | default("dw-stage") }}/orders/{{ dt }}/ region={{ region | default("ap-northeast-2") }} config={{ gp_config | default("/home/gpadmin/s3.conf") }}')
FORMAT 'TEXT' (DELIMITER E'\t' NULL E'\\N' ESCAPE E'\\')
ENCODING 'UTF8';

INSERT INTO {{ schema | default("staging") }}.orders
       (order_id, customer_id, order_dt, amount, status)
SELECT  order_id, customer_id, order_dt, amount, status
  FROM ext_orders_{{ dt | replace("-", "") }};

DROP EXTERNAL TABLE ext_orders_{{ dt | replace("-", "") }};
