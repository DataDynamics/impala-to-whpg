-- 기간별 주문 집계. 시작일과 종료일을 준다.
--
--     bin/query-to-csv -f order_range.sql \
--         --var from_dt=2026-08-01 --var to_dt=2026-08-07 -o weekly.csv
--
-- 파티션 컬럼(order_dt)에 범위 조건을 걸어야 파티션 프루닝이 걸린다.
SELECT
    order_dt,
    status,
    count(*)                          AS order_cnt,
    CAST(sum(amount) AS DECIMAL(18,2)) AS amount_sum
  FROM sales.orders
 WHERE order_dt >= '{{ from_dt }}'
   AND order_dt <= '{{ to_dt }}'
 GROUP BY order_dt, status
 ORDER BY order_dt, status
