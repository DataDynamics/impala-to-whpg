-- Greenplum에 적재된 주문을 날짜별로 집계한다. bin/gp-query 로 실행한다.
--
--     bin/gp-query -f order_summary.sql --var dt=2026-08-01
--
-- 스키마는 설정의 greenplum.schema 가 search_path 로 잡아준다.
-- 다른 스키마를 보려면 --var schema=... 로 준다.
SELECT
    order_dt,
    status,
    count(*)                           AS order_cnt,
    CAST(sum(amount) AS DECIMAL(18,2)) AS amount_sum
  FROM {{ schema | default("staging") }}.orders
 WHERE order_dt = '{{ dt }}'
 GROUP BY order_dt, status
 ORDER BY order_dt, status
