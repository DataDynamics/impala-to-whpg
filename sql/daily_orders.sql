-- 하루치 주문. 날짜는 --var dt=YYYY-MM-DD 로 준다.
--
--     bin/query-to-csv -f daily_orders.sql --var dt=2026-08-01 -o orders.csv
--
-- status 를 함께 주면 그 상태만 걸러낸다.
--
--     bin/query-to-csv -f daily_orders.sql --var dt=2026-08-01 --var status=PAID \
--         -o orders.csv
SELECT
    order_id,
    customer_id,
    order_dt,
    CAST(amount AS DECIMAL(18,2)) AS amount,
    status
  FROM sales.orders
 WHERE order_dt = '{{ dt }}'
{%- if status %}
   AND status = '{{ status }}'
{%- endif %}
 ORDER BY order_id
