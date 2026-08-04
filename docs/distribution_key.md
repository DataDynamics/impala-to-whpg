# Greenplum 분산키 선정 가이드

`CREATE TABLE ... DISTRIBUTED BY (...)` 에 넣을 컬럼을 고를 때 참고하는 문서입니다.
`DISTRIBUTED BY` 를 생략하면 Greenplum이 첫 컬럼이나 기본키로 정하는데, 이 기본값이
항상 좋은 선택은 아니므로 큰 테이블은 아래 절차로 한 번 확인하는 편이 좋습니다.

분산키 선정은 결국 세 가지를 보는 일입니다.

| 지표 | 기준 |
| --- | --- |
| 고유값 개수(NDV) | 세그먼트 수의 최소 10배, 가능하면 100배 이상 |
| NULL 비율 | NULL은 전부 한 세그먼트로 몰리므로 0에 가까울수록 좋음 |
| 최빈값 비중 | `1 / 세그먼트수` 를 넘으면 그 값 하나로 이미 편중 확정 |

아래 예시는 모두 `staging.orders` 테이블을 대상으로 합니다. 실제 스키마와
테이블명으로 바꿔서 사용하세요.

쿼리는 `bin/gp-query` 로 실행합니다. 여러 줄짜리가 많아 셸에서 `\paste` 로 붙여넣는
편이 편합니다.

```bash
bin/gp-shell
dw=> \paste
| (진단 쿼리를 붙여넣고 Ctrl-D)
```

자주 쓰는 것은 `sql/` 에 두고 `-f` 로 부르거나 셸에서 `\i` 로 실행하세요.

## 1. 통계 기반 후보 스캔 (가장 빠름)

`ANALYZE` 만 되어 있으면 데이터를 읽지 않고 모든 컬럼을 한 번에 평가할 수 있습니다.

```sql
ANALYZE staging.orders;   -- 파티션 테이블이면 ANALYZE ROOTPARTITION staging.orders;

WITH seg AS (
    SELECT count(*)::numeric AS n
      FROM gp_segment_configuration
     WHERE content >= 0 AND role = 'p'
), tab AS (
    SELECT c.reltuples::numeric AS rows
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'staging' AND c.relname = 'orders'
)
SELECT
    s.attname AS column_name,
    CASE WHEN s.n_distinct < 0                        -- 음수면 전체 행 대비 비율
         THEN round(-s.n_distinct * tab.rows)
         ELSE round(s.n_distinct::numeric) END        AS est_ndv,
    round(s.null_frac::numeric, 4)                    AS null_frac,
    round(coalesce(s.most_common_freqs[1], 0)::numeric, 4) AS top_value_frac,
    seg.n::int                                        AS segments,
    CASE
      WHEN (CASE WHEN s.n_distinct < 0 THEN -s.n_distinct * tab.rows
                 ELSE s.n_distinct END) < seg.n * 10  THEN '부적합: 고유값이 세그먼트 수에 비해 너무 적음'
      WHEN s.null_frac > 0.05                          THEN '주의: NULL이 한 세그먼트로 몰림'
      WHEN coalesce(s.most_common_freqs[1], 0) > 1.0 / seg.n
                                                       THEN '주의: 최빈값 하나가 공평 분배량을 초과'
      ELSE '양호'
    END AS verdict
FROM pg_stats s
CROSS JOIN seg
CROSS JOIN tab
WHERE s.schemaname = 'staging' AND s.tablename = 'orders'
ORDER BY est_ndv DESC;
```

`n_distinct` 가 음수면 "전체 행 수 대비 비율"이라는 뜻이라 위처럼 행 수를 곱해
환산해야 합니다. 통계가 없으면 결과가 비거나 부정확하니 `ANALYZE` 를 먼저 돌리세요.

## 2. 최악 편중 직접 계산

통계가 없거나 복합키(2개 이상)를 평가할 때는 직접 집계합니다.

```sql
WITH freq AS (
    SELECT customer_id AS k, count(*) AS c
      FROM staging.orders
     GROUP BY 1
)
SELECT count(*)                                        AS ndv,
       sum(c)                                          AS total_rows,
       max(c)                                          AS max_freq,
       round(max(c)::numeric * (SELECT count(*) FROM gp_segment_configuration
                                 WHERE content >= 0 AND role = 'p') / sum(c), 2)
                                                       AS worst_skew_ratio
  FROM freq;
```

`worst_skew_ratio` 가 1을 넘으면 그 값 하나만으로도 한 세그먼트가 평균치를
초과한다는 뜻입니다. 복합키는 `GROUP BY customer_id, order_dt` 로 바꾸면 됩니다.

## 3. 실제 분배 검증 (가장 정확)

1·2번은 근사치입니다. `hashtext()` 같은 함수로 흉내내도 Greenplum이 실제로 쓰는
해시(`cdbhash`)와 다르므로, 확실히 하려면 후보 키로 임시 테이블을 만들어
세그먼트별 행 수를 직접 확인합니다.

```sql
CREATE TEMP TABLE dk_test AS
SELECT * FROM staging.orders
 WHERE random() < 0.05          -- 큰 테이블은 샘플링, 편중은 상대값이라 비교에 충분
DISTRIBUTED BY (customer_id);

SELECT gp_segment_id, count(*) AS rows
  FROM dk_test
 GROUP BY 1
 ORDER BY 2 DESC;
```

요약 지표로 보려면:

```sql
WITH seg AS (
    SELECT gp_segment_id, count(*)::numeric AS rows
      FROM dk_test GROUP BY 1
)
SELECT count(*)                                        AS segments_with_data,
       min(rows), max(rows), round(avg(rows), 1)       AS avg_rows,
       round(stddev(rows) / nullif(avg(rows), 0) * 100, 2)     AS skew_pct,
       round((max(rows) / nullif(avg(rows), 0) - 1) * 100, 2)  AS max_over_avg_pct
  FROM seg;
```

- `skew_pct`(변동계수)가 10% 이하면 아주 좋고, 30%를 넘으면 다른 키를 찾는 편이 낫습니다.
- `segments_with_data` 가 전체 세그먼트 수보다 작으면 아예 쓰이지 않는 세그먼트가 있다는 뜻입니다.
- 행이 하나도 안 들어간 세그먼트는 `GROUP BY` 결과에 나타나지 않으므로, 전체
  세그먼트 수와 반드시 비교해야 합니다.

## 4. 이미 만든 테이블 점검

현재 분산키 확인:

```sql
-- GP 6 이상
SELECT n.nspname, c.relname, pg_get_table_distributedby(c.oid) AS distributed_by
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'staging' AND c.relkind = 'r'
 ORDER BY 1, 2;
```

편중된 테이블 찾기. `gp_toolkit` 뷰는 전체 테이블을 스캔하므로 한가한 시간에 돌리세요.

```sql
SELECT skcnamespace, skcrelname, round(skccoeff::numeric, 2) AS skew_coeff
  FROM gp_toolkit.gp_skew_coefficients
 WHERE skcnamespace NOT IN ('pg_catalog', 'information_schema')
 ORDER BY skccoeff DESC
 LIMIT 20;
```

특정 테이블의 실제 편중은 임시 테이블 없이도 바로 볼 수 있습니다.

```sql
SELECT gp_segment_id, count(*) AS rows
  FROM staging.orders
 GROUP BY 1
 ORDER BY 2 DESC;
```

## 5. 균등 분배만 보면 놓치는 것

분배가 고른 것만으로 좋은 분산키가 되지는 않습니다. 우선순위는 이렇습니다.

1. **균등 분배** — 위 지표로 거릅니다.
2. **조인 키 일치** — 자주 조인하는 테이블끼리는 조인 키를 분산키로 맞춥니다.
   분산키가 다르면 조인할 때마다 세그먼트 간 재분배(motion)가 발생해서, 분배가
   아무리 고르더라도 손해가 큽니다.
3. **단일 값 조회 회피** — `WHERE order_id = 123` 처럼 단일 값 조회가 잦은 컬럼을
   분산키로 쓰면 그 쿼리가 한 세그먼트에서만 처리돼 병렬성을 잃습니다.

그 밖에:

- 분산키 컬럼은 `UPDATE` 대상이 될 수 없습니다. 값이 바뀌면 저장 세그먼트가
  달라지기 때문입니다. 자주 갱신되는 컬럼은 피하세요.
- 마땅한 후보가 없으면 `DISTRIBUTED RANDOMLY` 도 선택지입니다. 조인 시 재분배는
  항상 발생하지만 편중은 확실히 없앨 수 있습니다.
- 복합키는 컬럼을 늘릴수록 고유값이 늘어 분배는 고르지만, 조인 시 그 조합이 전부
  일치해야 지역 조인이 되므로 실익이 줄어듭니다. 2개를 넘기지 않는 편이 좋습니다.

## 확인한 컬럼 적용하기

새로 만드는 테이블이라면 `DISTRIBUTED BY` 절에 넣습니다.

```sql
CREATE TABLE staging.orders (
    order_id     bigint,
    customer_id  bigint,
    order_dt     date,
    amount       numeric(18,2)
)
DISTRIBUTED BY (customer_id);
```

이미 만들어진 테이블은 데이터를 유지한 채 바꿀 수 있습니다. 전체 재분배가 일어나므로
큰 테이블에서는 시간이 걸립니다.

```sql
ALTER TABLE staging.orders SET DISTRIBUTED BY (customer_id);
```

외부 테이블로 S3 파일을 읽어 적재하는 절차는
[S3 외부 테이블로 읽기](s3_external_table.md)에 있습니다. 분산키는 적재 대상 테이블의
속성이라 외부 테이블 쪽에는 지정하지 않습니다.
